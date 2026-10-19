"""Owner-bound Moodle views. Browser refresh has no account/token selectors."""
from collections.abc import Awaitable, Callable

from fastapi import Request
from fastapi.responses import JSONResponse

from ..domain.models import Contract
from ..security.context import AccessDenied
from .app import body


class RefreshMoodle(Contract):
    """Only a refresh intent; account and site come from trusted configuration."""


class LocalTaskStatus(Contract):
    task_id: str
    done: bool


def install_moodle_routes(app, *, auth, reader: Callable[..., Awaitable],
                          refresh: Callable[..., Awaitable], source_kind='rest', update_local_status=None):
    """Install with this app's WebAuth and server-owned async context adapters.

    Reader returns MoodleState. Refresher may coalesce overlapping requests;
    its return value is ignored in favour of a freshly read durable state.
    """
    if source_kind not in ('rest', 'calendar_feed'):
        raise ValueError('unsupported_moodle_source')
    async def serve(request, *, update=False):
        credential = await auth.load(request, mutation=update)
        if request.query_params:
            raise ValueError("moodle_selectors_forbidden")
        if update:
            await body(request, RefreshMoodle)
        with auth.request_scope(credential) as context:
            if update:
                await refresh(context)
            if source_kind == 'calendar_feed':
                from ..lms.calendar_feed import FeedState
                state = FeedState.model_validate(await reader(context))
            else:
                from ..lms.sync import MoodleState
                state = MoodleState.model_validate(await reader(context))
            auth.policy.require_owner(context, state.owner_id)
            if state.snapshot is not None and (state.snapshot.site_url, state.snapshot.user_id) != (
                    state.site_url, state.expected_user_id):
                raise AccessDenied("moodle_account_binding_mismatch")
            latest = await auth.load(request)
            if latest.session != credential.session:
                raise AccessDenied("session_changed")
            auth.policy.require_context(context)
            # Identity bindings and tokens never become display data. Snapshot
            # user_id is likewise not needed to display the student's records.
            snapshot = None if not state.connected or state.snapshot is None else state.snapshot.model_dump(
                mode="json", exclude={"user_id"})
            return JSONResponse({
                'source_kind': source_kind,
                "connected": state.connected, "stale": state.stale,
                "syncing": state.syncing, "last_attempt": None if state.last_attempt is None
                else state.last_attempt.isoformat(),
                "last_success": None if state.last_success is None else state.last_success.isoformat(),
                "error": state.error, "snapshot": snapshot,
            })

    @app.get("/api/moodle")
    async def read_moodle(request: Request):
        return await serve(request)

    @app.post("/api/moodle/sync")
    async def refresh_moodle(request: Request):
        return await serve(request, update=True)

    if source_kind == 'calendar_feed' and update_local_status is not None:
        @app.post('/api/moodle/task-status')
        async def task_status(request: Request):
            credential = await auth.load(request, mutation=True)
            if request.query_params:
                raise ValueError('moodle_selectors_forbidden')
            value = await body(request, LocalTaskStatus)
            if not value.task_id or len(value.task_id) > 512:
                raise ValueError('invalid_calendar_event_id')
            with auth.request_scope(credential) as context:
                await update_local_status(context, value.task_id, value.done)
            return await serve(request)
