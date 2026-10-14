"""Two independent visitor cookie jars; real API/SQLite, no model or VPS calls."""
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from apps.api.sonnic.main import create_app
from apps.bridge.contracts import JobInput
from apps.bridge.dispatch import DispatchSettings
from test_dispatch import AUTH, TOKEN, answer


@pytest.fixture
def visitors(tmp_path):
    app = create_app(str(tmp_path / 'site.db'), mode='disabled',
                     dispatch_settings=DispatchSettings(TOKEN, True))
    with TestClient(app) as owner:
        foreign = TestClient(app)
        try:
            for client in (owner, foreign):
                assert client.post('/api/v1/sessions/guest', json={}).status_code == 200
            assert owner.cookies != foreign.cookies
            assert owner.post('/api/v1/bridge/worker/heartbeat', headers=AUTH, json={'state':'ready'}).status_code == 200
            yield owner, foreign
        finally:
            foreign.close()


def enqueue(client, key, marker):
    response = client.post('/api/v1/bridge/jobs', headers={'Idempotency-Key':key},
                           json={'synthetic':True,'dream':{'dream_text':'приснился синий попугай ' + marker}})
    assert response.status_code == 201
    return response.json()['job_id']


@pytest.mark.parametrize('state', ['queued','leased','completed','deleted'])
def test_foreign_session_cannot_read_or_change_job_in_any_state(visitors, state):
    owner, foreign = visitors
    marker = 'синтетическая метка ' + str(uuid4())
    job = enqueue(owner, str(uuid4()), marker)
    if state in {'leased','completed'}:
        packet = owner.post('/api/v1/bridge/worker/claim', headers=AUTH, json={'claim_key':str(uuid4())}).json()['job']
        if state == 'completed':
            assert owner.post('/api/v1/bridge/worker/submit', headers=AUTH,
                              json=answer(JobInput.model_validate(packet)).model_dump(mode='json')).status_code == 200
    if state == 'deleted':
        assert owner.delete('/api/v1/bridge/jobs/' + job).status_code == 200
    path = '/api/v1/bridge/jobs/' + job
    for method, suffix, body in [('GET','',None),('DELETE','',None),('POST','/retry',None),
                                ('POST','/cancel',None),('POST','/review',{'approve':True,'output_sha256':'a'*64})]:
        response = foreign.request(method, path+suffix, json=body)
        assert response.status_code == 404
        assert response.json() == {'detail':'job_not_found'}
        assert marker not in response.text
        assert response.headers['cache-control'] == 'no-store'
    own = owner.get(path)
    assert own.status_code == (404 if state == 'deleted' else 200)
    if state != 'deleted':
        assert own.json()['state'] == state
        assert marker in own.json()['dream_text']


def test_shared_idempotency_key_cannot_cross_owner_boundary(visitors):
    owner, foreign = visitors
    key = str(uuid4())
    first = enqueue(owner, key, 'первый посетитель')
    second = enqueue(foreign, key, 'второй посетитель')
    assert first != second
    assert enqueue(owner, key, 'первый посетитель') == first
    assert enqueue(foreign, key, 'второй посетитель') == second


def test_cookie_does_not_grant_dispatch_access_or_allow_owner_injection(visitors):
    owner, foreign = visitors
    for client in (owner,foreign):
        assert client.post('/api/v1/bridge/worker/claim',json={'claim_key':str(uuid4())}).status_code == 401
        response = client.post('/api/v1/bridge/jobs',headers={'Idempotency-Key':str(uuid4())},
                               json={'case_id':'blue-parrot-ru','owner':'other-visitor'})
        assert response.status_code == 422


def test_cross_site_delete_with_real_owner_cookie_is_rejected(visitors):
    owner, _ = visitors
    job = enqueue(owner, str(uuid4()), 'защита удаления')
    path = '/api/v1/bridge/jobs/' + job
    assert owner.delete(path,headers={'Origin':'https://untrusted.invalid','Sec-Fetch-Site':'cross-site'}).status_code == 403
    assert owner.get(path).json()['state'] == 'queued'
