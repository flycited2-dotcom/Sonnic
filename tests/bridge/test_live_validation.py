"""Validation tooling must never turn a site polling failure into another model send."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from playwright.async_api import Error

from scripts import validate_worker_live as live
from scripts import validate_worker_once as control


@pytest.mark.parametrize('failures', [0, 1, 2, 3])
def test_site_snapshot_retries_are_bounded_and_redacted(monkeypatch, failures):
    response = SimpleNamespace(status=200, json=AsyncMock(return_value={'state': 'completed'}))
    get = AsyncMock(side_effect=[Error('cookie: secret-test-session')] * failures + [response])
    context = SimpleNamespace(request=SimpleNamespace(get=get))
    sleep = AsyncMock()
    monkeypatch.setattr(live, 'asyncio', SimpleNamespace(sleep=sleep))
    if failures == 3:
        with pytest.raises(RuntimeError, match='^validation_site_transport_failed$'):
            asyncio.run(live.read_snapshot(context, 'test-job'))
    else:
        assert asyncio.run(live.read_snapshot(context, 'test-job')) == {'state': 'completed'}
    assert get.await_count == min(failures + 1, 3)
    assert sleep.await_count == min(failures, 2)
    assert all(call.args[0].endswith('/jobs/test-job') for call in get.await_args_list)


def test_control_restores_stop_and_offline_on_browser_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(control, 'ROOT', tmp_path)
    monkeypatch.setenv('SONNIC_WORKER_CDP', 'http://127.0.0.1:9335')
    monkeypatch.setenv('SONNIC_WORKER_SERVER', 'https://sonnic.212.116.115.150.sslip.io')
    monkeypatch.setenv('SONNIC_WORKER_TOKEN', 'local-test-' + 'x'*32)
    folder = tmp_path / 'data/worker'
    folder.mkdir(parents=True)
    (folder / 'stop.requested').write_text('stopped')
    class Runtime:
        chromium = SimpleNamespace(connect_over_cdp=AsyncMock(side_effect=Error('cookie: secret-test-session')))
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
    monkeypatch.setattr(control, 'async_playwright', Runtime)
    requests = []
    def transport(request):
        requests.append(request)
        return httpx.Response(200, json={'ok': True})
    client_class = httpx.AsyncClient
    monkeypatch.setattr(control.httpx, 'AsyncClient', lambda **kw: client_class(**kw, transport=httpx.MockTransport(transport)))
    with pytest.raises(SystemExit) as error:
        asyncio.run(control.main(6))
    assert error.value.code == 2
    assert (folder / 'stop.requested').exists()
    assert len(requests) == 1 and requests[0].url.path.endswith('/heartbeat')
    assert b'offline' in requests[0].content
    assert 'secret-test-session' not in capsys.readouterr().out
