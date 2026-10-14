"""Service transport tests: real SQLite/HTTP, synthetic model output only."""
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from apps.api.sonnic.main import create_app
from apps.api.sonnic.models import DreamInput
from apps.bridge.contracts import JobOutput
from apps.bridge.dispatch import DispatchSettings
from apps.bridge.queue import BridgeError, Principal, Queue

SERVICE = Principal('sonnic-dispatcher', 'service', frozenset({'dispatch'}))
SITE = Principal('browser', 'owner', frozenset({'site'}))
TOKEN = 'test-only-service-token-' + 's' * 32
AUTH = {'Authorization': 'Bearer ' + TOKEN}


def answer(packet):
    values = packet.model_dump(mode='json')
    return JobOutput(**{k: values[k] for k in ('job_id', 'lease_id', 'attempt', 'input_sha256', 'output_locale')},
                     outcome='needs_context', reflection={
                         'title': 'Синий попугай', 'symbols': [{'label': 'Птица', 'evidence': 'синий попугай'}],
                         'summary': 'В рассказе есть синий попугай, но его личное значение пока неизвестно.',
                         'key_connection': 'Можно начать с ваших собственных ассоциаций с птицей.',
                         'reflection_question': 'Что делал попугай в вашем сне?'}, safe_message=None, source_use=[])


@pytest.fixture
def queue(tmp_path):
    now = [1000.0]
    q = Queue(tmp_path / 'queue.db', clock=lambda: now[0], job_ttl=600)
    return q, now


def test_atomic_global_claim_and_repeated_lost_response(queue):
    q, _ = queue
    for owner in ('one', 'two'):
        q.enqueue(Principal('browser', owner, frozenset({'site'})), 'blue-parrot-ru', uuid4())
    keys = [uuid4(), uuid4()]
    def claim(key):
        try:
            return q.claim_next(SERVICE, key)
        except BridgeError as exc:
            return str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, keys))
    assert results.count('worker_busy') == 1
    index = next(i for i, value in enumerate(results) if value != 'worker_busy')
    packet = results[index]
    assert q.claim_next(SERVICE, keys[index]) == packet
    assert q.submit_dispatch(SERVICE, answer(packet)) == 'completed'
    assert q.submit_dispatch(SERVICE, answer(packet)) == 'completed'
    with pytest.raises(BridgeError, match='claim_no_longer_active'):
        q.claim_next(SERVICE, keys[index])
    assert q.claim_next(SERVICE, uuid4()).job_id != packet.job_id


def test_lease_renewal_deadline_and_stale_attempt(queue):
    q, now = queue
    job = UUID(q.enqueue(SITE, 'blue-parrot-ru', uuid4()))
    packet = q.claim_next(SERVICE, uuid4())
    now[0] += 100
    assert q.renew(SERVICE, job, packet.lease_id, packet.attempt)['lease_until'] == 1220
    now[0] += 121
    with pytest.raises(BridgeError, match='job_not_leased'):
        q.renew(SERVICE, job, packet.lease_id, packet.attempt)
    q.retry(SITE, job)
    newer = q.claim_next(SERVICE, uuid4())
    with pytest.raises(BridgeError, match='stale_or_mismatched_result'):
        q.submit_dispatch(SERVICE, answer(packet))
    for _ in range(5):
        now[0] += 100
        q.renew(SERVICE, job, newer.lease_id, newer.attempt)
    now[0] += 100
    with pytest.raises(BridgeError, match='job_not_leased'):
        q.submit_dispatch(SERVICE, answer(newer))


def test_cancel_late_submit_and_model_validation(queue):
    q, _ = queue
    job = UUID(q.enqueue(SITE, 'blue-parrot-ru', uuid4()))
    packet = q.claim_next(SERVICE, uuid4())
    result = answer(packet)
    result.reflection.symbols[0].evidence = 'не было во сне'
    with pytest.raises(BridgeError, match='ungrounded_evidence'):
        q.submit_dispatch(SERVICE, result)
    q.cancel(SITE, job)
    with pytest.raises(BridgeError, match='stale_or_mismatched_result'):
        q.submit_dispatch(SERVICE, answer(packet))


