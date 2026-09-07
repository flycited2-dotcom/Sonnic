"""Owner-scoped beta site routes and stateless JSON Streamable HTTP MCP subset."""
import json
from urllib.parse import parse_qsl, urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import Field, StrictBool, ValidationError, model_validator

from apps.api.sonnic.models import DreamInput, Model
from .contracts import JobOutput
from .oauth import SCOPE
from .queue import BridgeError, Principal

VERSIONS = {'2025-03-26', '2025-06-18', '2025-11-25'}


class Enqueue(Model):
    case_id: str | None = Field(default=None, min_length=1, max_length=100)
    dream: DreamInput | None = None

    @model_validator(mode='after')
    def exactly_one_input(self):
        if (self.case_id is None) == (self.dream is None):
            raise ValueError('one_input_required')
        return self


class Review(Model):
    approve: StrictBool
    output_sha256: str = Field(pattern='^[a-f0-9]{64}$')


class ConsentRequest(Model):
    params: dict[str, str]


class ConsentDecision(Model):
    handle: str = Field(min_length=40, max_length=100)
    allow: StrictBool


class ClaimArgs(Model):
    job_id: UUID
    claim_key: UUID


def register_bridge(app, guest):
    router = APIRouter()

    def queue():
        return app.state.bridge_queue

    def oauth():
        return app.state.bridge_oauth

    def actor(sid, scope='site'):
        return Principal('browser:' + sid, sid, frozenset({scope}))

    def owner_active(sid):
        with app.state.store.db() as db:
            return db.execute('SELECT 1 FROM sessions WHERE id=? AND expires>?', (sid, queue().clock())).fetchone() is not None

    def call(function, *args):
        try:
            return function(*args)
        except BridgeError as error:
            code = str(error)
            status = 404 if code == 'job_not_found' else 403 if code == 'forbidden' else 429 if code in {'queue_full', 'consent_rate_limited'} else 409
            raise HTTPException(status, code) from None

    @router.get('/api/v1/bridge/config')
    def config():
        return {'synthetic_only': False, 'personal_input_enabled': True,
                'automatic_dispatch': False, 'chatgpt_connected': False,
                'mcp_url': oauth().settings.resource, 'oauth_configured': bool(oauth().settings.redirect_uri),
                'cases': [{'id': c['id'], 'dream_text': c['dream_text'], 'locale': c['locale']} for c in queue().cases.values()]}

    @router.post('/api/v1/bridge/jobs', status_code=201)
    def enqueue(body: Enqueue, sid=Depends(guest), idempotency_key: UUID = Header()):
        if body.dream is not None:
            job_id = call(queue().enqueue_dream, actor(sid), body.dream, idempotency_key)
        else:
            job_id = call(queue().enqueue, actor(sid), body.case_id, idempotency_key)
        return {'job_id': job_id, 'automatic_dispatch': False}

    @router.get('/api/v1/bridge/jobs/{job_id}')
    def snapshot(job_id: UUID, sid=Depends(guest)):
        return call(queue().snapshot_for_site, actor(sid), job_id)

    @router.post('/api/v1/bridge/jobs/{job_id}/review')
    def review(job_id: UUID, body: Review, sid=Depends(guest)):
        call(queue().review, actor(sid, 'review'), job_id, body.approve, body.output_sha256)
        return call(queue().snapshot_for_site, actor(sid), job_id)

    @router.post('/api/v1/bridge/jobs/{job_id}/retry')
    def retry(job_id: UUID, sid=Depends(guest)):
        call(queue().retry, actor(sid), job_id)
        return call(queue().snapshot_for_site, actor(sid), job_id)

    @router.post('/api/v1/bridge/jobs/{job_id}/cancel')
    def cancel(job_id: UUID, sid=Depends(guest)):
        call(queue().cancel, actor(sid), job_id)
        return call(queue().snapshot_for_site, actor(sid), job_id)

    @router.post('/api/v1/bridge/connections/revoke', status_code=204)
    def revoke(sid=Depends(guest)):
        oauth().revoke(sid)
        return Response(status_code=204)

    @router.get('/.well-known/oauth-protected-resource')
    def resource_metadata():
        return {'resource': oauth().settings.resource, 'authorization_servers': [oauth().settings.origin], 'scopes_supported': [SCOPE]}

    @router.get('/.well-known/oauth-authorization-server')
    def authorization_metadata():
        origin = oauth().settings.origin
        return {'issuer': origin, 'authorization_endpoint': origin+'/oauth/authorize',
                'token_endpoint': origin+'/oauth/token', 'response_types_supported': ['code'],
                'grant_types_supported': ['authorization_code'], 'code_challenge_methods_supported': ['S256'],
                'token_endpoint_auth_methods_supported': ['none'], 'scopes_supported': [SCOPE],
                'authorization_response_iss_parameter_supported': True}

    @router.get('/oauth/authorize')
    def authorize(request: Request):
        if len(request.query_params.multi_items()) != len(request.query_params):
            raise HTTPException(400, 'invalid_oauth_request')
        params = call(oauth().validate_request, dict(request.query_params))
        # On this first cross-site navigation a Strict cookie may be absent.
        # The same-origin consent page prepares a session-bound handle via fetch.
        return RedirectResponse(oauth().settings.origin+'/bridge/connect?'+urlencode(params), status_code=303)

    @router.post('/api/v1/bridge/oauth/prepare')
    def prepare(body: ConsentRequest, sid=Depends(guest)):
        handle = call(oauth().prepare, sid, body.params)
        return {'handle': handle, 'client_id': oauth().settings.client_id, 'scope': SCOPE,
                'redirect_uri': oauth().settings.redirect_uri, 'synthetic_only': True}

    @router.post('/api/v1/bridge/oauth/approve')
    def approve(body: ConsentDecision, sid=Depends(guest)):
        return {'redirect': call(oauth().approve, sid, body.handle, body.allow)}

    @router.post('/oauth/token')
    async def token(request: Request):
        if request.headers.get('content-type', '').split(';')[0] != 'application/x-www-form-urlencoded':
            return JSONResponse({'error': 'invalid_request'}, status_code=400)
        try:
            pairs = parse_qsl((await request.body()).decode('utf-8'), keep_blank_values=True, strict_parsing=True, max_num_fields=10)
            if len(dict(pairs)) != len(pairs):
                raise ValueError()
            return oauth().exchange(dict(pairs), owner_active)
        except (ValueError, BridgeError):
            return JSONResponse({'error': 'invalid_grant'}, status_code=400)

    def authenticated(request):
        header = request.headers.get('authorization', '')
        try:
            if not header.startswith('Bearer ') or len(header) > 200:
                raise BridgeError('invalid_token')
            return oauth().authenticate(header[7:], owner_active)
        except BridgeError:
            raise HTTPException(401, 'invalid_token', headers={'WWW-Authenticate':
                f'Bearer resource_metadata="{oauth().settings.origin}/.well-known/oauth-protected-resource", scope="{SCOPE}"'}) from None

    def rpc_error(identifier, code, message):
        return {'jsonrpc': '2.0', 'id': identifier, 'error': {'code': code, 'message': message}}

    def tool(name, description, schema):
        return {'name': name, 'description': description, 'inputSchema': schema,
                'annotations': {'readOnlyHint': False, 'destructiveHint': False, 'idempotentHint': True, 'openWorldHint': False},
                'securitySchemes': [{'type': 'oauth2', 'scopes': [SCOPE]}]}

    @router.api_route('/mcp', methods=['GET', 'DELETE'])
    def no_stream(request: Request):
        authenticated(request)
        return Response(status_code=405, headers={'Allow': 'POST'})

    @router.post('/mcp')
    async def mcp(request: Request):
        principal = authenticated(request)
        if request.headers.get('mcp-protocol-version', '2025-03-26') not in VERSIONS:
            raise HTTPException(400, 'unsupported_protocol_version')
        accepted = {p.split(';')[0].strip() for p in request.headers.get('accept', '').split(',')}
        if not {'application/json', 'text/event-stream'} <= accepted:
            raise HTTPException(406, 'mcp_accept_required')
        if request.headers.get('content-type', '').split(';')[0] != 'application/json':
            raise HTTPException(415, 'json_required')
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            return JSONResponse(rpc_error(None, -32700, 'Parse error'), status_code=400)
        if (not isinstance(body, dict) or body.get('jsonrpc') != '2.0'
                or not isinstance(body.get('method'), str) or set(body)-{'jsonrpc', 'id', 'method', 'params'}
                or ('id' in body and (isinstance(body['id'], bool) or not isinstance(body['id'], (str, int))))):
            return JSONResponse(rpc_error(None, -32600, 'Invalid request'), status_code=400)
        method, identifier, params = body['method'], body.get('id'), body.get('params', {})
        if not isinstance(params, dict):
            return rpc_error(identifier, -32602, 'Invalid params')
        if 'id' not in body:
            if method in {'notifications/initialized', 'notifications/cancelled'}:
                return Response(status_code=202)
            return Response(status_code=400)
        if method == 'initialize':
            requested = params.get('protocolVersion')
            if not isinstance(requested, str) or not requested:
                return rpc_error(identifier, -32602, 'Invalid params')
            result = {'protocolVersion': requested if requested in VERSIONS else '2025-11-25',
                      'capabilities': {'tools': {}}, 'serverInfo': {'name': 'sonnic-synthetic-bridge', 'version': '0.4.0'},
                'instructions': 'Owner-scoped beta jobs may contain personal dream text. Never claim publication: submit_result returns awaiting_review. A website operator must review. No arbitrary URLs or owner arguments.'}
        elif method == 'ping':
            result = {}
        elif method == 'tools/list':
            result = {'tools': [
                tool('claim_job', 'Lease the current owner\'s exact job using its ID and stable claim key. The payload may contain personal dream text; do not retain it outside this job.', ClaimArgs.model_json_schema()),
                tool('submit_result', 'Submit the correlated JSON candidate for separate website review; this does not publish it.', JobOutput.model_json_schema())]}
        elif method == 'tools/call':
            if set(params)-{'name', 'arguments', '_meta'}:
                return rpc_error(identifier, -32602, 'Invalid params')
            try:
                if params.get('name') == 'claim_job':
                    args = ClaimArgs.model_validate(params.get('arguments', {}))
                    payload = queue().claim(principal, args.job_id, args.claim_key).model_dump(mode='json')
                elif params.get('name') == 'submit_result':
                    output = JobOutput.model_validate(params.get('arguments', {}))
                    payload = {'state': queue().submit(principal, output), 'published': False}
                else:
                    return rpc_error(identifier, -32602, 'Unknown tool')
                result = {'content': [{'type': 'text', 'text': json.dumps(payload, ensure_ascii=False)}],
                          'structuredContent': payload, 'isError': False}
            except ValidationError:
                result = {'content': [{'type': 'text', 'text': 'invalid_tool_arguments'}], 'isError': True}
            except BridgeError as error:
                result = {'content': [{'type': 'text', 'text': str(error)}], 'isError': True}
        else:
            return rpc_error(identifier, -32601, 'Method not found')
        return {'jsonrpc': '2.0', 'id': identifier, 'result': result}

    app.include_router(router)
