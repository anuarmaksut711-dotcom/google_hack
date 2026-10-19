"""Verified service identities and single-use, Firestore-backed invocation grants.

The browser and model never receive grants. A reference selects server-owned data;
it cannot replace a valid service ID token, live UI session or exact plan consent.
"""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
import secrets
import re
from typing import Annotated, Literal

from pydantic import Field

from ..domain.models import (Contract, Instant, PlanReference, PlanningConstraints, canonical_hash,
    ApplyPlanInput, VerifyPlanInput, ReadAssignmentsInput, ReadCalendarInput)
from ..domain.ports import BindingConflict
from ..security.context import AccessDenied, AuthenticatedCaller, RequestContextFactory, ServerRunBinding
from ..storage.plans import _decode, _encode, _key
from ..web.auth import WebSession, digest
from ..web.agent_client import AgentIntent
from ..web.runs import RunState

Reference = Annotated[str, Field(pattern=r'^[A-Za-z0-9_-]{43}$')]


def utcnow():
    return datetime.now(UTC)


class Grant(Contract):
    binding: ServerRunBinding
    session_path: str
    operation: Literal['read', 'recover', 'evidence', 'mcp']
    created_at: Instant
    planning_at: Instant
    expires_at: Instant
    status: Literal['ready', 'running', 'completed'] = 'ready'
    parent: Reference | None = None
    intent: AgentIntent | None = None
    constraints: PlanningConstraints
    proposal_id: str
    expected_current: PlanReference | None = None
    reference: PlanReference | None = None
    tool: str | None = None
    arguments: dict | None = None


def grant_path(reference):
    from pydantic import TypeAdapter
    TypeAdapter(Reference).validate_python(reference)
    return 'cp_cloud_invocations/' + digest(reference)


def tool_arguments(tool, arguments):
    models = {'read_assignments': ReadAssignmentsInput, 'read_calendar': ReadCalendarInput,
              'apply_plan': ApplyPlanInput, 'verify_plan': VerifyPlanInput, '_evidence': PlanReference}
    if tool not in models:
        raise AccessDenied('tool_not_allowed')
    return models[tool].model_validate(arguments).model_dump(mode='json')


class Grants:
    def __init__(self, documents, policy, *, clock=utcnow):
        self.documents, self.policy, self.clock = documents, policy, clock

    async def _session(self, reader, value):
        if not re.fullmatch(r'cp_web_sessions/[0-9a-f]{64}', value.session_path):
            raise AccessDenied('invalid_session_path')
        session = _decode(await reader.get(value.session_path), 'web_session', WebSession)
        b = value.binding
        self.policy.require_binding(b, session_id=b.session_id, run_id=b.run_id, now=self.clock())
        if (session.revoked or not session.authenticated_at <= self.clock() < session.expires_at
                or session.binding.session_id != b.session_id or session.binding.owner_id != b.owner_id
                or session.binding.config_revision != b.config_revision
                or session.expires_at != b.expires_at
                or session.principal_id != b.signin_principal_id or value.expires_at > session.expires_at
                or not value.created_at <= self.clock() < value.expires_at):
            raise AccessDenied('invocation_session_expired_or_revoked')
        return session

    async def _intent(self, reader, value):
        if value.intent is None:
            return
        run = _decode(await reader.get('cp_web_runs/' + _key(value.intent.run_id)), 'web_run', RunState)
        if run.intent != value.intent or run.binding != value.binding or not run.claimed or run.settled:
            raise AccessDenied('invocation_run_not_active')

    async def issue(self, context, *, operation, constraints, proposal_id, intent=None,
                    expected_current=None, reference=None, planning_at=None):
        self.policy.require_context(context)
        if context.boundary != 'backend':
            raise AccessDenied('backend_grant_issuer_required')
        handle = secrets.token_urlsafe(32)
        async def create(tx):
            index = await tx.get('cp_web_session_index/' + digest(context.session_id))
            if not isinstance(index, dict) or set(index) != {'path'}:
                raise AccessDenied('signin_again_for_cloud_session')
            if not isinstance(index['path'], str) or not re.fullmatch(r'cp_web_sessions/[0-9a-f]{64}', index['path']):
                raise AccessDenied('invalid_session_index')
            session = _decode(await tx.get(index['path']), 'web_session', WebSession)
            if session.binding.session_id != context.session_id:
                raise AccessDenied('session_index_binding_mismatch')
            binding = ServerRunBinding(**{**session.binding.model_dump(), 'run_id': context.trusted.run_id})
            now = self.clock()
            value = Grant(binding=binding, session_path=index['path'], operation=operation,
                created_at=now, expires_at=min(context.expires_at, session.expires_at, now + timedelta(minutes=3)),
                constraints=constraints, proposal_id=proposal_id, intent=intent, planning_at=planning_at or now,
                expected_current=expected_current, reference=reference)
            await self._session(tx, value)
            await self._intent(tx, value)
            self.policy.require_context(context)
            tx.create(grant_path(handle), _encode('cloud_grant', value))
            return value
        return handle, await self.documents.transact(create)

    async def load(self, handle, *, status=None, reader=None):
        reader = reader or self.documents
        value = _decode(await reader.get(grant_path(handle)), 'cloud_grant', Grant)
        await self._session(reader, value)
        await self._intent(reader, value)
        if status is not None and value.status != status:
            raise AccessDenied('invocation_already_consumed')
        if value.parent is not None:
            parent = await self.load(value.parent, status='running', reader=reader)
            if parent.parent is not None or parent.binding != value.binding or value.expires_at > parent.expires_at:
                raise AccessDenied('invocation_parent_mismatch')
        return value

    async def claim(self, handle, *, tool=None, arguments=None):
        if tool is not None:
            arguments = tool_arguments(tool, arguments)
        async def claim(tx):
            value = await self.load(handle, status='ready', reader=tx)
            if tool is not None and (value.operation != 'mcp' or value.tool != tool
                                    or canonical_hash(value.arguments) != canonical_hash(arguments)):
                raise AccessDenied('exact_tool_grant_required')
            value = value.model_copy(update={'status': 'running'})
            tx.set(grant_path(handle), _encode('cloud_grant', value))
            return value
        return await self.documents.transact(claim)

    async def child(self, parent_handle, *, tool, arguments):
        arguments = tool_arguments(tool, arguments)
        handle = secrets.token_urlsafe(32)
        async def create(tx):
            parent = await self.load(parent_handle, status='running', reader=tx)
            if parent.operation == 'mcp':
                raise AccessDenied('nested_tool_grant_rejected')
            if tool == '_evidence':
                if parent.operation != 'evidence' or parent.reference is None or arguments != parent.reference.model_dump(mode='json'):
                    raise AccessDenied('evidence_reference_required')
            elif tool in ('apply_plan', 'verify_plan'):
                intent = parent.intent
                if not intent or intent.action != 'apply':
                    raise AccessDenied('apply_intent_required')
                expected = (intent.apply.model_dump(mode='json') if tool == 'apply_plan' else
                            tool_arguments(tool, {'plan_id': intent.plan.plan_id, 'version': intent.plan.version}))
                if arguments != expected:
                    raise AccessDenied('exact_apply_arguments_required')
            elif tool in ('read_assignments', 'read_calendar'):
                if arguments != tool_arguments(tool, {'horizon': parent.constraints.horizon.model_dump(mode='json')}):
                    raise AccessDenied('server_horizon_required')
            else:
                raise AccessDenied('tool_not_allowed')
            value = parent.model_copy(update={'operation': 'mcp', 'parent': parent_handle,
                'status': 'ready', 'tool': tool, 'arguments': arguments})
            tx.create(grant_path(handle), _encode('cloud_grant', value))
            return value
        return handle, await self.documents.transact(create)

    async def finish(self, handle):
        # A completed grant can never be claimed again, including after session expiry.
        async def finish(tx):
            value = _decode(await tx.get(grant_path(handle)), 'cloud_grant', Grant)
            if value.status != 'running':
                raise BindingConflict('unclaimed_invocation_completion')
            tx.set(grant_path(handle), _encode('cloud_grant', value.model_copy(update={'status': 'completed'})))
        await self.documents.transact(finish)


