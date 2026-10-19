"""Production web composition with durable sources, proposals and private worker IO."""
import inspect
import os
from contextlib import asynccontextmanager
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi.responses import RedirectResponse

from ..agent.conversation import AdkConversationAgent
from ..application.preflight import _work, _busy
from ..application.proposals import ProposalBasis, ProposalService
from ..domain.models import AssignmentsSnapshot, CalendarSnapshot, VerificationResult
from ..domain.ports import BindingConflict
from ..integrations.assignments import normalized_assignments
from ..security.context import AccessDenied
from ..storage.plans import FirestorePlanStore, _decode, plan_reference
from ..storage.runs import FirestoreRunEventStore
from ..storage.snapshots import FirestoreSnapshotStore
from ..web.agent_client import RemoteFrame
from ..web.approval import FirestoreApprovalStore
from ..web.auth import GoogleSignInVerifier
from ..web.runs import RecoveryRuns, RunRegistry
from ..web.views.app import SafeSources, create_ui_app
from .agent import basis_path
from .authority import Grants, utcnow
from .transport import CloudTransport, MoodleRefreshTransport
from .planning import current_constraints, planning_inputs


class CloudConversation:
    """Authenticated web chat over real source projections; no mutation tools."""
    local_preview_only = False

    def __init__(self, target):
        self.agent = AdkConversationAgent(target)

    async def reply(self, **kwargs):
        return await self.agent.reply(**kwargs)


class CloudBackend:
    def __init__(self, settings, policy, documents):
        self.settings, self.policy, self.documents = settings, policy, documents
        self.plans = FirestorePlanStore(documents, policy)
        self.snapshots = FirestoreSnapshotStore(documents, policy)
        self.approvals = FirestoreApprovalStore(documents, policy, clock=utcnow)
        self.journal = FirestoreRunEventStore(documents, policy, clock=utcnow)
        self.grants = Grants(documents, policy, clock=utcnow)
        self.worker = CloudTransport(settings.agent_origin)
        self.planning = planning_inputs(settings, policy, documents, clock=utcnow)
        self.moodle = None
        if settings.moodle is not None:
            cfg = settings.moodle
            self.moodle = self.planning.connection
            self.moodle_worker = MoodleRefreshTransport(cfg.sync_origin)

    def _moodle_owner(self, context):
        trusted = self.policy.require_context(context)
        if context.boundary != 'backend' or trusted.owner_id != self.policy.config.student.trusted_user_id:
            raise AccessDenied('moodle_owner_required')
        if self.moodle is None:
            raise BindingConflict('moodle_not_configured')

    async def moodle_read(self, context):
        self._moodle_owner(context)
        return await self.moodle.read()

    async def moodle_refresh(self, context):
        self._moodle_owner(context)
        await self.moodle_worker.refresh()

    async def read(self, context, *, constraints=None):
        await self.journal.register(context)
        now = utcnow()
        constraints = constraints or await current_constraints(self.settings, self.planning, context, now)
        handle, _ = await self.grants.issue(context, operation='read', constraints=constraints,
            proposal_id='read_' + uuid4().hex, planning_at=now)
        result = await self.worker.request(handle, seconds=50)
        a = AssignmentsSnapshot.model_validate(result['read_assignments'])
        c = CalendarSnapshot.model_validate(result['read_calendar'])
        saved_a = await self.snapshots.get_assignments(context, a.source.reference)
        saved_c = await self.snapshots.get_calendar(context, c.source.reference)
        if (saved_a, saved_c) != (a, c):
            raise BindingConflict('source_store_mismatch')
        for snapshot in (a, c):
            self.policy.require_source(context, snapshot.source.reference)
            if not snapshot.source.complete:
                raise BindingConflict('complete_current_sources_required')
        if c.source.covered_interval != constraints.horizon:
            raise BindingConflict('calendar_horizon_mismatch')
        return a, c

    async def resolve(self, context):
        self.policy.require_context(context)
        basis = _decode(await self.documents.get(basis_path(context.trusted.run_id)), 'cloud_basis', ProposalBasis)
        self.policy.require_source(context, basis.assignments)
        self.policy.require_source(context, basis.calendar)
        if basis.constraints != await current_constraints(self.settings, self.planning, context, basis.now):
            raise BindingConflict('cloud_planning_policy_changed')
        return basis

    async def check(self, context, reference):
        plan = await self.plans.get(context, reference.plan_id, reference.version)
        if plan_reference(plan) != reference:
            raise BindingConflict('exact_plan_changed')
        if plan.payload.constraints != await current_constraints(
                self.settings, self.planning, context, plan.payload.constraints.horizon.start):
            raise BindingConflict('planning_policy_changed')
        old, old_calendar = await self.snapshots.get_for_plan(context, reference)
        assignments, calendar = await self.read(context, constraints=plan.payload.constraints)
        if _work(old) != _work(assignments):
            raise BindingConflict('assignments_changed')
        external = lambda s: [event for event in s.events if event.campuspulse_marker is None]
        horizon = plan.payload.constraints.horizon
        if _busy(external(old_calendar), horizon) != _busy(external(calendar), horizon):
            raise BindingConflict('calendar_availability_changed')

    async def stream(self, context, intent):
        now = utcnow()
        constraints = await current_constraints(self.settings, self.planning, context, now)
        handle, _ = await self.grants.issue(context, operation='recover', intent=intent,
            constraints=constraints, planning_at=now,
            proposal_id='plan_' + uuid4().hex, expected_current=await self.plans.get_current(context))
        result = await self.worker.request(handle, seconds=110)
        self.policy.require_context(context)
        yield RemoteFrame.model_validate(result)

    async def stored_evidence(self, context, reference):
        saved = await self.plans.get(context, reference.plan_id, reference.version)
        if plan_reference(saved) != reference:
            raise BindingConflict('exact_plan_changed')
        now = utcnow()
        constraints = await current_constraints(self.settings, self.planning, context, now)
        handle, _ = await self.grants.issue(context, operation='evidence', reference=reference,
            constraints=constraints, planning_at=now, proposal_id=reference.plan_id)
        result = VerificationResult.model_validate(await self.worker.request(handle, seconds=25))
        if result.plan != reference:
            raise BindingConflict('evidence_plan_mismatch')
        if result.calendar_source is not None:
            self.policy.require_source(context, result.calendar_source.reference, current_run=False)
        return result

    async def current(self, context, intent):
        if intent.plan is None:
            return None
        return await self.stored_evidence(context, intent.plan)

    async def project(self, context, *, run, plan):
        if self.moodle is not None:
            state = await self.moodle_read(context)
            if state.snapshot is None:
                return SafeSources(assignments=('Данные Moodle ещё не получены.',))
            lines = []
            if state.stale or state.error:
                lines.append('Показаны последние сохранённые данные Moodle; обновление не подтверждено.')
            for task in state.snapshot.tasks:
                score = 'ещё не оценено' if task.grade_display is None else 'баллы: ' + task.grade_display
                deadline = 'срок не указан' if task.due_at is None else task.due_at.isoformat()
                lines.append(f'{task.course_title}: {task.title} · {deadline} · {score}'[:500])
            for course in state.snapshot.courses:
                if course.grade_display is not None:
                    lines.append(f'{course.title}: итог по курсу {course.grade_display}'[:500])
            evidence = None
            if run is not None and plan is not None and run.verification_status is not None:
                latest = await self.stored_evidence(context, plan_reference(plan))
                if latest.run_id == run.run_id:
                    evidence = latest
            return SafeSources(assignments=tuple(lines[:100]), verification=evidence)
        assignments, calendar = await self.read(context)
        zone = ZoneInfo(self.policy.config.student.timezone)
        def fmt(value):
            return value.astimezone(zone).strftime('%d.%m %H:%M')
        # Curated, bounded display text; never return raw Sheets/Calendar bodies.
        rows = normalized_assignments(assignments)
        lines = []
        for item in rows:
            remaining = 'уточните объём работы' if item.remaining_minutes is None else f'осталось {item.remaining_minutes} мин.'
            deadline = 'уточните срок' if item.deadline is None else 'срок ' + fmt(item.deadline.at)
            lines.append(f'{item.course}: {item.title} · {remaining} · {deadline}'[:500])
        spans = [f'{"Учебный блок" if event.campuspulse_marker else "Занятие"} · '
                 f'{fmt(event.interval.start)}–{fmt(event.interval.end)}'
                 for event in calendar.events if event.interval is not None]
        if len(lines) > 100:
            lines = lines[:99] + ['Показаны первые 99 заданий; расчёт использует весь прочитанный список.']
        if len(spans) > 100:
            spans = spans[:99] + ['Показаны первые 99 событий; расчёт использует весь прочитанный календарь.']
        evidence = None
        if run is not None and plan is not None and run.verification_status is not None:
            latest = await self.stored_evidence(context, plan_reference(plan))
            if latest.run_id == run.run_id:
                evidence = latest
        return SafeSources(assignments=tuple(lines), calendar=tuple(spans), verification=evidence)

    def runs(self, auth):
        self.auth = auth
        return RecoveryRuns(auth=auth, registry=RunRegistry(self.documents, self.policy, clock=utcnow),
            remote=self, proposals=ProposalService(self.snapshots, self.plans, self.policy, self, clock=utcnow),
            plans=self.plans, approvals=self.approvals, sources=self, journal=self.journal, evidence=self)


