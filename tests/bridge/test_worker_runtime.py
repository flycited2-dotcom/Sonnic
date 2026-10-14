"""Run the actual top-level worker against local fakes; network is intercepted."""
import asyncio
import json
import ssl
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from apps.worker import main as module
from apps.worker.main import Journal

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = json.loads((ROOT / 'chatgpt-project/examples/blue-parrot.output.json').read_text(encoding='utf-8'))


class Playwright:
    def __init__(self):
        self.chromium = SimpleNamespace(connect_over_cdp=AsyncMock(side_effect=AssertionError('Browser must not be contacted')))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


@pytest.mark.parametrize('markers,phase', [
    ('retrieving webSocket url from private-text-not-to-log', 'http_discovery'),
    ('<ws connecting> private-text-not-to-log', 'websocket_handshake'),
    ('<ws connecting> private-text-not-to-log\n<ws connected> private-text-not-to-log', 'target_initialization'),
    ('private-text-not-to-log', 'unknown'),
])
def test_probe_cdp_timeout_reports_only_fixed_phase_without_server_or_model_requests(tmp_path, monkeypatch, capsys, markers, phase):
    import playwright.async_api
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    monkeypatch.setenv('SONNIC_WORKER_SERVER', 'https://testserver')
    monkeypatch.setenv('SONNIC_WORKER_CDP', 'http://127.0.0.1:9335')
    runtime = Playwright()
    runtime.chromium.connect_over_cdp = AsyncMock(side_effect=playwright.async_api.TimeoutError(markers))
    monkeypatch.setattr(playwright.async_api, 'async_playwright', lambda: runtime)
    def transport(request):
        raise AssertionError('Readiness probe must not claim or send server requests')
    client_class = httpx.AsyncClient
    monkeypatch.setattr(module.httpx, 'AsyncClient', lambda **kw: client_class(**kw, transport=httpx.MockTransport(transport)))
    with pytest.raises(SystemExit) as caught:
        asyncio.run(module.run(probe=True))
    assert caught.value.code == 2
    runtime.chromium.connect_over_cdp.assert_awaited_once_with('http://127.0.0.1:9335', timeout=10000)
    diagnostic = json.loads((tmp_path / 'data/worker/pause-diagnostic.json').read_text())
    assert diagnostic['cdp']['phase'] == phase and diagnostic['cdp']['error_type'] == 'TimeoutError'
    assert diagnostic['cdp']['elapsed_seconds'] >= 0
    assert 'private-text-not-to-log' not in json.dumps(diagnostic) + capsys.readouterr().out


@pytest.mark.parametrize('server_state', ['completed', 'cancelled'])
def test_durable_delivery_runs_without_browser(tmp_path, monkeypatch, server_state):
    import playwright.async_api
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    monkeypatch.setenv('SONNIC_WORKER_TOKEN', 'local-test-' + 'x'*32)
    monkeypatch.setenv('SONNIC_WORKER_SERVER', 'https://testserver')
    monkeypatch.setenv('SONNIC_WORKER_CDP', 'http://127.0.0.1:9335')
    journal = Journal(tmp_path / 'data/worker')
    journal.save({'stage': 'delivery', 'packet': OUTPUT, 'output': OUTPUT})
    runtime = Playwright()
    monkeypatch.setattr(playwright.async_api, 'async_playwright', lambda: runtime)
    requests = []
    def transport(request):
        requests.append(request.url.path)
        assert request.url.host == 'testserver'
        assert request.url.path.endswith('/submit')
        assert json.loads(request.content) == OUTPUT
        return httpx.Response(200 if server_state == 'completed' else 409,
                              json={'state': 'completed'} if server_state == 'completed' else {'detail': 'stale_or_mismatched_result'})
    client_class = httpx.AsyncClient
    monkeypatch.setattr(module.httpx, 'AsyncClient', lambda **kw: client_class(**kw, transport=httpx.MockTransport(transport)))
    asyncio.run(module.run(once=True))
    runtime.chromium.connect_over_cdp.assert_not_awaited()
    assert requests == ['/api/v1/bridge/worker/submit']
    assert journal.read() is None
    assert ('completed' if server_state == 'completed' else 'delivery_rejected') in (journal.folder / 'receipts.jsonl').read_text()


