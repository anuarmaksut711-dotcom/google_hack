"""Compose existing services through real local MCP; no LLM or live IO."""
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from time import monotonic
from types import SimpleNamespace
from uuid import uuid4
import secrets

import httpx
from fastapi.responses import HTMLResponse, Response
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.requests import Request

from ..application.apply import ApplyService
from ..application.preflight import PreflightService, _work, _busy
from ..application.proposals import ProposalService, ProposalBasis
from ..application.verify import VerificationService
from ..domain.models import (Interval, PlanningConstraints, SolveRepairInput,
    AssignmentsSnapshot, CalendarSnapshot, ApplyResult, VerificationResult)
from ..domain.ports import BindingConflict
from ..domain.solver import solve_repair
from ..domain.time_rules import planning_horizon
from ..integrations.assignments import AssignmentsReader, normalized_assignments
from ..integrations.calendar_read import CalendarReader
from ..integrations.calendar_write import CalendarWriter
from ..mcp_service.server import create_complete_app
from ..observability.adapters import (JournalEmitter, ObservationHealth, instrument_assignments,
    instrument_calendar, instrument_apply, instrument_verification)
from ..observability.events import SafeSummary
from ..security.context import (AccessDenied, AuthenticatedCaller, RequestContextFactory, ServerRunBinding)
from ..security.resources import ResourcePolicy
from ..storage.plans import FirestorePlanStore, plan_reference
from ..storage.snapshots import FirestoreSnapshotStore
from ..storage.owned_blocks import FirestoreOwnedBlockRegistry
from ..storage.leases import FirestoreCalendarCoordination
from ..storage.operations import FirestoreExecutionStore
from ..storage.runs import FirestoreRunEventStore
from ..web.auth import VerifiedSignIn
from ..web.approval import FirestoreApprovalStore
from ..web.agent_client import RemoteFrame
from ..web.runs import RecoveryRuns, RunRegistry
from ..web.views.app import create_ui_app, SafeSources
from . import NOTICE, require_demo
from .adapters import demo_config, MemoryDocuments, DemoWorkspace, NoUncertainTakeover


