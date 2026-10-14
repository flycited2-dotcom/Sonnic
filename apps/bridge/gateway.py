"""Narrow loopback gateway for the synthetic ChatGPT connection test.

Point a reviewed HTTPS tunnel at this gateway, never at the full API or Next
development server. The lab launcher uses a separate database and production
Next assets. This module does not start a tunnel or grant external access.
"""
from contextlib import asynccontextmanager
import os
import re

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from .oauth import BridgeSettings

READ = {'GET', 'HEAD'}
JOB = r'/api/v1/bridge/jobs/[a-fA-F0-9-]{36}'
REQUEST_HEADERS = {
    'accept', 'authorization', 'content-type', 'cookie', 'origin',
    'sec-fetch-site', 'mcp-protocol-version', 'idempotency-key',
}
RESPONSE_HEADERS = {
    'content-type', 'location', 'www-authenticate', 'allow',
    'mcp-protocol-version', 'mcp-session-id',
}


def route_for(path: str, method: str):
    if method == 'POST' and path in {
        '/api/v1/bridge/worker/heartbeat', '/api/v1/bridge/worker/claim',
        '/api/v1/bridge/worker/renew', '/api/v1/bridge/worker/fail', '/api/v1/bridge/worker/submit',
    }:
        return 'api'
    if path in {'/bridge', '/bridge/connect', '/icon.svg'} and method in READ:
        return 'web'
    if (method in READ and re.fullmatch(r'/_next/static/[A-Za-z0-9_./@()\[\]-]+', path)
            and '..' not in path and not path.endswith('.map')):
        return 'web'
    if method in READ and path in {
        '/api/v1/bridge/config', '/.well-known/oauth-protected-resource',
        '/.well-known/oauth-authorization-server', '/oauth/authorize',
    }:
        return 'api'
    if path == '/mcp' and method in {'POST', 'GET', 'DELETE'}:
        return 'api'
    if method == 'GET' and re.fullmatch(JOB, path):
        return 'api'
    if method == 'DELETE' and re.fullmatch(JOB, path):
        return 'api'
    if method == 'POST' and (path in {
        '/api/v1/sessions/guest', '/api/v1/bridge/jobs', '/oauth/token',
        '/api/v1/bridge/oauth/prepare', '/api/v1/bridge/oauth/approve',
        '/api/v1/bridge/connections/revoke',
    } or re.fullmatch(JOB + r'/(review|cancel|retry)', path)):
        return 'api'
    return None


def create_gateway(origin: str | None = None, transport=None):
    settings = BridgeSettings(origin=origin or os.getenv('SONNIC_BRIDGE_ORIGIN', 'http://127.0.0.1:3010'),
        public_aliases=tuple(filter(None, os.getenv('SONNIC_BRIDGE_ALIASES', '').split(','))))

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False,
                                     timeout=35) as client:
            app.state.client = client
            yield

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.middleware('http')
    async def forward(request: Request, call_next):
        if request.url.hostname not in settings.allowed_hosts | {'127.0.0.1', 'localhost', 'testserver'}:
            return JSONResponse({'detail': 'host_not_allowed'}, status_code=400)
        path = request.url.path
        # Reject encoded separators/segments rather than normalizing to another route.
        raw = request.scope.get('raw_path', b'')
        if b'%' in raw or '\\' in path or '//' in path or '..' in path:
            return JSONResponse({'detail': 'route_not_available'}, status_code=404)
        if path == '/' and request.method in READ:
            return RedirectResponse('/bridge', status_code=303)
        target = route_for(path, request.method)
        if target is None:
            return JSONResponse({'detail': 'route_not_available'}, status_code=404)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 65536:
                return JSONResponse({'detail': 'body_too_large'}, status_code=413)
        headers = {k: v for k, v in request.headers.items() if k in REQUEST_HEADERS}
        # Each request carries only its own cookie. The pooled client's cookie jar
        # must never implicitly attach another visitor's session.
        headers.setdefault('cookie', '')
        port = 3000 if target == 'web' else 8000
        url = f'http://127.0.0.1:{port}{path}'
        if request.url.query:
            url += '?' + request.url.query
        try:
            upstream = await app.state.client.request(request.method, url, headers=headers, content=bytes(body))
        except httpx.HTTPError:
            return JSONResponse({'detail': 'lab_upstream_unavailable'}, status_code=502)
        response = Response(upstream.content, status_code=upstream.status_code,
                            headers={k: v for k, v in upstream.headers.items() if k in RESPONSE_HEADERS})
        for cookie in upstream.headers.get_list('set-cookie'):
            response.headers.append('set-cookie', cookie)
        response.headers.update({'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
                                 'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY',
                                 'Content-Security-Policy': "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; form-action 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'"})
        return response

    return app


app = create_gateway()
