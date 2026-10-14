"""Domain migration keeps explicit host/CSRF boundaries and old guest sessions."""
import httpx
import pytest
from fastapi.testclient import TestClient

from apps.api.sonnic.main import create_app
from apps.bridge.gateway import create_gateway
from apps.bridge.oauth import BridgeSettings

PRIMARY = 'https://sonrazbor.ru'
PREVIOUS = 'https://sonnic.212.116.115.150.sslip.io'


@pytest.mark.parametrize('alias', [
    'http://external.example', 'https://*.example', 'https://example.com/path',
    'https://example.com?x=1', 'https://example.com#x', 'https://user@example.com',
    'https://example.com:443', 'https://example.com\\evil', ' https://example.com',
])
def test_aliases_cannot_expand_into_wildcards_or_ambiguous_origins(alias):
    with pytest.raises(ValueError, match='invalid_bridge_alias'):
        BridgeSettings(origin=PRIMARY, public_aliases=(alias,))


def test_aliases_preserve_canonical_oauth_resource():
    settings = BridgeSettings(origin=PRIMARY, public_aliases=(PREVIOUS,))
    assert settings.resource == PRIMARY + '/mcp'
    assert settings.allowed_origins == {PRIMARY, PREVIOUS}
    assert settings.allowed_hosts == {'sonrazbor.ru', 'sonnic.212.116.115.150.sslip.io'}


def test_api_accepts_explicit_domains_but_rejects_unknown_origins_and_cross_site_posts(tmp_path):
    settings = BridgeSettings(origin=PRIMARY, public_aliases=(PREVIOUS,))
    with TestClient(create_app(db_path=str(tmp_path / 'main.sqlite3'), mode='disabled',
                              bridge_settings=settings)) as client:
        for origin in (PRIMARY, PREVIOUS):
            host = origin.removeprefix('https://')
            assert client.get('/api/v1/bridge/config', headers={'host': host}).status_code == 200
            assert client.post('/api/v1/sessions/guest', json={}, headers={
                'host': host, 'origin': origin, 'sec-fetch-site': 'same-origin'}).status_code == 200
        assert client.get('/api/v1/bridge/config', headers={'host': 'evil.example'}).status_code == 400
        assert client.post('/api/v1/sessions/guest', json={}, headers={
            'origin': 'https://evil.example'}).status_code == 403
        assert client.post('/api/v1/sessions/guest', json={}, headers={
            'origin': PREVIOUS, 'sec-fetch-site': 'cross-site'}).status_code == 403


def test_gateway_aliases_are_explicit_and_never_forward_an_external_host(monkeypatch):
    monkeypatch.setenv('SONNIC_BRIDGE_ALIASES', PREVIOUS)
    seen = []
    def upstream(request):
        seen.append(request.headers['host'])
        return httpx.Response(200, text='ok')
    with TestClient(create_gateway(origin=PRIMARY, transport=httpx.MockTransport(upstream))) as client:
        for host in ('sonrazbor.ru', 'sonnic.212.116.115.150.sslip.io'):
            assert client.get('/bridge', headers={'host': host}).status_code == 200
        assert client.get('/bridge', headers={'host': 'evil.example'}).status_code == 400
        assert seen == ['127.0.0.1:3000', '127.0.0.1:3000']
