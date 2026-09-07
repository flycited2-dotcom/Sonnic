import httpx
import pytest
from fastapi.testclient import TestClient
from apps.bridge.gateway import create_gateway


def test_gateway_blocks_non_lab_routes():
    seen = []
    def upstream(request):
        seen.append(request)
        return httpx.Response(200, text='ok')
    with TestClient(create_gateway(transport=httpx.MockTransport(upstream))) as client:
        for path in ['/api/v1/dreams', '/docs', '/openapi.json', '/.env', '/_next/webpack-hmr', '/_next/static/a.js.map', '/api/v1/bridge/jobs/../../dreams']:
            assert client.get(path).status_code == 404
        assert not seen
        assert client.get('/bridge').status_code == 200
        assert seen[-1].url.port == 3000
        assert client.post('/mcp', json={}).status_code == 200
        assert seen[-1].url.port == 8000


def test_gateway_never_reuses_another_visitors_cookie():
    seen = []
    def upstream(request):
        seen.append(request.headers.get('cookie'))
        return httpx.Response(200, headers={'set-cookie': 'sonnic_guest=visitor-one; Path=/'}, text='ok')
    with TestClient(create_gateway(transport=httpx.MockTransport(upstream))) as client:
        client.post('/api/v1/sessions/guest', json={})
        client.cookies.clear()
        client.get('/api/v1/bridge/config')
        assert seen == ['', '']
        client.cookies.clear()
        client.get('/api/v1/bridge/config', headers={'cookie': 'sonnic_guest=visitor-two'})
        assert seen[-1] == 'sonnic_guest=visitor-two'


def test_gateway_limits_body_and_does_not_forward_untrusted_host():
    def upstream(request):
        assert request.headers['host'] == '127.0.0.1:8000'
        assert 'x-forwarded-host' not in request.headers
        return httpx.Response(401, headers={'www-authenticate': 'Bearer realm="lab"'})
    with TestClient(create_gateway(transport=httpx.MockTransport(upstream))) as client:
        assert client.get('/bridge', headers={'host':'evil.invalid'}).status_code == 400
        assert client.post('/mcp', content='a'*65537).status_code == 413
        response = client.post('/mcp', json={}, headers={'x-forwarded-host':'evil.invalid'})
        assert response.status_code == 401
        assert response.headers['www-authenticate'] == 'Bearer realm="lab"'


def test_upstream_failure_is_sanitized():
    def upstream(request):
        raise httpx.ConnectError('private details')
    with TestClient(create_gateway(transport=httpx.MockTransport(upstream))) as client:
        response = client.get('/bridge')
        assert response.status_code == 502
        assert 'private' not in response.text
