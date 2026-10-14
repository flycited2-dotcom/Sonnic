import json
import os
from pathlib import Path
from contextlib import asynccontextmanager
from uuid import UUID

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .models import DreamInput, GuestInput
from .orchestrator import Orchestrator, Provider
from .store import Store
from .real_provider import AISettings, OpenAIProvider
from apps.bridge.queue import Queue
from apps.bridge.oauth import BridgeSettings, LabOAuth
from apps.bridge.http import register_bridge
from apps.bridge.dispatch import DispatchSettings, register_dispatch

load_dotenv(Path(__file__).resolve().parents[3] / ".env")
COOKIE = "sonnic_guest"
ALLOWED_ORIGINS = {"http://127.0.0.1:3000", "http://localhost:3000", "http://127.0.0.1:8000"}


def create_app(db_path: str | None = None, provider: Provider | None = None, mode: str | None = None,
               bridge_settings: BridgeSettings | None = None, dispatch_settings: DispatchSettings | None = None):
    dispatch = dispatch_settings or DispatchSettings.from_env()
    bridge = bridge_settings or BridgeSettings(origin=os.getenv('SONNIC_BRIDGE_ORIGIN', 'http://127.0.0.1:3000'),
        client_id=os.getenv('SONNIC_BRIDGE_CLIENT_ID', 'sonnic-chatgpt'),
        redirect_uri=os.getenv('SONNIC_BRIDGE_REDIRECT_URI') or None,
        public_aliases=tuple(filter(None, os.getenv('SONNIC_BRIDGE_ALIASES', '').split(','))))
    ai_mode = mode or os.getenv("AI_MODE", "demo")
    if ai_mode not in {"demo", "disabled", "real"}:
        raise RuntimeError("AI_MODE must be demo, real or disabled")
    selected_provider = provider
    if ai_mode == "real" and selected_provider is None:
        selected_provider = OpenAIProvider(AISettings.from_env())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = Store(db_path or os.getenv("SONNIC_DB_PATH", "data/sonnic.sqlite3"))
        store.recover()
        app.state.store = store
        app.state.dispatch_settings = dispatch
        app.state.bridge_queue = Queue(Path(str(store.path) + '.bridge.sqlite3'), job_ttl=600 if dispatch.enabled else 3600)
        app.state.bridge_oauth = LabOAuth(app.state.bridge_queue, bridge)
        app.state.orchestrator = Orchestrator(store, selected_provider)
        yield
        await app.state.orchestrator.shutdown()

    app = FastAPI(title="Sonnic local API", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        origin = request.headers.get("origin")
        bridge_path = request.url.path.startswith(('/api/v1/bridge', '/oauth/', '/.well-known/')) or request.url.path == '/mcp'
        if bridge_path and request.url.hostname not in bridge.allowed_hosts | {'127.0.0.1', 'localhost', 'testserver'}:
            return JSONResponse({'detail': 'host_not_allowed'}, status_code=400)
        machine_path = request.url.path in {'/mcp', '/oauth/token'}
        permitted_origins = ALLOWED_ORIGINS | bridge.allowed_origins | ({'https://chatgpt.com'} if machine_path else set())
        if bridge_path and origin and origin not in permitted_origins:
            return JSONResponse({'detail': 'origin_not_allowed'}, status_code=403)
        if request.method not in {"GET", "HEAD", "OPTIONS"} and (
            (origin and origin not in permitted_origins) or (not machine_path and request.headers.get("sec-fetch-site") == "cross-site")
        ):
            return JSONResponse({"detail": "origin_not_allowed"}, status_code=403)
        # A streaming bound also handles missing/false Content-Length.
        if request.method in {"POST", "PUT", "PATCH"}:
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 65536:
                    return JSONResponse({"detail": "body_too_large"}, status_code=413)
            request._body = bytes(body)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        if bridge_path:
            response.headers['Referrer-Policy'] = 'no-referrer'
            response.headers['X-Frame-Options'] = 'DENY'
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request: Request, exc: RequestValidationError):
        # FastAPI's default 422 includes the rejected raw input. Omit it.
        return JSONResponse({"detail": "invalid_input", "fields": [".".join(str(p) for p in e["loc"]) for e in exc.errors()]}, status_code=422)

    def guest(request: Request):
        sid = request.app.state.store.session(request.cookies.get(COOKIE))
        if not sid:
            raise HTTPException(401, "session_required")
        return sid

    def owned(did: UUID, sid: str):
        row = app.state.store.get(str(did), sid)
        if not row:
            raise HTTPException(404, "dream_not_found")
        return row

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready():
        app.state.store.ready()
        return {"status": "ok", "ai_mode": ai_mode, "real_ai_available": ai_mode == "real", "provider_connection_verified": False}

    @app.get("/api/v1/config")
    def config():
        return {"ai_mode": ai_mode, "local_only": True, "research_retrieval_used": False, "supported_locales": ["ru-RU", "en-US"]}

    @app.post("/api/v1/sessions/guest")
    def create_guest(body: GuestInput, request: Request, response: Response):
        sid = app.state.store.session(request.cookies.get(COOKIE))
        if not sid:
            sid, token = app.state.store.guest(body)
            response.set_cookie(COOKIE, token, httponly=True, secure=bridge.origin.startswith('https:'), samesite="strict", max_age=86400, path="/")
        return {"guest_session_id": sid, "ai_mode": ai_mode}

    @app.post("/api/v1/dreams", status_code=201)
    async def create_dream(body: DreamInput, sid: str = Depends(guest), idempotency_key: UUID = Header()):
        if ai_mode == "disabled":
            raise HTTPException(503, "provider_not_configured")
        try:
            did, created = app.state.store.create(sid, body, str(idempotency_key))
        except ValueError as error:
            code = str(error)
            raise HTTPException(429 if code == "rate_limited" else 409, code) from None
        if created:
            app.state.orchestrator.enqueue(did, body)
        row = app.state.store.get(did, sid)
        return {"dream_id": did, "status": "accepted", "free_interpretation_status": row["status"], "mode": ai_mode}

    @app.get("/api/v1/dreams/{did}/free")
    def free(did: UUID, sid: str = Depends(guest)):
        row = owned(did, sid)
        return {"dream_id": str(did), "status": row["status"], "stage": row["stage"],
                "result": json.loads(row["result"]) if row["result"] else None,
                "error": row["error"], "can_retry": row["status"] == "failed" and row["attempt"] < 3 and row["error"] not in {"safety_support", "safety_refused", "provider_refused", "provider_auth"}}

    @app.post("/api/v1/dreams/{did}/free", status_code=202)
    async def retry(did: UUID, sid: str = Depends(guest)):
        if ai_mode == "disabled":
            raise HTTPException(503, "provider_not_configured")
        row = owned(did, sid)
        if row["status"] in {"completed", "queued", "processing"}:
            return {"status": row["status"]}
        if row["error"] in {"safety_support", "safety_refused", "provider_refused", "provider_auth"}:
            raise HTTPException(409, "retry_exhausted")
        if not app.state.store.retry(str(did), sid):
            raise HTTPException(409, "retry_exhausted")
        app.state.orchestrator.enqueue(str(did), DreamInput.model_validate_json(row["input"]))
        return {"status": "queued"}

    @app.post("/api/v1/dreams/{did}/viewed", status_code=204)
    def viewed(did: UUID, sid: str = Depends(guest)):
        row = owned(did, sid)
        if row["status"] != "completed":
            raise HTTPException(409, "result_not_ready")
        app.state.store.viewed(str(did), sid)
        return Response(status_code=204)

    @app.delete("/api/v1/dreams/{did}", status_code=204)
    async def delete(did: UUID, sid: str = Depends(guest)):
        owned(did, sid)
        await app.state.orchestrator.cancel(str(did))
        app.state.store.delete(str(did), sid)
        return Response(status_code=204)

    register_bridge(app, guest)
    register_dispatch(app)
    return app


app = create_app()
