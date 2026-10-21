"""Private Cloud Run MCP, real Workspace clients and durable execution state."""
from contextvars import ContextVar
from datetime import UTC, datetime
from urllib.parse import urlsplit

from ..security.context import AccessDenied, AuthenticatedCaller, RequestContextFactory
from ..security.resources import ResourcePolicy
from .live_common import NoUncertainTakeover, invocation, settings, sync_client


class IamBindings:
    def __init__(self, config, audience):
        self.config, self.audience = config, audience
        self.client = sync_client(config)
        self.current = ContextVar('verified_mcp_binding', default=None)

    def authenticate(self, request, *, boundary, now):
        from google.auth.transport.requests import Request
        from google.oauth2.id_token import verify_oauth2_token
        if boundary != 'mcp':
            raise AccessDenied('mcp_boundary_required')
        header = request.headers.get('authorization', '')
        if not header.startswith('Bearer ') or len(header) > 16384:
            raise AccessDenied('iam_token_required')
        try:
            class BoundedRequest(Request):
                def __call__(self, *args, **kwargs):
                    kwargs['timeout'] = 5
                    return super().__call__(*args, **kwargs)
            claims = verify_oauth2_token(header[7:], BoundedRequest(), audience=self.audience)
            if (claims.get('email') != self.config.identities.agent_service_account
                    or claims.get('email_verified') is not True):
                raise AccessDenied('agent_identity_required')
            value, binding = invocation(self.client, request.headers.get('x-cp-invocation'), self.config)
            if value.get('state') != 'running':
                raise AccessDenied('active_managed_invocation_required')
            self.current.set(binding)
            return AuthenticatedCaller(principal_id=self.config.identities.agent_service_account,
                session_id=binding.session_id, run_id=binding.run_id,
                authenticated_at=datetime.fromtimestamp(claims['iat'], UTC),
                expires_at=min(binding.expires_at, datetime.fromtimestamp(claims['exp'], UTC)))
        except Exception:
            raise AccessDenied('iam_invocation_rejected') from None

    def resolve(self, *, session_id, run_id):
        b = self.current.get()
        return b if b and (b.session_id, b.run_id) == (session_id, run_id) else None


def create_app(*, port=8080):
    from google.cloud.firestore import AsyncClient
    from starlette.responses import JSONResponse
    from ..application.apply import ApplyService
    from ..application.preflight import PreflightService
    from ..application.verify import VerificationService
    from ..domain.models import PlanReference
    from ..integrations.assignments import AssignmentsReader
    from ..integrations.calendar_read import CalendarReader
    from ..integrations.calendar_write import CalendarWriter
    from ..integrations.google_clients import GoogleWorkspaceClients
    from ..mcp_service.server import create_complete_app
    from ..observability.adapters import (JournalEmitter, ObservationHealth, instrument_assignments,
        instrument_calendar, instrument_apply, instrument_verification)
    from ..storage.leases import FirestoreCalendarCoordination
    from ..storage.operations import FirestoreExecutionStore
    from ..storage.owned_blocks import FirestoreOwnedBlockRegistry
    from ..storage.plans import FirestoreDocuments, FirestorePlanStore
    from ..storage.runs import FirestoreRunEventStore
    from ..storage.snapshots import FirestoreSnapshotStore
    from ..web.approval import FirestoreApprovalStore
    value, config = settings()
    policy = ResourcePolicy(config, environment='cloud')
    auth = IamBindings(config, value['mcp_url'])
    contexts = RequestContextFactory(policy, boundary='mcp', authenticator=auth, bindings=auth)
    client = AsyncClient(project=config.project_id, database=config.firestore_database)
    documents = FirestoreDocuments(client, project_id=config.project_id, database_id=config.firestore_database)
    snapshots = FirestoreSnapshotStore(documents, policy)
    registry = FirestoreOwnedBlockRegistry(documents, policy)
    clients = GoogleWorkspaceClients(policy, environment='cloud', read_only=False)
    plans = FirestorePlanStore(documents, policy)
    clock = lambda: datetime.now(UTC)
    approvals = FirestoreApprovalStore(documents, policy, clock=clock)
    coordination = FirestoreCalendarCoordination(documents, policy, clock=clock, recovery_guard=NoUncertainTakeover())
    executions = FirestoreExecutionStore(documents, policy, coordination=coordination,
        approval_guard=approvals.transaction_guard())
    emitter = JournalEmitter(FirestoreRunEventStore(documents, policy, clock=clock), clock=clock, health=ObservationHealth())
    assignments = instrument_assignments(AssignmentsReader(clients, snapshots, clock=clock), emitter)
    calendar = instrument_calendar(CalendarReader(clients, snapshots, registry, clock=clock), emitter)
    preflight = PreflightService(plans, snapshots, approvals, executions, coordination,
        registry, assignments, calendar, policy, clock=clock)
    verification = instrument_verification(VerificationService(plans, snapshots, executions,
        registry, assignments, calendar, policy, clock=clock), emitter)
    executions.verification_guard = verification.transaction_guard()
    apply = instrument_apply(ApplyService(preflight,
        CalendarWriter(clients, registry, coordination, plans, clock=clock),
        clock=clock, verification_reader=verification), emitter)
    app = create_complete_app(contexts=contexts, assignments=assignments, calendar=calendar,
        apply=apply, verification=verification,
        allowed_hosts=[urlsplit(value['mcp_url']).netloc], allowed_origins=[], port=port, close=client.close)

    async def evidence(request):
        try:
            raw = await request.body()
            if len(raw) > 2048:
                raise ValueError()
            reference = PlanReference.model_validate_json(raw)
            with contexts.request(request) as context:
                result = (await verification.latest(context, reference)).result
                return JSONResponse(result.model_dump(mode='json'))
        except Exception:
            return JSONResponse({'error': 'verification_evidence_unavailable'}, status_code=409)
    # Authenticated service endpoint, not an additional model-facing MCP tool.
    app.app.add_route('/private/evidence', evidence, methods=['POST'])
    return app