class ServiceIdentity:
    def __init__(self, audience, principal):
        self.audience, self.principal = audience, principal

    async def verify(self, request):
        values = request.headers.getlist('authorization')
        if len(values) != 1 or not values[0].startswith('Bearer ') or len(values[0]) > 12000:
            raise AccessDenied('service_identity_required')
        token = values[0][7:]
        def check():
            import requests
            from google.auth.transport.requests import Request
            from google.oauth2.id_token import verify_oauth2_token
            class BoundedRequest(Request):
                def __call__(self, *args, **kwargs):
                    kwargs['timeout'] = 5
                    return super().__call__(*args, **kwargs)
            with requests.Session() as session:
                claims = verify_oauth2_token(token, BoundedRequest(session=session), audience=self.audience)
            now = utcnow()
            if (claims.get('iss') not in ('accounts.google.com', 'https://accounts.google.com')
                    or claims.get('aud') != self.audience or claims.get('email') != self.principal
                    or claims.get('email_verified') is not True or not claims.get('sub')
                    or not datetime.fromtimestamp(claims['iat'], UTC) <= now < datetime.fromtimestamp(claims['exp'], UTC)):
                raise AccessDenied('service_identity_rejected')
            return datetime.fromtimestamp(claims['iat'], UTC), datetime.fromtimestamp(claims['exp'], UTC)
        try:
            async with asyncio.timeout(8):
                return await asyncio.to_thread(check)
        except Exception:
            raise AccessDenied('service_identity_rejected') from None


class BoundAuthority:
    """Only verified ingress installs the context consumed by synchronous T04 adapters."""
    def __init__(self, policy, boundary, principal):
        self.policy, self.boundary, self.principal = policy, boundary, principal
        self.current = ContextVar('cloud_bound_authority_' + boundary, default=None)
        self.contexts = RequestContextFactory(policy, boundary=boundary, authenticator=self, bindings=self)

    @contextmanager
    def bind(self, credential, grant, verified):
        token = self.current.set((credential, grant, verified))
        try:
            yield
        finally:
            self.current.reset(token)

    def authenticate(self, credential, *, boundary, now):
        value = self.current.get()
        if value is None or credential.scope is not value[0].scope or boundary != self.boundary:
            raise AccessDenied('verified_ingress_scope_required')
        grant, (issued, expires) = value[1], value[2]
        return AuthenticatedCaller(principal_id=self.principal, session_id=grant.binding.session_id,
            run_id=grant.binding.run_id, authenticated_at=issued,
            expires_at=min(expires, grant.expires_at))

    def resolve(self, *, session_id, run_id):
        value = self.current.get()
        if value is not None and (session_id, run_id) == (value[1].binding.session_id, value[1].binding.run_id):
            return value[1].binding
        return None
