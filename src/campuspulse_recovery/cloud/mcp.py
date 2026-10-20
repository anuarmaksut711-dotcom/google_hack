"""Production MCP composition: Google APIs, Firestore, IAM and exact tool grants."""
import asyncio
from contextvars import ContextVar
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse

from ..application.preflight import PreflightService
from ..application.verify import VerificationService
from ..domain.models import PlanReference
from ..domain.ports import BindingConflict
from ..integrations.assignments import AssignmentsReader
from ..integrations.calendar_read import CalendarReader
from ..integrations.calendar_write import CalendarWriter
from ..mcp_service.server import create_complete_app
from ..observability.adapters import (JournalEmitter, ObservationHealth,
    instrument_assignments, instrument_calendar, instrument_verification, instrument_moodle_assignments)
from ..security.context import AccessDenied
from ..storage.operations import FirestoreExecutionStore
from ..storage.owned_blocks import FirestoreOwnedBlockRegistry
from ..storage.plans import FirestorePlanStore
from ..storage.runs import FirestoreRunEventStore
from ..storage.snapshots import FirestoreSnapshotStore
from ..web.approval import FirestoreApprovalStore
from .authority import BoundAuthority, Grants, ServiceIdentity, utcnow
from .writers import CloudWorkspaceClients, DurableRecoveryGuard, TrackedApplyService, TrackedCoordination
from .planning import current_constraints, planning_inputs


def create_mcp(settings, policy, documents, *, port, close):
    grants = Grants(documents, policy, clock=utcnow)
    inputs = planning_inputs(settings, policy, documents, clock=utcnow)
    authority = BoundAuthority(policy, 'mcp', policy.config.identities.agent_service_account)
    identity = ServiceIdentity(settings.mcp_origin, policy.config.identities.agent_service_account)
    active_reference = ContextVar('mcp_cloud_reference', default=None)
    clients = CloudWorkspaceClients(policy, grants, active_reference.get)
    snapshots = FirestoreSnapshotStore(documents, policy)
    registry = FirestoreOwnedBlockRegistry(documents, policy)
    plans = FirestorePlanStore(documents, policy)
    if inputs is None:
        approvals = FirestoreApprovalStore(documents, policy, clock=utcnow)
    else:
        from ..lms.planning_guard import MoodleApprovalStore
        approvals = MoodleApprovalStore(documents, policy, inputs=inputs, snapshots=snapshots, clock=utcnow)
    guard = DurableRecoveryGuard(documents, policy)
    coordination = TrackedCoordination(documents, policy, clock=utcnow, recovery_guard=guard)
    executions = FirestoreExecutionStore(documents, policy, coordination=coordination,
                                         approval_guard=approvals.transaction_guard())
    journal = FirestoreRunEventStore(documents, policy, clock=utcnow)
    # Emitters do not grant authority. Required store writes fail before dispatch.
    trace_health = ContextVar('cloud_mcp_trace_health', default=None)
    class ScopedEmitter(JournalEmitter):
        def __init__(self):
            self.store, self.clock = journal, utcnow

        @property
        def health(self):
            value = trace_health.get()
            if value is None:
                raise BindingConflict('trace_scope_required')
            return value
    emitter = ScopedEmitter()
    if inputs is None:
        assignments = instrument_assignments(AssignmentsReader(clients, snapshots, clock=utcnow), emitter)
    else:
        from ..lms.planning import MoodleAssignmentsReader
        assignments = instrument_moodle_assignments(
            MoodleAssignmentsReader(clients, snapshots, inputs=inputs, clock=utcnow), emitter)
    calendar = instrument_calendar(CalendarReader(clients, snapshots, registry, clock=utcnow), emitter)
    preflight = PreflightService(plans, snapshots, approvals, executions, coordination,
        registry, assignments, calendar, policy, clock=utcnow)
    verification = instrument_verification(VerificationService(plans, snapshots, executions,
        registry, assignments, calendar, policy, clock=utcnow), emitter)
    executions.verification_guard = verification.transaction_guard()
    apply = TrackedApplyService(preflight,
        CalendarWriter(clients, registry, coordination, plans, clock=utcnow),
        clock=utcnow, verification_reader=verification)
    apply.trace = emitter

    async def dispatch_guard(name, arguments, request, invoke):
        reference = active_reference.get()
        if name == 'apply_plan':
            with authority.contexts.request(request) as context:
                saved = await plans.get(context, arguments['plan_id'], arguments['version'])
                if saved.payload.constraints != await current_constraints(
                        settings, inputs, context, saved.payload.constraints.horizon.start):
                    raise AccessDenied('planning_policy_changed')
        await grants.claim(reference, tool=name, arguments=arguments)
        health = ObservationHealth()
        trace_token = trace_health.set(health)
        try:
            result = await invoke()
            if health.failed:
                raise BindingConflict('journal_delivery_unconfirmed')
            return result
        finally:
            # Does not authorize retry or assert a successful Calendar outcome.
            try:
                await grants.finish(reference)
            finally:
                trace_health.reset(trace_token)
    authority.contexts.dispatch_guard = dispatch_guard
    inner = create_complete_app(contexts=authority.contexts, assignments=assignments,
        calendar=calendar, apply=apply, verification=verification,
        allowed_hosts=[urlsplit(settings.mcp_origin).netloc], allowed_origins=[], port=port, close=close)

    async def evidence(request):
        reference = active_reference.get()
        grant = await grants.load(reference, status='ready')
        await grants.claim(reference, tool='_evidence', arguments=grant.arguments)
        try:
            with authority.contexts.request(request) as context:
                saved = await verification.latest(context, PlanReference.model_validate(grant.arguments))
                return JSONResponse(saved.result.model_dump(mode='json'))
        finally:
            await grants.finish(reference)
    inner.app.add_route('/internal/evidence', evidence, methods=['POST'])

    class AuthorizedMcp:
        async def __call__(self, scope, receive, send):
            if scope['type'] != 'http' or scope['path'] == '/healthz':
                await inner(scope, receive, send)
                return
            request = Request(scope)
            started = False
            async def tracked_send(message):
                nonlocal started
                if message['type'] == 'http.response.start':
                    started = True
                await send(message)
            try:
                verified = await identity.verify(request)
                refs = request.headers.getlist('x-campuspulse-invocation')
                if len(refs) != 1:
                    raise AccessDenied('invocation_reference_required')
                grant = await grants.load(refs[0])
                if grant.operation != 'mcp':
                    raise AccessDenied('mcp_grant_required')
                with authority.bind(request, grant, verified):
                    token = active_reference.set(refs[0])
                    try:
                        await inner(scope, receive, tracked_send)
                    finally:
                        active_reference.reset(token)
            except asyncio.CancelledError:
                raise
            except Exception:
                if started:
                    raise RuntimeError('mcp_response_interrupted') from None
                # Authentication errors are redacted; MCP dispatch handles domain errors.
                await JSONResponse({'error': 'service_or_invocation_rejected'}, status_code=403)(scope, receive, send)
    result = AuthorizedMcp()
    result.router = inner.app.router
    return result