def create_backend(settings, policy, documents, *, close):
    system = CloudBackend(settings, policy, documents)
    moodle_enabled = settings.moodle is not None
    chat_available = settings.chat.provider == 'vertex' or bool(os.environ.get('GEMINI_API_KEY', '').strip())
    app = create_ui_app(policy=policy, documents=documents,
        verifier=GoogleSignInVerifier(settings.google_signin_client_id), origin=settings.backend_origin,
        environment='cloud', clock=utcnow, runs_factory=system.runs, projection=system,
        chat_agent=CloudConversation(settings.chat) if chat_available else None,
        signin_url='/login', moodle_enabled=moodle_enabled)
    system.auth = app.state.web_auth
    if moodle_enabled:
        from ..web.moodle import install_moodle_routes
        install_moodle_routes(app, auth=system.auth, reader=system.moodle_read,
                              refresh=system.moodle_refresh)
        from ..web.moodle_planning import install_planning_routes
        install_planning_routes(app, auth=system.auth, inputs=system.planning)

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            value = close()
            if inspect.isawaitable(value):
                await value
    app.router.lifespan_context = lifespan

    @app.get('/healthz')
    async def health():
        return {'status': 'alive'}

    @app.middleware('http')
    async def require_signin(request, call_next):
        if request.method == 'GET' and request.url.path == '/':
            try:
                await system.auth.load(request)
            except AccessDenied:
                return RedirectResponse('/login', status_code=303, headers={'Cache-Control': 'no-store'})
        return await call_next(request)

    from .login import install_login
    install_login(app, system.auth, settings.google_signin_client_id)
    return app