def test_personal_gate_worker_heartbeat_and_scope(queue):
    q, now = queue
    q.enqueue_dream(SITE, DreamInput(dream_text='Мне приснился синий попугай у окна.', locale='ru-RU'), uuid4())
    assert q.claim_next(SERVICE, uuid4()) is None
    assert q.availability() == 'offline'
    q.heartbeat(SERVICE, 'ready')
    assert q.availability() == 'ready'
    now[0] += 45
    assert q.availability() == 'offline'
    with pytest.raises(BridgeError, match='forbidden'):
        q.claim_next(SITE, uuid4())
    assert q.claim_next(SERVICE, uuid4(), allow_personal=True).data_class == 'personal'


def test_failed_service_retry_via_mcp_keeps_manual_review(queue):
    q, _ = queue
    job = UUID(q.enqueue(SITE, 'blue-parrot-ru', uuid4()))
    packet = q.claim_next(SERVICE, uuid4())
    q.fail_dispatch(SERVICE, job, packet.lease_id, packet.attempt, 'page_changed')
    q.retry(SITE, job)
    manual = Principal('mcp', SITE.owner, frozenset({'worker'}))
    newer = q.claim(manual, job, uuid4())
    assert q.submit(manual, answer(newer)) == 'awaiting_review'


def test_http_service_auth_auto_publication_and_two_visitors(tmp_path):
    app = create_app(str(tmp_path / 'site.db'), mode='disabled',
                     dispatch_settings=DispatchSettings(TOKEN, True))
    with TestClient(app) as client:
        client.post('/api/v1/sessions/guest', json={})
        assert client.post('/api/v1/bridge/worker/claim', json={'claim_key': str(uuid4())}).status_code == 401
        assert client.post('/api/v1/bridge/worker/heartbeat', headers=AUTH, json={'state': 'ready'}).status_code == 200
        config = client.get('/api/v1/bridge/config').json()
        assert config['automatic_dispatch'] and config['chatgpt_connected'] and config['synthetic_only']
        request = {'dream': {'dream_text': 'Мне приснился синий попугай возле окна.', 'locale': 'ru-RU'}}
        assert client.post('/api/v1/bridge/jobs', headers={'Idempotency-Key': str(uuid4())}, json=request).status_code == 403
        response = client.post('/api/v1/bridge/jobs', headers={'Idempotency-Key': str(uuid4())}, json=request | {'synthetic': True})
        assert response.status_code == 201
        job = response.json()['job_id']
        cookies = dict(client.cookies)
        client.cookies.clear()
        from apps.bridge.contracts import JobInput
        packet = JobInput.model_validate(client.post('/api/v1/bridge/worker/claim', headers=AUTH, json={'claim_key': str(uuid4())}).json()['job'])
        assert packet.job_id == UUID(job)
        assert client.post('/api/v1/bridge/worker/submit', headers=AUTH, json=answer(packet).model_dump(mode='json')).json()['state'] == 'completed'
        client.post('/api/v1/sessions/guest', json={})
        assert client.get('/api/v1/bridge/jobs/' + job).status_code == 404
        client.cookies.clear()
        client.cookies.update(cookies)
        snapshot = client.get('/api/v1/bridge/jobs/' + job).json()
        assert snapshot['state'] == 'completed' and snapshot['candidate'] is None
        assert snapshot['result']['reflection']['title'] == 'Синий попугай'
        assert 'lease_id' not in snapshot['result']
        assert TOKEN not in str(snapshot) + str(config)


def test_unavailable_worker_rejects_new_jobs(tmp_path):
    app = create_app(str(tmp_path / 'site.db'), mode='disabled', dispatch_settings=DispatchSettings(TOKEN, True))
    with TestClient(app) as client:
        client.post('/api/v1/sessions/guest', json={})
        assert client.post('/api/v1/bridge/jobs', headers={'Idempotency-Key': str(uuid4())}, json={'case_id': 'blue-parrot-ru'}).status_code == 503
