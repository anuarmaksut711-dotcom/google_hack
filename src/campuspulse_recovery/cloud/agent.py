"""Private Cloud Run ADK worker. Only a backend-issued grant selects work."""
import asyncio
import inspect
import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..application.proposals import ProposalBasis
from ..domain.ports import BindingConflict
from ..security.context import AccessDenied
from ..storage.plans import _decode, _encode, _key
from ..web.agent_client import RemoteFrame
from .authority import BoundAuthority, Grants, ServiceIdentity, utcnow
from .transport import CloudTransport, McpTransport
from .planning import current_constraints, planning_inputs


def basis_path(run_id):
    return 'cp_cloud_bases/' + _key(run_id)


async def recover(settings, documents, policy, context, grants, handle, grant, mcp):
    from google.adk.agents import Agent
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.adk.tools import FunctionTool
    from google.genai import types
    from ..agent import policy as rules
    from ..agent.agent import run_recovery
    from ..agent.conversation import AdkConversationAgent
    from ..agent.instructions import RECOVERY_INSTRUCTIONS
    from ..agent.solve_tool import solve_repair_tool
    inputs = planning_inputs(settings, policy, documents, clock=utcnow)
    input_state = None if inputs is None else await inputs.get(context)

    async def forward(name, arguments):
        state = rules.current()
        if state.pending != name or state.remote_attempts >= rules.MAX_REMOTE_ATTEMPTS:
            return {'isError': True, 'code': 'remote_attempt_budget'}
        state.remote_attempts += 1
        try:
            child, _ = await grants.child(handle, tool=name, arguments=arguments)
            return await mcp.call(child, name, arguments)
        except asyncio.CancelledError:
            raise
        except Exception:
            return {'isError': True, 'code': 'transport_outcome_unconfirmed'}

    async def read_assignments(horizon: dict, schema_version: str = '1.0') -> dict:
        """Read the configured assignment source for the server planning horizon."""
        return await forward('read_assignments', {'horizon': horizon})

    async def read_calendar(horizon: dict, schema_version: str = '1.0') -> dict:
        """Read the configured calendar for the server planning horizon."""
        return await forward('read_calendar', {'horizon': horizon})

    async def apply_plan(plan_id: str, version: int, request_key: str, schema_version: str = '1.0') -> dict:
        """Apply only the exact saved plan approved in the user interface."""
        return await forward('apply_plan', dict(plan_id=plan_id, version=version, request_key=request_key))

    async def verify_plan(plan_id: str, version: int, schema_version: str = '1.0') -> dict:
        """Read Calendar again and independently verify the saved plan."""
        return await forward('verify_plan', dict(plan_id=plan_id, version=version))

    def before_tool(tool, args, tool_context):
        if tool.name == 'solve_repair':
            state = rules.current(tool_context)
            now = utcnow()
            # Proposal time follows both actual reads. Crossing a quarter-hour
            # requires a new horizon/read instead of publishing stale coverage.
            expected = settings.constraints(now) if inputs is None else inputs.constraints(input_state, now=now)
            if expected != state.constraints:
                state.status = 'stale'
                return rules.safe_error('planning_horizon_changed')
            state.now = now
        return rules.before_tool(tool=tool, args=args, tool_context=tool_context)

    intent = grant.intent
    if intent is None:
        raise AccessDenied('recovery_intent_required')
    model = AdkConversationAgent(settings.chat)._create_model()
    runner = None
    try:
        agent = Agent(name='campuspulse_recovery', model=model, instruction=RECOVERY_INSTRUCTIONS,
            tools=[FunctionTool(read_assignments), FunctionTool(read_calendar),
                   FunctionTool(apply_plan), FunctionTool(verify_plan), solve_repair_tool],
            before_agent_callback=rules.before_agent, before_model_callback=rules.before_model,
            after_model_callback=rules.after_model, before_tool_callback=before_tool,
            after_tool_callback=rules.after_tool, on_tool_error_callback=rules.on_tool_error,
            on_model_error_callback=rules.on_model_error)
        sessions = InMemorySessionService()
        await sessions.create_session(app_name='campuspulse_cloud', user_id=context.trusted.owner_id,
                                      session_id=context.session_id)
        runner = Runner(agent=agent, app_name='campuspulse_cloud', session_service=sessions)
        with rules.backend_invocation(context, policy,
            mode='recovery' if intent.action == 'propose' else 'apply', constraints=grant.constraints,
            now=grant.planning_at, proposal_id=grant.proposal_id, proposal_version=1,
            allowed_plan=intent.plan, allowed_apply=intent.apply) as state:
            message = ('Read both configured sources and calculate a recovery candidate using solve_repair.'
                       if intent.action == 'propose' else
                       'Apply the exact approved plan in the invocation policy, then verify it by reading Calendar.')
            message += '\nServer-selected tool arguments: ' + json.dumps({
                'horizon': grant.constraints.horizon.model_dump(mode='json'),
                'apply': None if intent.apply is None else intent.apply.model_dump(mode='json'),
                'verify': None if intent.plan is None else
                    {'plan_id': intent.plan.plan_id, 'version': intent.plan.version}})
            result = await run_recovery(runner, new_message=types.Content(role='user',
                parts=[types.Part(text=message)]))
            pair = result.candidate_input is not None and result.candidate is not None
            if pair:
                if intent.action != 'propose' or state.assignments is None or state.calendar is None:
                    raise BindingConflict('proposal_source_basis_missing')
                basis = ProposalBasis(plan_id=grant.proposal_id, version=1,
                    assignments=state.assignments.source.reference, calendar=state.calendar.source.reference,
                    constraints=grant.constraints, now=state.now, expected_current=grant.expected_current)
                async def save(tx):
                    await grants.load(handle, status='running', reader=tx)
                    old = await tx.get(basis_path(intent.run_id))
                    if old is not None:
                        if _decode(old, 'cloud_basis', ProposalBasis) != basis:
                            raise BindingConflict('immutable_proposal_basis')
                    else:
                        tx.create(basis_path(intent.run_id), _encode('cloud_basis', basis))
                await documents.transact(save)
                return RemoteFrame(run_id=intent.run_id, intent_id=intent.intent_id, kind='terminal',
                    status='candidate', candidate_input=result.candidate_input, candidate=result.candidate)
            status = 'applied_unverified' if result.status == 'verified' else result.status
            if status not in {'no_change', 'needs_input', 'infeasible', 'partial', 'unknown',
                             'applied_unverified', 'verification_failed', 'stale', 'unavailable'}:
                status = 'unknown' if state.apply_dispatched else 'unavailable'
            return RemoteFrame(run_id=intent.run_id, intent_id=intent.intent_id, kind='terminal', status=status)
    finally:
        try:
            if runner is not None:
                await runner.close()
        finally:
            # Same explicit client lifetime as the conversational ADK adapter.
            client = model.__dict__.get('api_client')
            if client is not None:
                async with asyncio.timeout(3):
                    await client.aio.aclose()
                client.close()


