"""Private IAM-authenticated Moodle polling endpoint for Cloud Scheduler.

The token exists only in this worker's Secret Manager-mounted file. Neither a
browser session nor a Gemini model is involved in scheduled synchronization.
"""
from contextlib import asynccontextmanager
import inspect
import json
import os
from pathlib import Path

from fastapi import FastAPI, Request
from starlette.responses import JSONResponse

from ..lms.client import MoodleClient
from ..lms.sync import MoodleSync
from ..security.context import AccessDenied
from .authority import ServiceIdentity


def _mounted_token():
    path = os.environ.get('CAMPUSPULSE_MOODLE_TOKEN_FILE')
    if not path:
        raise ValueError('moodle_token_mount_required')
    try:
        with Path(path).open('r', encoding='utf-8') as source:
            value = source.read(4097).strip()
        if not value or len(value) > 4096 or any(char.isspace() for char in value):
            raise ValueError()
        return value
    except (OSError, UnicodeError, ValueError):
        raise ValueError('moodle_token_mount_invalid') from None


def create_moodle_worker(settings, policy, documents, *, close):
    config = settings.moodle
    if config is None:
        raise ValueError('moodle_configuration_required')
    if policy.config != settings.application:
        raise ValueError('moodle_policy_configuration_mismatch')
    client = MoodleClient(config.site_url, _mounted_token(), config.expected_user_id)
    sync = MoodleSync(documents, owner_id=policy.config.student.trusted_user_id,
        site_url=config.site_url, expected_user_id=config.expected_user_id,
        fetch_snapshot=client.fetch, interval_minutes=config.interval_minutes,
        stale_after_minutes=config.stale_after_minutes)
    identities = tuple(ServiceIdentity(config.sync_origin, principal) for principal in (
        policy.config.identities.backend_service_account, config.scheduler_service_account))

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            result = close()
            if inspect.isawaitable(result):
                await result

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get('/healthz')
    async def health():
        return {'status': 'alive'}

    @app.post('/internal/moodle/sync')
    async def refresh(request: Request):
        try:
            for identity in identities:
                try:
                    await identity.verify(request)
                    break
                except AccessDenied:
                    pass
            else:
                raise AccessDenied('moodle_service_identity_required')
        except AccessDenied:
            return JSONResponse({'error': 'service_identity_rejected'}, status_code=403)
        try:
            body = b''
            async for chunk in request.stream():
                body += chunk
                if len(body) > 1024:
                    raise ValueError()
            data = json.loads(body) if body else {}
            if (not isinstance(data, dict) or set(data) - {'force'}
                    or type(data.get('force', False)) is not bool):
                raise ValueError()
        except (ValueError, UnicodeError):
            return JSONResponse({'error': 'invalid_sync_request'}, status_code=400)
        try:
            state = await sync.refresh(force=data.get('force', False))
            # A scheduler sees health/counts only, never coursework or grades.
            result = dict(status=state.status, stale=state.stale, syncing=state.syncing,
                last_attempt=state.last_attempt.isoformat() if state.last_attempt else None,
                last_success=state.last_success.isoformat() if state.last_success else None,
                task_count=len(state.snapshot.tasks) if state.snapshot else 0, error=state.error)
            return JSONResponse(result, status_code=503 if state.error and not state.syncing else 200)
        except Exception:
            return JSONResponse({'error': 'moodle_sync_unavailable'}, status_code=503)

    return app