class DemoSystem:
    def __init__(self, *, demo, origin, clock=lambda:datetime.now(UTC)):
        require_demo(demo)
        self.clock, self.origin = clock, origin
        self.config = demo_config()
        self.policy = ResourcePolicy(self.config,environment="local")
        self.documents = MemoryDocuments(self.config,demo=True)
        self.workspace = DemoWorkspace(self.policy,clock(),demo=True)
        self.plans = FirestorePlanStore(self.documents,self.policy)
        self.snapshots = FirestoreSnapshotStore(self.documents,self.policy)
        self.registry = FirestoreOwnedBlockRegistry(self.documents,self.policy)
        self.approvals = FirestoreApprovalStore(self.documents,self.policy,clock=clock)
        self.coordination = FirestoreCalendarCoordination(self.documents,self.policy,clock=clock,recovery_guard=NoUncertainTakeover())
        self.executions = FirestoreExecutionStore(self.documents,self.policy,coordination=self.coordination,approval_guard=self.approvals.transaction_guard())
        self.journal = FirestoreRunEventStore(self.documents,self.policy,clock=clock)
        self.health = ObservationHealth()
        self.emitter = JournalEmitter(self.journal,clock=clock,health=self.health)
        self.assignments = instrument_assignments(AssignmentsReader(self.workspace,self.snapshots,clock=clock),self.emitter)
        self.calendar = instrument_calendar(CalendarReader(self.workspace,self.snapshots,self.registry,clock=clock),self.emitter)
        preflight = PreflightService(self.plans,self.snapshots,self.approvals,self.executions,self.coordination,self.registry,self.assignments,self.calendar,self.policy,clock=clock)
        self.verification = instrument_verification(VerificationService(self.plans,self.snapshots,self.executions,self.registry,self.assignments,self.calendar,self.policy,clock=clock),self.emitter)
        self.executions.verification_guard = self.verification.transaction_guard()
        apply = ApplyService(preflight,CalendarWriter(self.workspace,self.registry,self.coordination,self.plans,clock=clock),clock=clock,verification_reader=self.verification)
        self.apply = instrument_apply(apply,self.emitter)
        self.invocation = ContextVar("demo_internal_invocation",default=None)
        self.secret = secrets.token_urlsafe(32)
        self.bases = {}
        contexts = RequestContextFactory(self.policy,boundary="mcp",clock=clock,
            authenticator=SimpleNamespace(authenticate=self.authenticate_mcp),bindings=SimpleNamespace(resolve=self.binding))
        self.mcp = create_complete_app(contexts=contexts,assignments=self.assignments,calendar=self.calendar,
            apply=self.apply,verification=self.verification,allowed_hosts=["127.0.0.1:8948"],allowed_origins=[],port=8948)
        self.contexts=contexts

    def active(self):
        context = self.invocation.get()
        if context is None or context.boundary != "backend":
            raise AccessDenied("demo_internal_invocation_required")
        self.policy.require_context(context)
        return context

    def authenticate_mcp(self, request, *, boundary, now):
        context = self.active()
        if request.headers.get("authorization") != "Bearer "+self.secret:
            raise AccessDenied("demo_internal_transport_required")
        return AuthenticatedCaller(principal_id=self.config.identities.agent_service_account,
            session_id=context.session_id,run_id=context.trusted.run_id,
            authenticated_at=context.issued_at,expires_at=context.expires_at)

    def binding(self, *, session_id,run_id):
        c=self.active()
        if (session_id,run_id)!=(c.session_id,c.trusted.run_id):
            return None
        return ServerRunBinding(owner_id=c.trusted.owner_id,signin_principal_id=self.config.student.allowed_signin_principal,
            session_id=session_id,run_id=run_id,config_revision=self.config.config_revision,expires_at=c.expires_at)

    async def call(self, context, name, payload, output):
        self.policy.require_context(context)
        await self.journal.register(context)
        token=self.invocation.set(context)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.mcp),
                headers={"Authorization":"Bearer "+self.secret},trust_env=False) as client:
                async with streamable_http_client("http://127.0.0.1:8948/mcp",http_client=client) as (read,write,_):
                    async with ClientSession(read,write) as session:
                        await session.initialize()
                        response=await session.call_tool(name,payload)
            if response.structuredContent is None:
                raise BindingConflict("demo_mcp_result_unavailable")
            if self.health.failed:
                raise BindingConflict("demo_trace_not_confirmed")
            return output.model_validate(response.structuredContent)
        finally:
            self.invocation.reset(token)

    def constraints(self,now=None):
        start,end=planning_horizon(self.clock() if now is None else now)
        return PlanningConstraints(config_revision=self.config.config_revision,timezone=self.config.student.timezone,
            horizon=Interval(start=start,end=end),
            working_windows=(Interval(start=max(start,self.workspace.anchor),end=self.workspace.anchor+timedelta(hours=8)),))

    async def read(self, context,horizon=None):
        payload={"horizon":(horizon or self.constraints().horizon).model_dump(mode="json")}
        a=await self.call(context,"read_assignments",payload,AssignmentsSnapshot)
        c=await self.call(context,"read_calendar",payload,CalendarSnapshot)
        return a,c

    async def resolve(self, context):
        self.policy.require_context(context)
        return self.bases[context.trusted.run_id]

    async def check(self, context, reference):
        plan=await self.plans.get(context,reference.plan_id,reference.version)
        if plan_reference(plan)!=reference:
            raise BindingConflict("demo_plan_changed")
        old,old_c=await self.snapshots.get_for_plan(context,reference)
        horizon=plan.payload.constraints.horizon
        a,c=await self.read(context,horizon)
        if _work(old)!=_work(a):
            raise BindingConflict("demo_assignments_changed")
        external=lambda snapshot:[e for e in snapshot.events if e.campuspulse_marker is None]
        if _busy(external(old_c),horizon)!=_busy(external(c),horizon):
            raise BindingConflict("demo_availability_changed")

    async def stream(self, context,intent):
        if intent.action=="propose":
            expected=await self.plans.get_current(context)
            for _ in range(2):
                constraints=self.constraints()
                a,c=await self.read(context,constraints.horizon)
                now=self.clock()
                if (constraints.horizon.start,constraints.horizon.end)==planning_horizon(now):break
            else: raise BindingConflict("demo_read_crossed_horizon_boundary_retry")
            basis=ProposalBasis(plan_id="demo_"+intent.run_id,version=1,assignments=a.source.reference,
                calendar=c.source.reference,constraints=constraints,now=now,expected_current=expected)
            self.bases[intent.run_id]=basis
            data=SolveRepairInput(assignments=a,calendar=c,constraints=basis.constraints,now=now,plan_id=basis.plan_id,version=basis.version)
            started=monotonic(); result=solve_repair(data)
            await self.emitter.emit(context,layer="backend",call_id="solver_"+intent.intent_id,tool="solve_repair",
                phase="result",status=str(result.status),duration_ms=round((monotonic()-started)*1000),summary=SafeSummary(operation_count=len(result.operations)))
            yield RemoteFrame(run_id=intent.run_id,intent_id=intent.intent_id,kind="terminal",status="candidate",candidate_input=data,candidate=result)
        else:
            applied=await self.call(context,"apply_plan",intent.apply.model_dump(mode="json"),ApplyResult)
            status=str(applied.execution.status)
            if status in ("applied_unverified","verified"):
                await self.call(context,"verify_plan",{"plan_id":intent.plan.plan_id,"version":intent.plan.version},VerificationResult)
                status="applied_unverified" # Only the original backend evidence gate may promote.
            if status not in ("partial","unknown","applied_unverified","verification_failed","stale","unavailable"):
                status="unknown"
            yield RemoteFrame(run_id=intent.run_id,intent_id=intent.intent_id,kind="terminal",status=status)

    async def current(self, context,intent):
        return await self.stored_evidence(context,intent.plan)

    async def stored_evidence(self, context, reference):
        """Private authenticated demo evidence projection, not a new MCP tool.

        Reads the ORIGINAL saved verification proof/ledger; never promotes a
        cached DTO. The synthetic internal identity is bound to active UI scope.
        """
        self.policy.require_context(context)
        token=self.invocation.set(context)
        try:
            request=Request({'type':'http','method':'GET','path':'/private-demo-evidence',
                'headers':[(b'authorization',('Bearer '+self.secret).encode())]})
            with self.contexts.request(request) as active:
                evidence=await self.verification.latest(active,reference)
                return evidence.result
        finally:
            self.invocation.reset(token)

    async def project(self, context, *, run,plan):
        a,c=await self.read(context)
        zone=self.config.student.timezone
        from zoneinfo import ZoneInfo
        fmt=lambda value:value.astimezone(ZoneInfo(zone)).strftime("%d.%m %H:%M")
        assignments=tuple(f"{x.course}: {x.title} · осталось {x.remaining_minutes} мин. · срок {fmt(x.deadline.at)}" for x in normalized_assignments(a))
        calendar=tuple(f"{'Учебный блок' if e.campuspulse_marker else 'Занятие'} · {fmt(e.interval.start)}–{fmt(e.interval.end)}" for e in c.events if e.interval is not None)
        evidence=None
        if run is not None and plan is not None and run.verification_status is not None:
            saved=await self.stored_evidence(context,plan_reference(plan))
            if saved.run_id==run.run_id:
                evidence=saved
        return SafeSources(assignments=(NOTICE,*assignments),calendar=("Тестовый календарь · свежее чтение адаптера",*calendar),verification=evidence)

    def runs(self,auth):
        self.runtime=RecoveryRuns(auth=auth,registry=RunRegistry(self.documents,self.policy,clock=self.clock),remote=self,
            proposals=ProposalService(self.snapshots,self.plans,self.policy,self,clock=self.clock),plans=self.plans,
            approvals=self.approvals,sources=self,journal=self.journal,evidence=self)
        return self.runtime

    def app(self):
        system=self
        class DemoSignIn:
            async def verify(self,token,*,nonce,now):
                if token!="local-demo-signin" or not nonce:
                    raise AccessDenied("demo_signin_rejected")
                return VerifiedSignIn(principal_id=system.config.student.allowed_signin_principal,
                    authenticated_at=now,expires_at=now+timedelta(hours=1))
        app=create_ui_app(policy=self.policy,documents=self.documents,verifier=DemoSignIn(),origin=self.origin,
            environment="local",clock=self.clock,runs_factory=self.runs,projection=self,demo_mode=True)
        @asynccontextmanager
        async def lifespan(app):
            async with self.mcp.app.router.lifespan_context(self.mcp.app):
                yield
        app.router.lifespan_context=lifespan
        @app.get('/demo',response_class=HTMLResponse)
        async def entry():
            return '<!doctype html><html lang="ru"><meta charset="utf-8"><title>CampusPulse · ДЕМО</title><h1>CampusPulse · локальное ДЕМО</h1><p>'+NOTICE+'</p><p>Только тестовый вход. Не вводите Google-пароль или токены.</p><button id="demo-signin">Открыть тестовое расписание</button><p id="result" role="status"></p><script src="/demo-signin.js"></script></html>'
        @app.get('/demo-signin.js')
        async def signin_js():
            return Response("document.getElementById('demo-signin').onclick=async()=>{try{const c=await(await fetch('/auth/challenge')).json();const r=await fetch('/auth/signin',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':c.csrf_token},body:JSON.stringify({credential:'local-demo-signin'})});if(!r.ok)throw Error();location.href='/';}catch{document.getElementById('result').textContent='Тестовый вход не выполнен. Обновите страницу.';}};",media_type='text/javascript')
        @app.middleware('http')
        async def demo_csp(request,call_next):
            response=await call_next(request)
            if request.url.path in ('/demo','/demo-signin.js'):
                response.headers['Content-Security-Policy']="default-src 'none'; script-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"
            return response
        return app


def create_demo_app(*, demo=False,origin="https://127.0.0.1:8947"):
    from urllib.parse import urlsplit
    parts=urlsplit(origin)
    if parts.scheme!="https" or parts.hostname!="127.0.0.1":
        raise ValueError("demo_literal_loopback_https_required")
    system=DemoSystem(demo=demo,origin=origin)
    return system.app(),system
