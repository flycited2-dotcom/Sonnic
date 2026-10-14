"""Recovery checks inject network loss around the real service queue."""
import asyncio
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.api.sonnic.main import create_app
from apps.bridge.contracts import JobInput
from apps.bridge.dispatch import DispatchSettings
from apps.worker.browser import parse_answer
from apps.worker.main import Journal, Worker
from test_dispatch import TOKEN, AUTH, answer


class Browser:
    def __init__(self):
        self.generations = 0
        self.prepares = 0
        self.releases = []

    async def prepare(self):
        self.prepares += 1

    async def release(self, record):
        self.releases.append(record['packet']['job_id'])

    async def generate(self, record, save, prompt):
        assert 'SONNIC DIRECT TRANSPORT' in prompt and TOKEN not in prompt
        self.generations += 1
        record.update(stage='sent', chat_url='https://chatgpt.com/c/synthetic-test')
        save(record)
        return answer(JobInput.model_validate(record['packet'])).model_dump(mode='json')


@pytest.mark.parametrize('lost_endpoint', ['claim', 'submit', 'submit_before_server'])
def test_restart_reuses_claim_and_delivers_without_regenerating(tmp_path, lost_endpoint):
    app = create_app(str(tmp_path / 'site.db'), mode='disabled', dispatch_settings=DispatchSettings(TOKEN, True))
    with TestClient(app) as site:
        site.post('/api/v1/sessions/guest', json={})
        site.post('/api/v1/bridge/worker/heartbeat', headers=AUTH, json={'state': 'ready'})
        job = site.post('/api/v1/bridge/jobs', headers={'Idempotency-Key': str(uuid4())}, json={'case_id': 'blue-parrot-ru'}).json()['job_id']
        lost = [False]
        def transport(request):
            if request.url.path.endswith('/submit') and lost_endpoint == 'submit_before_server' and not lost[0]:
                lost[0] = True
                raise httpx.ConnectError('simulated offline before submit', request=request)
            response = site.post(request.url.path, headers=AUTH, json=json.loads(request.content))
            if request.url.path.endswith('/' + lost_endpoint) and not lost[0]:
                lost[0] = True
                raise httpx.ReadError('simulated lost response', request=request)
            return httpx.Response(response.status_code, json=response.json(), request=request)
        browser = Browser()
        async def exercise():
            async with httpx.AsyncClient(base_url='https://testserver', transport=httpx.MockTransport(transport)) as client:
                journal = Journal(tmp_path / 'journal')
                with pytest.raises(httpx.TransportError):
                    await Worker(client, browser, journal).tick()
                before = journal.read()
                assert before['stage'] == ('claiming' if lost_endpoint == 'claim' else 'delivery')
                assert browser.releases == []
                await Worker(client, browser, Journal(journal.folder)).tick()
                assert journal.read() is None
                assert browser.generations == 1
                assert browser.releases == [job]
                assert site.get('/api/v1/bridge/jobs/' + job).json()['state'] == 'completed'
                receipt = (journal.folder / 'receipts.jsonl').read_text()
                assert 'dream_text' not in receipt and 'reflection' not in receipt
        asyncio.run(exercise())


def test_cancelled_lease_stops_generation_and_clears_journal(tmp_path):
    packet = JobInput.model_validate(json.loads((Path(__file__).resolve().parents[2] / 'chatgpt-project/examples/blue-parrot.input.json').read_text(encoding='utf-8')))
    journal = Journal(tmp_path)
    journal.save({'stage': 'sent', 'claim_key': str(uuid4()), 'packet': packet.model_dump(mode='json'), 'chat_url': 'https://chatgpt.com/c/synthetic'})
    cancelled = []
    class WaitingBrowser(Browser):
        async def generate(self, *args):
            try:
                await asyncio.sleep(100)
            finally:
                cancelled.append(True)
    async def exercise():
        renewals = 0
        def transport(request):
            nonlocal renewals
            if request.url.path.endswith('/renew'):
                renewals += 1
                if renewals == 1:
                    return httpx.Response(200, json={'lease_until': 1000}, request=request)
            if request.url.path.endswith('/heartbeat'):
                return httpx.Response(200, json={'ok': True}, request=request)
            return httpx.Response(409, json={'detail': 'stale_or_mismatched_result'}, request=request)
        async with httpx.AsyncClient(base_url='https://testserver', transport=httpx.MockTransport(transport)) as client:
            assert await Worker(client, WaitingBrowser(), journal).tick()
    asyncio.run(exercise())
    assert journal.read() is None
    assert cancelled == [True]


@pytest.mark.parametrize('detail', ['job_not_leased', 'stale_or_mismatched_result'])
def test_recovered_claim_checks_lease_before_sending(tmp_path, detail):
    packet = JobInput.model_validate_json((Path(__file__).resolve().parents[2] / 'chatgpt-project/examples/blue-parrot.input.json').read_text(encoding='utf-8'))
    journal = Journal(tmp_path)
    journal.save({'stage': 'claimed', 'claim_key': str(uuid4()), 'packet': packet.model_dump(mode='json')})
    browser = Browser()
    async def exercise():
        async def transport(request):
            # Yield as a real network request would, giving an incorrectly
            # concurrent generation enough time to submit the cancelled dream.
            await asyncio.sleep(0)
            return httpx.Response(409, json={'detail': detail}, request=request)
        async with httpx.AsyncClient(base_url='https://testserver', transport=httpx.MockTransport(transport)) as client:
            assert await Worker(client, browser, journal).tick()
    asyncio.run(exercise())
    assert browser.generations == 0
    assert journal.read() is None


def test_model_output_is_correlated_and_must_be_complete():
    packet = JobInput.model_validate_json((Path(__file__).resolve().parents[2] / 'chatgpt-project/examples/blue-parrot.input.json').read_text(encoding='utf-8'))
    raw = answer(packet).model_dump_json()
    assert parse_answer('```json\n' + raw + '\n```', packet.model_dump(mode='json'))['job_id'] == str(packet.job_id)
    with pytest.raises(ValueError):
        parse_answer(raw[:-10], packet.model_dump(mode='json'))
    with pytest.raises(ValueError, match='correlation_mismatch'):
        parse_answer(raw, packet.model_dump(mode='json') | {'job_id': str(uuid4())})
