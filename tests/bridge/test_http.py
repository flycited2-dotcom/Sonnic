"""In-process HTTP/OAuth lifecycle checks; these do not connect to ChatGPT."""
import base64
import hashlib
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from apps.api.sonnic.main import create_app
from apps.bridge.oauth import BridgeSettings


ORIGIN = 'http://127.0.0.1:3000'
CALLBACK = 'https://chatgpt.com/sonnic-audit-callback'
VERIFIER = 'a' * 43


@pytest.fixture
def client(tmp_path):
    settings = BridgeSettings(origin=ORIGIN, redirect_uri=CALLBACK)
    app = create_app(str(tmp_path / 'site.sqlite3'), mode='demo', bridge_settings=settings)
    with TestClient(app, base_url=ORIGIN, raise_server_exceptions=False) as session:
        assert session.post('/api/v1/sessions/guest', json={}).status_code == 200
        yield session


def authorization_params():
    challenge = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip('=')
    return {
        'response_type': 'code', 'client_id': 'sonnic-chatgpt', 'redirect_uri': CALLBACK,
        'resource': ORIGIN + '/mcp', 'scope': 'sonnic:process',
        'code_challenge_method': 'S256', 'code_challenge': challenge, 'state': 'test-state-' + 'x' * 32,
    }


def test_chatgpt_optional_ui_locale_preserves_oauth_bindings(client):
    params = authorization_params() | {'ui_locales': 'ru-RU'}
    response = client.get('/oauth/authorize', params=params, follow_redirects=False)
    assert response.status_code == 303
    forwarded = parse_qs(urlsplit(response.headers['location']).query)
    assert forwarded == {key: [value] for key, value in authorization_params().items()}
    for changed in ({'resource': 'https://other.example/mcp'},
                    {'redirect_uri': 'https://other.example/callback'},
                    {'ui_locales': '<script>'}, {'unexpected': 'value'}):
        response = client.get('/oauth/authorize', params=params | changed, follow_redirects=False)
        assert response.status_code == 409


def consent(client):
    params = authorization_params()
    response = client.post('/api/v1/bridge/oauth/prepare', json={'params': params})
    assert response.status_code == 200, response.text
    return response.json()['handle']


def code_request(client):
    response = client.post('/api/v1/bridge/oauth/approve', json={'handle': consent(client), 'allow': True})
    assert response.status_code == 200, response.text
    redirect = urlsplit(response.json()['redirect'])
    values = parse_qs(redirect.query)
    assert redirect.scheme + '://' + redirect.netloc + redirect.path == CALLBACK
    assert values['state'] == [authorization_params()['state']]
    assert values['iss'] == [ORIGIN]
    return {
        'grant_type': 'authorization_code', 'code': values['code'][0],
        'client_id': 'sonnic-chatgpt', 'redirect_uri': CALLBACK,
        'resource': ORIGIN + '/mcp', 'code_verifier': VERIFIER,
    }


def token(client):
    response = client.post('/oauth/token', data=code_request(client))
    assert response.status_code == 200, response.text
    return response.json()['access_token']


def rpc(client, bearer, method, params):
    return client.post('/mcp', headers={
        'Authorization': 'Bearer ' + bearer,
        'Accept': 'application/json, text/event-stream',
    }, json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})


def create_job(client):
    response = client.post('/api/v1/bridge/jobs', headers={'Idempotency-Key': str(uuid4())},
                           json={'case_id': 'blue-parrot-ru'})
    assert response.status_code == 201, response.text
    return response.json()['job_id']


def claim(client, bearer, job_id):
    response = rpc(client, bearer, 'tools/call', {'name': 'claim_job', 'arguments': {
        'job_id': job_id, 'claim_key': str(uuid4()),
    }})
    assert response.status_code == 200, response.text
    assert response.json()['result']['isError'] is False
    return response.json()['result']['structuredContent']


def candidate(packet):
    result = {key: packet[key] for key in (
        'contract_version', 'knowledge_version', 'job_id', 'lease_id', 'attempt', 'input_sha256', 'output_locale',
    )}
    result.update(outcome='needs_context', reflection={
        'title': 'A blue parrot',
        'symbols': [{'label': 'Blue parrot', 'evidence': 'синий попугай'}],
        'summary': 'The report describes a blue parrot, but its personal significance is unknown.',
        'key_connection': 'Begin with the associations this bird evokes for you.',
        'reflection_question': 'What was the parrot doing in this dream?',
    }, safe_message=None, source_use=[])
    return result