def create_agent(settings, policy, documents, *, close):
    grants = Grants(documents, policy, clock=utcnow)
    inputs = planning_inputs(settings, policy, documents, clock=utcnow)
    identity = ServiceIdentity(settings.agent_origin, policy.config.identities.backend_service_account)
    authority = BoundAuthority(policy, 'agent', policy.config.identities.backend_service_account)
    mcp = McpTransport(settings.mcp_origin)
    evidence = CloudTransport(settings.mcp_origin)

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            value = close()
            if inspect.isawaitable(value):
                await value

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get('/healthz')
    async def health():
        return {'status': 'alive'}

    @app.post('/internal/invoke')
    async def invoke(request: Request):
        handle = None
        claimed = False
        try:
            verified = await identity.verify(request)
            refs = request.headers.getlist('x-campuspulse-invocation')
            if len(refs) != 1:
                raise AccessDenied('invocation_reference_required')
            handle = refs[0]
            grant = await grants.load(handle, status='ready')
            if grant.parent is not None or grant.operation not in {'read', 'recover', 'evidence'}:
                raise AccessDenied('worker_grant_required')
            grant = await grants.claim(handle)
            claimed = True
            with authority.bind(request, grant, verified), authority.contexts.request(request) as context:
                if grant.operation == 'recover' and grant.constraints != await current_constraints(
                        settings, inputs, context, grant.planning_at):
                    raise AccessDenied('planning_policy_changed')
                if grant.operation == 'read':
                    output = {}
                    for name in ('read_assignments', 'read_calendar'):
                        args = {'horizon': grant.constraints.horizon.model_dump(mode='json')}
                        child, _ = await grants.child(handle, tool=name, arguments=args)
                        result = await mcp.call(child, name, args)
                        if result.get('isError') or result.get('structuredContent') is None:
                            raise BindingConflict('source_unavailable')
                        output[name] = result['structuredContent']
                    return output
                if grant.operation == 'evidence':
                    if grant.reference is None:
                        raise AccessDenied('evidence_reference_required')
                    child, _ = await grants.child(handle, tool='_evidence',
                        arguments=grant.reference.model_dump(mode='json'))
                    return await evidence.request(child, path='/internal/evidence')
                frame = await recover(settings, documents, policy, context, grants, handle, grant, mcp)
                return frame.model_dump(mode='json')
        except asyncio.CancelledError:
            raise
        except AccessDenied:
            return JSONResponse({'error': 'service_or_invocation_rejected'}, status_code=403)
        except Exception:
            return JSONResponse({'error': 'invocation_outcome_unconfirmed'}, status_code=503)
        finally:
            if claimed:
                await grants.finish(handle)

    return app
