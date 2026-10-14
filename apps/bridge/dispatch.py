"""Restricted PC-to-VPS transport, independent of visitor cookies and MCP OAuth."""
from dataclasses import dataclass
import hmac
import os
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field

from apps.api.sonnic.models import Model
from .contracts import JobOutput
from .queue import BridgeError, Principal

WorkerState = Literal['ready', 'busy', 'login_required', 'browser_unavailable', 'model_limit', 'page_changed', 'offline']
Failure = Literal['login_required', 'browser_unavailable', 'model_limit', 'page_changed',
                  'generation_timeout', 'invalid_model_output', 'submission_uncertain']


@dataclass(frozen=True)
class DispatchSettings:
    token: str = ''
    enabled: bool = False
    allow_personal: bool = False

    def __post_init__(self):
        if self.enabled and len(self.token) < 32:
            raise ValueError('SONNIC_WORKER_TOKEN must contain at least 32 characters')

    @classmethod
    def from_env(cls):
        return cls(os.getenv('SONNIC_WORKER_TOKEN', ''), os.getenv('SONNIC_AUTOMATIC_DISPATCH') == 'true',
                   os.getenv('SONNIC_WORKER_ALLOW_PERSONAL') == 'true')


class NextClaim(Model):
    claim_key: UUID


class Lease(Model):
    job_id: UUID
    lease_id: UUID
    attempt: int = Field(ge=1, le=3)


class Failed(Lease):
    error: Failure


class Heartbeat(Model):
    state: WorkerState


def register_dispatch(app):
    router = APIRouter(prefix='/api/v1/bridge/worker')

    def service(request: Request):
        settings = app.state.dispatch_settings
        if not settings.enabled:
            raise HTTPException(503, 'dispatch_disabled')
        supplied = request.headers.get('authorization', '')
        if len(supplied) > 512 or not hmac.compare_digest(supplied.encode(), ('Bearer ' + settings.token).encode()):
            raise HTTPException(401, 'invalid_worker_token')
        return Principal('sonnic-dispatcher', 'service', frozenset({'dispatch'}))

    def call(fn, *args):
        try:
            return fn(*args)
        except BridgeError as exc:
            raise HTTPException(409, str(exc)) from None

    @router.post('/heartbeat')
    def heartbeat(body: Heartbeat, actor=Depends(service)):
        call(app.state.bridge_queue.heartbeat, actor, body.state)
        return {'ok': True}

    @router.post('/claim')
    def claim(body: NextClaim, actor=Depends(service)):
        job = call(app.state.bridge_queue.claim_next, actor, body.claim_key, app.state.dispatch_settings.allow_personal)
        return {'job': job.model_dump(mode='json') if job else None}

    @router.post('/renew')
    def renew(body: Lease, actor=Depends(service)):
        return call(app.state.bridge_queue.renew, actor, body.job_id, body.lease_id, body.attempt)

    @router.post('/fail')
    def fail(body: Failed, actor=Depends(service)):
        call(app.state.bridge_queue.fail_dispatch, actor, body.job_id, body.lease_id, body.attempt, body.error)
        return {'state': 'failed'}

    @router.post('/submit')
    def submit(body: JobOutput, actor=Depends(service)):
        return {'state': call(app.state.bridge_queue.submit_dispatch, actor, body)}

    app.include_router(router)