def test_oauth_mcp_candidate_requires_owner_review(client):
    """A model-scoped token can deliver, but cannot approve or read another owner's job."""
    bearer = token(client)
    job_id = create_job(client)
    init = rpc(client, bearer, 'initialize', {'protocolVersion': '2025-11-25'})
    assert init.json()['result']['protocolVersion'] == '2025-11-25'
    names = {item['name'] for item in rpc(client, bearer, 'tools/list', {}).json()['result']['tools']}
    assert names == {'claim_job', 'submit_result'}
    packet = claim(client, bearer, job_id)
    result = candidate(packet)
    submitted = rpc(client, bearer, 'tools/call', {'name': 'submit_result', 'arguments': result})
    assert submitted.json()['result']['structuredContent'] == {'state': 'awaiting_review', 'published': False}
    snapshot = client.get('/api/v1/bridge/jobs/' + job_id).json()
    assert snapshot['candidate'] is not None and snapshot['result'] is None
    bad_review = client.post('/api/v1/bridge/jobs/' + job_id + '/review', json={
        'approve': True, 'output_sha256': '0' * 64,
    })
    assert bad_review.status_code == 409
    saved_cookies = dict(client.cookies)
    client.cookies.clear()
    assert client.post('/api/v1/bridge/jobs/' + job_id + '/review', headers={'Authorization': 'Bearer ' + bearer},
                       json={'approve': True, 'output_sha256': snapshot['output_sha256']}).status_code == 401
    assert client.post('/api/v1/sessions/guest', json={}).status_code == 200
    assert client.get('/api/v1/bridge/jobs/' + job_id).status_code == 404
    foreign_token = token(client)
    foreign_claim = rpc(client, foreign_token, 'tools/call', {'name': 'claim_job', 'arguments': {
        'job_id': job_id, 'claim_key': str(uuid4()),
    }})
    assert foreign_claim.json()['result']['isError'] is True
    client.cookies.clear()
    client.cookies.update(saved_cookies)
    reviewed = client.post('/api/v1/bridge/jobs/' + job_id + '/review', json={
        'approve': True, 'output_sha256': snapshot['output_sha256'],
    }).json()
    assert reviewed['state'] == 'completed' and reviewed['result'] is not None
    assert reviewed['candidate'] is None
    assert 'lease_id' not in reviewed['result'] and 'source_use' not in reviewed['result']
    assert client.post('/api/v1/bridge/connections/revoke').status_code == 204
    assert rpc(client, bearer, 'tools/list', {}).status_code == 401


def test_authorization_code_requires_pkce_and_is_single_use(client):
    request = code_request(client)
    assert client.post('/oauth/token', data={**request, 'code_verifier': 'b' * 43}).status_code == 400
    assert client.post('/oauth/token', data={**request, 'resource': 'https://other.invalid/mcp'}).status_code == 400
    assert client.post('/oauth/token', data=request).status_code == 200
    assert client.post('/oauth/token', data=request).status_code == 400


def test_consent_is_owner_bound_and_denial_does_not_issue_code(client):
    handle = consent(client)
    cookies = dict(client.cookies)
    client.cookies.clear()
    client.post('/api/v1/sessions/guest', json={})
    assert client.post('/api/v1/bridge/oauth/approve', json={'handle': handle, 'allow': True}).status_code == 409
    client.cookies.clear()
    client.cookies.update(cookies)
    response = client.post('/api/v1/bridge/oauth/approve', json={'handle': handle, 'allow': False})
    params = parse_qs(urlsplit(response.json()['redirect']).query)
    assert params['error'] == ['access_denied'] and 'code' not in params
    assert client.post('/api/v1/bridge/oauth/approve', json={'handle': handle, 'allow': True}).status_code == 409


def test_code_and_bearer_expire_without_waiting(client):
    q = client.app.state.bridge_queue
    now = [q.clock()]
    q.clock = lambda: now[0]
    request = code_request(client)
    now[0] += 121
    assert client.post('/oauth/token', data=request).status_code == 400
    bearer = token(client)
    assert rpc(client, bearer, 'ping', {}).status_code == 200
    now[0] += 901
    assert rpc(client, bearer, 'ping', {}).status_code == 401


@pytest.mark.parametrize('version', [None, [], {}, 1, True, ''])
def test_malformed_initialize_returns_jsonrpc_error(client, version):
    response = rpc(client, token(client), 'initialize', {'protocolVersion': version})
    assert response.status_code == 200
    assert response.json()['error']['code'] == -32602


def test_bridge_request_boundary(client):
    unauthenticated = client.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'ping'})
    assert unauthenticated.status_code == 401
    assert 'oauth-protected-resource' in unauthenticated.headers['www-authenticate']
    metadata = client.get('/.well-known/oauth-protected-resource').json()
    assert metadata['resource'] == ORIGIN + '/mcp'
    assert client.get('/.well-known/oauth-protected-resource', headers={'Host': 'attacker.invalid'}).status_code == 400
    assert client.post('/api/v1/bridge/oauth/prepare', headers={'Origin': 'https://attacker.invalid'},
                       json={'params': authorization_params()}).status_code == 403
    assert client.post('/oauth/token', headers={'Content-Type': 'application/x-www-form-urlencoded'},
                       content='x' * 65537).status_code == 413
    auth = authorization_params()
    assert client.get('/oauth/authorize', params=list(auth.items()) + [('client_id', 'other')],
                      follow_redirects=False).status_code == 400
    assert client.post('/api/v1/bridge/oauth/prepare', json={
        'params': {**auth, 'redirect_uri': 'https://attacker.invalid/callback'},
    }).status_code == 409


def test_cancelled_job_rejects_delayed_mcp_result(client):
    bearer = token(client)
    job_id = create_job(client)
    result = candidate(claim(client, bearer, job_id))
    assert client.post('/api/v1/bridge/jobs/' + job_id + '/cancel').status_code == 200
    response = rpc(client, bearer, 'tools/call', {'name': 'submit_result', 'arguments': result})
    assert response.json()['result']['isError'] is True
    snapshot = client.get('/api/v1/bridge/jobs/' + job_id).json()
    assert snapshot['state'] == 'cancelled'
    assert snapshot['dream_text'] is None and snapshot['result'] is None
