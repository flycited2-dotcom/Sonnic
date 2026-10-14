import asyncio
import json
from uuid import uuid4

import httpx
import pytest

from scripts import validate_multi_visitor_live as live


@pytest.mark.parametrize('failed', [False, True])
def test_live_validator_never_reenqueues_and_checks_foreign_access(tmp_path, monkeypatch, failed):
    monkeypatch.setattr(live, 'ROOT', tmp_path)
    jobs, counts = {}, {'enqueue': 0, 'foreign': 0}
    def handler(request):
        path = request.url.path
        if path.endswith('/config'):
            return httpx.Response(200, json={'chatgpt_connected': True, 'synthetic_only': True})
        if path.endswith('/guest'):
            return httpx.Response(200, headers={'set-cookie': f'visitor={uuid4()}; Secure; Path=/'}, json={})
        owner = request.headers['cookie']
        if path.endswith('/jobs'):
            counts['enqueue'] += 1
            body = json.loads(request.content)
            job = str(uuid4())
            jobs[job] = (owner, body['dream']['dream_text'], body['dream']['locale'])
            return httpx.Response(201, json={'job_id': job})
        saved_owner, dream, locale = jobs[path.rsplit('/', 1)[1]]
        if saved_owner != owner:
            counts['foreign'] += 1
            return httpx.Response(404, json={'detail': 'job_not_found'})
        return httpx.Response(200, json={'dream_text': dream, 'state': 'failed' if failed else 'completed',
                                        'result': {'locale': locale, 'reflection': None}})
    client = httpx.AsyncClient
    monkeypatch.setattr(live.httpx, 'AsyncClient', lambda **kw: client(**(kw | {'transport': httpx.MockTransport(handler)})))
    assert asyncio.run(live.run()) is (not failed)
    assert counts['enqueue'] == 4
    assert counts['foreign'] == 12
    report = json.loads(next(tmp_path.rglob('report.json')).read_text())
    assert report['status'] == ('failed' if failed else 'completed')
    assert report['public_ui_verified'] is False


def test_ssh_transport_retains_secure_cookie_scope():
    def handler(request):
        assert str(request.url).startswith('http://127.0.0.1:3181/api/')
        assert request.headers['host'] == httpx.URL(live.ORIGIN).host
        if request.url.path.endswith('/guest'):
            return httpx.Response(200, headers={'set-cookie': 'visitor=synthetic; Secure; Path=/'})
        assert request.headers['cookie'] == 'visitor=synthetic'
        return httpx.Response(200)
    async def exercise():
        transport = live.SshTransport()
        transport.inner = httpx.MockTransport(handler)
        async with httpx.AsyncClient(base_url=live.ORIGIN, transport=transport) as client:
            await client.post('/api/v1/sessions/guest')
            assert (await client.get('/api/v1/bridge/config')).status_code == 200
            with pytest.raises(ValueError, match='unexpected_validation_destination'):
                await client.get('https://unrelated.invalid/api/config')
    asyncio.run(exercise())