@pytest.mark.parametrize('marker', ['stop.requested', 'pause.requested'])
def test_persistent_stop_prevents_browser_and_job_requests(tmp_path, monkeypatch, marker):
    import playwright.async_api
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    monkeypatch.setenv('SONNIC_WORKER_TOKEN', 'local-test-' + 'x'*32)
    monkeypatch.setenv('SONNIC_WORKER_SERVER', 'https://testserver')
    monkeypatch.setenv('SONNIC_WORKER_CDP', 'http://127.0.0.1:9335')
    folder = Journal(tmp_path / 'data/worker').folder
    (folder / marker).write_text('explicit pause')
    runtime = Playwright()
    monkeypatch.setattr(playwright.async_api, 'async_playwright', lambda: runtime)
    def transport(request):
        assert request.url.host == 'testserver'
        assert request.url.path.endswith('/heartbeat') and json.loads(request.content) == {'state': 'offline'}
        return httpx.Response(200, json={'ok': True})
    client_class = httpx.AsyncClient
    monkeypatch.setattr(module.httpx, 'AsyncClient', lambda **kw: client_class(**kw, transport=httpx.MockTransport(transport)))
    asyncio.run(module.run(once=True))
    runtime.chromium.connect_over_cdp.assert_not_awaited()
    assert (folder / marker).exists()


@pytest.mark.parametrize('failures', [1,5])
@pytest.mark.parametrize('error_type', ['SSLError', 'ReadTimeout', 'HTTPStatusError'])
def test_network_error_preserves_delivery_and_backs_off_without_model_call(tmp_path, monkeypatch, capsys, failures, error_type):
    import playwright.async_api
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    monkeypatch.setenv('SONNIC_WORKER_TOKEN', 'local-test-' + 'x'*32)
    monkeypatch.setenv('SONNIC_WORKER_SERVER', 'https://testserver')
    monkeypatch.setenv('SONNIC_WORKER_CDP', 'http://127.0.0.1:9335')
    journal = Journal(tmp_path / 'data/worker')
    pending = {'stage': 'delivery', 'packet': OUTPUT, 'output': OUTPUT}
    journal.save(pending)
    runtime = Playwright()
    monkeypatch.setattr(playwright.async_api, 'async_playwright', lambda: runtime)
    requests = []
    def transport(request):
        requests.append(request.url.path)
        # Also fail the final offline heartbeat: shutdown must still succeed.
        if error_type == 'SSLError':
            raise ssl.SSLError('record layer failure private-text-not-to-log')
        if error_type == 'ReadTimeout':
            raise httpx.ReadTimeout('private-text-not-to-log', request=request)
        raise httpx.HTTPStatusError('private-text-not-to-log', request=request,
                                    response=httpx.Response(503, text='private-text-not-to-log'))
    client_class = httpx.AsyncClient
    monkeypatch.setattr(module.httpx, 'AsyncClient', lambda **kw: client_class(**kw, transport=httpx.MockTransport(transport)))
    delays = []
    async def backoff(seconds):
        delays.append(seconds)
        if len(delays) == failures:
            (journal.folder / 'stop.requested').write_text('operator stop')
    monkeypatch.setattr(module.asyncio, 'sleep', backoff)
    asyncio.run(module.run())
    assert delays == [5,10,20,30,30][:failures]
    assert requests == ['/api/v1/bridge/worker/submit'] * failures + ['/api/v1/bridge/worker/heartbeat']
    assert journal.read() == pending
    assert not (journal.folder / 'pause.requested').exists()
    runtime.chromium.connect_over_cdp.assert_not_awaited()
    log = capsys.readouterr().out
    assert 'worker_state:server_unavailable' in log
    assert 'private-text-not-to-log' not in log and 'local-test-' not in log
    diagnostics = [json.loads(line.removeprefix('server_retry:')) for line in log.splitlines() if line.startswith('server_retry:')]
    assert [entry['retry_seconds'] for entry in diagnostics] == delays
    assert all(entry['error_type'] == error_type and entry['time'] > 0 for entry in diagnostics)
    assert all(entry['status_code'] == (503 if error_type == 'HTTPStatusError' else None) for entry in diagnostics)
