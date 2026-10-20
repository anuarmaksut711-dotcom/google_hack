"""Explicit composition of original T16 identity and T24 durable run APIs.

No closure inspection or replacement auth. Separate WebAuth instances use the
same explicit dependencies and durable sessions; capabilities remain local.
Production runtime and safe source projections must be supplied by the operator.
"""
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Protocol

from fastapi import Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import Field, StringConstraints

from ...domain.models import Contract, PlanVersion, VerificationResult
from ...storage.plans import FirestorePlanStore, plan_reference
from ..app import create_app as create_identity_app
from ..approval import FirestoreApprovalStore
from ..auth import WebAuth
from ..chat import create_chat_router
from ..speech import create_speech_router
from ..runs import RecoveryRuns, RunView, create_runs_router
from ..assets.client import SCRIPT
from ..assets.styles import STYLES
from .render import render_panel, render_shell


DisplayLine = Annotated[str, StringConstraints(strict=True, max_length=500)]
DisplayLines = Annotated[tuple[DisplayLine, ...], Field(max_length=100)]


class SafeSources(Contract):
    """Explicitly curated display text, never raw Sheets/Calendar payloads.

    The injected adapter must authorize sources against the supplied context.
    Text is still untrusted and always HTML escaped. No fixture fallback.
    """
    assignments: DisplayLines | None = None
    calendar: DisplayLines | None = None
    verification: VerificationResult | None = None


class SafeProjection(Protocol):
    async def project(self, context, *, run: RunView | None,
                      plan: PlanVersion | None) -> SafeSources: ...


def create_ui_app(*, policy, documents, verifier, origin: str, environment: str,
                  clock=lambda: datetime.now(UTC), runs_factory=None,
                  projection: SafeProjection | None = None, demo_mode: bool = False,
                  chat_agent=None, signin_url=None, moodle_enabled: bool = False,
                  chat_mode: str = 'recovery', owner_chat_history: bool = False,
                  account_url: str | None = None, planning_url: str | None = None, approval_store=None):
    """Trusted startup only. Missing runtime renders needs-input, never fake IO.

    runs_factory(auth) must return T24 RecoveryRuns using exactly this auth.
    Original identity/approval handlers keep their original implementations.
    """
    if chat_mode not in ('recovery', 'calendar_advice'):
        raise ValueError('unsupported_chat_mode')
    if demo_mode:
        from ...demo import require_demo
        require_demo(environment == "local")
    if environment != 'local' and getattr(chat_agent, 'local_preview_only', False):
        raise ValueError('agent_engine_chat_runtime_required')
    app = create_identity_app(policy=policy, documents=documents, verifier=verifier,
                             origin=origin, environment=environment, clock=clock, approval_store=approval_store)
    auth = WebAuth(documents, policy, verifier=verifier, origin=origin,
                   environment=environment, clock=clock)
    app.state.web_auth = auth
    app.include_router(create_speech_router(auth=auth, agent=chat_agent))
    plans = FirestorePlanStore(documents, policy)
    approvals = approval_store or FirestoreApprovalStore(documents, policy, clock=clock)
    app.include_router(create_chat_router(auth=auth, documents=documents, policy=policy,
        agent=chat_agent, projection=projection, owner_history=owner_chat_history))
    runs = None if runs_factory is None else runs_factory(auth)
    if runs is not None:
        if not isinstance(runs, RecoveryRuns) or runs.auth is not auth or runs.clock is not clock:
            raise ValueError("matching_explicit_run_dependencies_required")
        app.include_router(create_runs_router(runs))

    app.mount('/assets/logo', StaticFiles(directory=Path(__file__).resolve().parents[1] / 'assets' / 'logo'), name='welcome-logo')

    @app.get("/", response_class=HTMLResponse)
    async def page(request: Request):
        auth.check_request(request)
        return render_shell(policy.config.student.timezone, demo_mode=demo_mode,
                            live_chat=chat_agent is not None, signin_url=signin_url,
                            moodle_enabled=moodle_enabled, chat_mode=chat_mode, account_url=account_url,
                            planning_url=planning_url)

    @app.get("/assets/campuspulse.css")
    async def css():
        return Response(STYLES, media_type="text/css")

    @app.get("/assets/campuspulse.js")
    async def javascript():
        return Response(SCRIPT, media_type="text/javascript")

    @app.get("/ui/state", response_class=HTMLResponse)
    async def state(request: Request):
        credential = await auth.load(request)
        if set(request.query_params) - {"run_id", "plan_id", "version"} or any(
                len(request.query_params.getlist(k)) != 1 for k in request.query_params):
            raise ValueError("invalid_ui_selector")
        run_id = request.query_params.get("run_id")
        run = None
        if run_id is not None:
            if runs is None:
                raise ValueError("runtime_not_configured")
            run = await runs.read(request, run_id)
        plan = approval = current = None
        sources = SafeSources()
        with auth.request_scope(credential) as context:
            reference = None if run is None else run.plan
            if "plan_id" in request.query_params or "version" in request.query_params:
                from ..app import PlanQuery
                raw = request.query_params.get("version", "")
                if not raw.isascii() or not raw.isdigit() or len(raw) > 19:
                    raise ValueError("invalid_version")
                query = PlanQuery(plan_id=request.query_params.get("plan_id"), version=int(raw))
                if reference is not None and (query.plan_id, query.version) != (reference.plan_id, reference.version):
                    raise ValueError("run_plan_mismatch")
                plan = await plans.get(context, query.plan_id, query.version)
            elif reference is not None:
                plan = await plans.get(context, reference.plan_id, reference.version)
            if plan is not None:
                if reference is not None and reference != plan_reference(plan):
                    raise ValueError("run_plan_hash_mismatch")
                approval = await approvals.get(context, plan_reference(plan))
                current = await plans.get_current(context)
            if projection is not None:
                sources = SafeSources.model_validate(await projection.project(context, run=run, plan=plan))
                if sources.verification is not None and sources.verification.calendar_source is not None:



                    policy.require_source(context, sources.verification.calendar_source.reference, current_run=False)

            latest = await auth.load(request)
            if latest.session != credential.session:
                from ...security.context import AccessDenied
                raise AccessDenied("session_changed")
            policy.require_context(context)
            return render_panel(run=run, plan=plan, approval=approval, current=current,
                sources=sources, timezone=policy.config.student.timezone,
                now=clock(), runtime_available=runs is not None,
                moodle_enabled=moodle_enabled)



    @app.middleware("http")
    async def ui_csp(request, call_next):
        response = await call_next(request)
        if request.url.path in {"/", "/ui/state", "/planning", "/assets/recovery-planning.js",
                "/assets/campuspulse.css", "/assets/campuspulse.js"} or request.url.path.startswith('/assets/logo/'):
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; "
                "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
        return response

    return app
