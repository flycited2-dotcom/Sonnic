"""Four invented visitor jobs, one live worker. Never retries model submissions."""
import argparse
import asyncio
from contextlib import AsyncExitStack
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from uuid import uuid4

import httpx

ORIGIN = 'https://sonrazbor.ru'
ROOT = Path(__file__).resolve().parents[1]
CASES = [
    ('ru-RU', 'Мне приснился медный самовар на заснеженной скамейке. Рядом прыгал фиолетовый бумажный заяц. Я удивился.'),
    ('en-US', 'I dreamed of a glass violin on a sandy island. A green paper kite circled overhead. I felt curious.'),
    ('ru-RU', 'Во сне я увидел полосатый чемодан на синем плоту. Внутри лежала деревянная ложка. Я улыбнулся.'),
    ('en-US', 'I dreamed of a velvet umbrella in an empty greenhouse. A silver toy bus stood beneath it. I felt peaceful.'),
]


class SshTransport(httpx.AsyncBaseTransport):
    """Preserve HTTPS cookie semantics; send bytes over an existing SSH forward."""
    def __init__(self):
        self.inner = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request):
        if str(request.url).split('/api/')[0] != ORIGIN:
            raise ValueError('unexpected_validation_destination')
        forwarded = httpx.Request(request.method,
            request.url.copy_with(scheme='http', host='127.0.0.1', port=3181),
            headers=request.headers, content=await request.aread(), extensions=request.extensions)
        return await self.inner.handle_async_request(forwarded)

    async def aclose(self):
        await self.inner.aclose()


async def run(via_ssh=False):
    folder = ROOT / 'data/worker/validation' / datetime.now(timezone.utc).strftime('multi-%Y%m%dT%H%M%SZ')
    folder.mkdir(parents=True, exist_ok=False)
    report = {'transport': 'ssh_forward' if via_ssh else 'public_https', 'public_ui_verified': False,
              'status': 'running', 'jobs': []}
    def persist():
        (folder / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    persist()
    print('report:' + str(folder / 'report.json'), flush=True)
    async with AsyncExitStack() as stack:
        visitors = [await stack.enter_async_context(httpx.AsyncClient(base_url=ORIGIN,
            transport=SshTransport() if via_ssh else None, trust_env=False, timeout=15,
            follow_redirects=False)) for _ in CASES]
        async def send(index):
            client = visitors[index]
            locale, dream = CASES[index]
            response = await client.post('/api/v1/bridge/jobs', headers={'Idempotency-Key': str(uuid4())},
                json={'synthetic': True, 'dream': {'dream_text': dream, 'locale': locale}})
            response.raise_for_status()
            job = response.json()['job_id']
            report['jobs'].append({'visitor': index, 'job_id': job, 'state': 'queued',
                                   'started': time.time(), 'foreign_denied': False})
            persist()
            print(f'visitor:{index+1} queued:{job}', flush=True)
        try:
            for client in visitors:
                config = (await client.get('/api/v1/bridge/config')).raise_for_status().json()
                if not config['chatgpt_connected'] or not config['synthetic_only']:
                    raise ValueError('worker_not_ready_or_wrong_mode')
                (await client.post('/api/v1/sessions/guest', json={})).raise_for_status()
            # Finish all enqueue observations even if one fails, so known jobs are recoverable.
            sent = await asyncio.gather(*(send(i) for i in range(4)), return_exceptions=True)
            if any(isinstance(value, BaseException) for value in sent):
                raise ValueError('enqueue_failed_or_uncertain_no_retry')
            if len({job['job_id'] for job in report['jobs']}) != 4:
                raise ValueError('duplicate_job_id')
            deadline = time.monotonic() + 630
            while time.monotonic() < deadline:
                for job in report['jobs']:
                    if job['state'] in {'completed', 'failed', 'cancelled', 'expired'}:
                        continue
                    client = visitors[job['visitor']]
                    snapshot = (await client.get('/api/v1/bridge/jobs/' + job['job_id'])).raise_for_status().json()
                    if snapshot['dream_text'] != CASES[job['visitor']][1]:
                        raise ValueError('wrong_visitor_input')
                    job.update(state=snapshot['state'], error=snapshot.get('error'))
                    if job['state'] == 'completed':
                        job.update(seconds=round(time.time()-job['started'], 2), result=snapshot['result'])
                        result = snapshot['result']
                        if result['locale'] != CASES[job['visitor']][0]:
                            raise ValueError('wrong_visitor_locale')
                        if result.get('reflection') and any(symbol['evidence'] not in CASES[job['visitor']][1]
                                                          for symbol in result['reflection']['symbols']):
                            raise ValueError('wrong_visitor_evidence')
                        readback = (await client.get('/api/v1/bridge/jobs/' + job['job_id'])).raise_for_status().json()
                        if readback['state'] != 'completed' or readback['result'] != result:
                            raise ValueError('result_not_persisted')
                        job['result_readback_verified'] = True
                        print(f"visitor:{job['visitor']+1} completed:{job['seconds']}s", flush=True)
                    persist()
                    if job.get('error') in {'model_limit', 'login_required', 'submission_uncertain', 'page_changed', 'browser_unavailable'}:
                        raise ValueError('worker_pause_no_retry')
                if all(job['state'] in {'completed', 'failed', 'cancelled', 'expired'} for job in report['jobs']):
                    break
                await asyncio.sleep(10)
            if any(job['state'] not in {'completed', 'failed', 'cancelled', 'expired'} for job in report['jobs']):
                raise ValueError('validation_deadline_no_retry')
            for job in report['jobs']:
                for index, client in enumerate(visitors):
                    if index != job['visitor']:
                        response = await client.get('/api/v1/bridge/jobs/' + job['job_id'])
                        if response.status_code != 404:
                            raise ValueError('foreign_result_access')
                job['foreign_denied'] = True
            report['status'] = 'completed' if all(job['state'] == 'completed' for job in report['jobs']) else 'failed'
        except Exception as exc:
            report.update(status='failed', error_type=type(exc).__name__)
            # Preserve jobs and the real worker's journal for recovery. No automatic
            # re-enqueue, regeneration, cancellation or deletion after uncertainty.
            print('validation_failed:' + type(exc).__name__, flush=True)
        finally:
            receipts = ROOT / 'data/worker/receipts.jsonl'
            if receipts.exists():
                rows = [json.loads(line) for line in receipts.read_text(encoding='utf-8').splitlines()]
                for job in report['jobs']:
                    receipt = next((row for row in reversed(rows) if row.get('job_id') == job['job_id']), None)
                    if receipt and 'started' in receipt:
                        job['processing_seconds'] = round(receipt['finished'] - receipt['started'], 2)
                        job['output_error_types'] = receipt.get('output_error_types', [])
            persist()
    return report['status'] == 'completed'


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true', required=True)
    parser.add_argument('--via-ssh', action='store_true')
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(run(args.via_ssh)) else 2)
