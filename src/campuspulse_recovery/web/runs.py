"""Opt-in bounded recovery flow; NOT wired into T16's default app.

Durable intent mapping/claim precedes agent IO. Unknown outcomes never authorize
redispatch. Production runtime binding, proposal basis/index, evidence projection
and startup composition remain explicit required integrations, not fake defaults.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import time
from typing import Literal, Protocol
from uuid import uuid4

from fastapi import APIRouter, Request

from ..application.proposals import ProposalService
from ..domain.models import (
    ApplyPlanInput, Contract, Instant, PlanReference, VerificationResult, canonical_hash,
)
from ..domain.ports import BindingConflict, PortError
from ..observability.events import TechnicalId, observation
from ..security.context import (
    AccessDenied, AuthenticatedCaller, RequestContextFactory, ServerRunBinding,
)
from ..storage.plans import _Repository, _decode, _encode, _key, plan_reference
from .agent_client import AgentIntent, StreamLimits, consume


class StartRequest(Contract):
    request_key: TechnicalId


class ObservationHealthUnknown(PortError):
    """Health commit/readback could not be confirmed; no optimistic response."""


class ContinueRequest(StartRequest):
    plan: PlanReference


class EvidenceSummary(Contract):
    """Bounded technical projection; source bodies/URLs stay in evidence store."""
    status: Literal["verified", "verification_failed", "applied_unverified", "unknown"]
    verification_at: Instant | None
    run_id: TechnicalId


class RunState(Contract):
    binding: ServerRunBinding
    scope: str
    intent: AgentIntent
    created_at: Instant
    status: Literal["ready", "unknown", "proposed", "no_change", "needs_input",
                    "infeasible", "partial", "applied_unverified", "verification_failed",
                    "stale", "unavailable", "needs_review", "verified"] = "ready"
    claimed: bool = False
    claimed_at: Instant | None = None
    settled: bool = False
    plan: PlanReference | None = None
    verification: EvidenceSummary | None = None


    observation_available: bool = False
    observation_failed: bool = False


class RunView(Contract):
    run_id: TechnicalId
    intent_id: TechnicalId
    status: str
    settled: bool
    plan: PlanReference | None
    verification_status: Literal["verified", "verification_failed", "applied_unverified", "unknown"] | None
    verification_at: Instant | None
    verification_run_id: TechnicalId | None


    verified_success: bool
    observation_available: bool


class EvidenceProjection(Protocol):
    async def current(self, context, intent: AgentIntent) -> VerificationResult | None:
        """Private authenticated evidence path, not T20's MCP-only API. Return
        actual evidence for THIS invocation; historical/replay evidence is not
        fresh success. No production implementation is provided here.
        """
        ...


class SourceGuard(Protocol):
    async def check(self, context, reference: PlanReference) -> None:
        """Existing authoritative server basis/guard; no backend Google IO.
        Detect stale source before continuation. MCP retains final preflight.
        """
        ...


class RunRegistry(_Repository):
    """Small document/CAS registry, reusable across backend instances.

    Each user/session/key maps immutably to an intent. Exact apply uniqueness is
    additionally calendar/config/plan-version scoped across request keys AND
    sessions. A different session cannot adopt a prior session's intent.
    Retention is bounded by sign-in expiry; physical TTL/global admission remain
    deployment concerns. No external calls occur in transaction callbacks.
    """
    def __init__(self, documents, policy, *, clock):
        super().__init__(documents, policy)
        self.clock = clock

    def scope(self, session):
        b, c = session.binding, self.policy.config
        return _key(b.owner_id, c.student.calendar_id, b.config_revision,
                    b.session_id, session.principal_id, session.expires_at.isoformat())

    def _validate(self, context, session, state):
        self._trusted(context, writer="backend")
        self.policy.require_session(context, session.binding.session_id)
        self.policy.require_binding(state.binding, session_id=session.binding.session_id,
                                    run_id=state.intent.run_id, now=self.clock())
        if (state.scope != self.scope(session) or state.binding.run_id != state.intent.run_id
                or state.binding.signin_principal_id != session.principal_id
                or state.binding.expires_at != session.expires_at):
            raise AccessDenied("run_session_binding_mismatch")
        return state

    def path(self, run_id):
        from pydantic import TypeAdapter
        TypeAdapter(TechnicalId).validate_python(run_id)
        return "cp_web_runs/" + _key(run_id)

    async def bind(self, context, session, request_key, *, plan=None):
        self._trusted(context, writer="backend")
        scope = self.scope(session)
        action = "propose" if plan is None else "apply"
        wire_plan = None if plan is None else plan.model_dump(mode="json")
        payload = canonical_hash([action, wire_plan])
        keypath = "cp_web_run_keys/" + _key(scope, request_key)
        run_id, intent_id = "run_" + uuid4().hex, "int_" + uuid4().hex
        apply = None if plan is None else ApplyPlanInput(plan_id=plan.plan_id, version=plan.version,
                                                        request_key="apply_" + uuid4().hex)
        proposed = RunState(binding=ServerRunBinding(
            **{**session.binding.model_dump(), "run_id": run_id}), scope=scope,
            intent=AgentIntent(run_id=run_id, intent_id=intent_id, action=action, plan=plan, apply=apply),
            created_at=self.clock(), plan=plan)
        exactpath = None if plan is None else "cp_web_apply_intents/" + _key(
            session.binding.owner_id, self.policy.config.student.calendar_id,
            session.binding.config_revision, wire_plan)

        async def bind(tx):
            old = await tx.get(keypath)
            exact = None if exactpath is None else await tx.get(exactpath)
            pointer = old or exact
            if old is not None and (set(old) != {"run_id", "payload"} or old["payload"] != payload):
                raise BindingConflict("request_intent_conflict")
            if pointer is not None:
                if set(pointer) != {"run_id", "payload"} or pointer["payload"] != payload:
                    raise BindingConflict("apply_intent_conflict")
                state = _decode(await tx.get(self.path(pointer["run_id"])), "web_run", RunState)
                self._validate(context, session, state)
                if state.intent.action != action or state.intent.plan != plan:
                    raise BindingConflict("stored_intent_payload_mismatch")
                if old is not None and exact is not None and old != exact:
                    raise BindingConflict("intent_pointer_conflict")
                if old is None:
                    tx.create(keypath, pointer)
                return state
            self._validate(context, session, proposed)
            pointer = {"run_id": run_id, "payload": payload}
            tx.create(keypath, pointer)
            tx.create(self.path(run_id), _encode("web_run", proposed))
            if exactpath is not None:
                tx.create(exactpath, pointer)
            return proposed

        return await self.documents.transact(bind)

    async def get(self, context, session, run_id):
        self._trusted(context, writer="backend")
        state = _decode(await self.documents.get(self.path(run_id)), "web_run", RunState)
        return self._validate(context, session, state)

    async def claim(self, context, session, state):
        async def claim(tx):
            latest = _decode(await tx.get(self.path(state.intent.run_id)), "web_run", RunState)
            self._validate(context, session, latest)
            if latest.intent != state.intent:
                raise BindingConflict("intent_changed")
            if latest.claimed:
                return latest, False
            value = RunState.model_validate({**latest.model_dump(), "claimed": True,
                "claimed_at": self.clock(), "status": "unknown", "observation_available": False})
            tx.set(self.path(value.intent.run_id), _encode("web_run", value))
            return value, True
        return await self.documents.transact(claim)

    async def finish(self, context, session, state):
        async def finish(tx):
            latest = _decode(await tx.get(self.path(state.intent.run_id)), "web_run", RunState)
            self._validate(context, session, latest)
            self._validate(context, session, state)
            if latest.intent != state.intent or not latest.claimed:
                raise BindingConflict("unclaimed_result")
            if latest.settled:
                health = {"observation_available", "observation_failed"}
                if latest.model_dump(exclude=health) != state.model_dump(exclude=health):
                    raise BindingConflict("immutable_result_conflict")
                return latest
            failed = latest.observation_failed or state.observation_failed
            result = RunState.model_validate({**state.model_dump(), "observation_failed": failed,
                "observation_available": state.observation_available and not failed})
            tx.set(self.path(state.intent.run_id), _encode("web_run", result))
            return result
        return await self.documents.transact(finish)

    async def observation_unavailable(self, context, session, state, *, checkpoint):
        """Monotonic health-only transaction, never a business-state replacement.

        Re-read the latest committed result and patch only health. In particular
        a concurrent/ambiguously committed settled or partial outcome survives.
        The original WebAuth checkpoint rechecks the durable session after the
        awaited transaction read and immediately before the write.
        """
        await checkpoint()

        async def degrade(tx):
            latest = _decode(await tx.get(self.path(state.intent.run_id)), "web_run", RunState)
            self._validate(context, session, latest)
            if latest.intent != state.intent or not latest.claimed:
                raise BindingConflict("observation_intent_mismatch")
            await checkpoint()
            self._validate(context, session, latest)
            if latest.observation_failed and not latest.observation_available:
                return latest
            degraded = RunState.model_validate({**latest.model_dump(),
                "observation_available": False, "observation_failed": True})
            tx.set(self.path(state.intent.run_id), _encode("web_run", degraded))
            return degraded

        try:
            saved = await self.documents.transact(degrade)
        except AccessDenied:
            raise
        except Exception:


            await checkpoint()
            try:
                saved = await self.get(context, session, state.intent.run_id)
            except AccessDenied:
                raise
            except Exception:
                raise ObservationHealthUnknown("observation_health_outcome_unknown") from None
            await checkpoint()
            if not saved.observation_failed or saved.observation_available:
                raise ObservationHealthUnknown("observation_health_outcome_unknown") from None
        await checkpoint()
        return saved


class _CheckedPort:
    """Compose original T15 ports, preserving their implementations/authority."""
    def __init__(self, target, checkpoint):
        self.target, self.checkpoint = target, checkpoint

    def __getattr__(self, name):
        method = getattr(self.target, name)
        async def checked(*args, **kwargs):
            await self.checkpoint()
            result = await method(*args, **kwargs)
            await self.checkpoint()
            return result
        return checked


class RecoveryRuns:
    def __init__(self, *, auth, registry: RunRegistry, remote, proposals: ProposalService,
                 plans, approvals, sources: SourceGuard, journal, evidence: EvidenceProjection | None = None,
                 limits: StreamLimits | None = None):
        if sources is None or proposals is None or journal is None:
            raise ValueError("explicit_authoritative_integrations_required")
        self.auth, self.registry, self.remote = auth, registry, remote
        self.proposals, self.plans, self.approvals = proposals, plans, approvals
        self.sources, self.journal, self.evidence = sources, journal, evidence
        self.policy, self.clock = auth.policy, auth.clock
        self.limits = limits or StreamLimits()
        if any(getattr(x, "policy", self.policy) is not self.policy
               for x in (registry, proposals, plans, approvals, journal)):
            raise ValueError("shared_policy_required")
        if any(getattr(x, "documents", registry.documents) is not registry.documents
               for x in (auth, plans, approvals, journal)):
            raise ValueError("shared_documents_required")

    async def _fresh(self, request, credential, context=None, *, mutation=False):
        if context is not None:
            self.policy.require_context(context)
        current = await self.auth.load(request, mutation=mutation)
        if current.session != credential.session:
            raise AccessDenied("session_changed")
        if context is not None:
            self.policy.require_context(context)

    @contextmanager
    def _scope(self, credential, state):

        with self.auth.request_scope(credential) as original:
            self.registry._validate(original, credential.session, state)
            session, binding = credential.session, state.binding

            class Authenticator:
                def authenticate(inner, actual, *, boundary, now):
                    if actual is not credential or boundary != "backend":
                        raise AccessDenied("server_session_required")
                    original.require_active()
                    return AuthenticatedCaller(principal_id=session.principal_id,
                        session_id=binding.session_id, run_id=binding.run_id,
                        authenticated_at=session.authenticated_at, expires_at=session.expires_at)

            class Bindings:
                def resolve(inner, *, session_id, run_id):
                    return binding if (session_id, run_id) == (binding.session_id, binding.run_id) else None

            factory = RequestContextFactory(self.policy, boundary="backend", authenticator=Authenticator(),
                                            bindings=Bindings(), clock=self.clock)
            with factory.request(credential) as context:
                yield context

    async def _allowed(self, request, credential, context, intent):
        async def fresh():
            await self._fresh(request, credential, context, mutation=True)
        await fresh()
        if intent.action == "apply":
            plan = await self.plans.get(context, intent.plan.plan_id, intent.plan.version)
            await fresh()
            if plan_reference(plan) != intent.plan:
                raise BindingConflict("exact_plan_changed")
            current = await self.plans.get_current(context)
            await fresh()
            if current != intent.plan:
                raise BindingConflict("plan_no_longer_current")
            approval = await self.approvals.get(context, intent.plan)
            await fresh()
            if approval is None or not approval.active_at(self.clock()):
                raise AccessDenied("exact_approval_required")
            await self.sources.check(context, intent.plan)
            await fresh()

            approval = await self.approvals.get(context, intent.plan)
            await fresh()
            if approval is None or not approval.active_at(self.clock()):
                raise AccessDenied("exact_approval_required")

    @staticmethod
    def _view(state):
        evidence = state.verification
        return RunView(run_id=state.intent.run_id, intent_id=state.intent.intent_id,
            status=state.status, settled=state.settled, plan=state.plan,
            verification_status=None if evidence is None else evidence.status,
            verification_at=None if evidence is None else evidence.verification_at,
            verification_run_id=None if evidence is None else evidence.run_id,
            verified_success=state.status == "verified" and evidence is not None and evidence.status == "verified",
            observation_available=state.observation_available)

    async def _project_evidence(self, context, state, terminal, values, fresh):
        """Evidence outage cannot erase a completed/partial apply observation."""
        if state.intent.action != "apply" or self.evidence is None:
            return
        try:
            evidence = await self.evidence.current(context, state.intent)
            await fresh()
            if evidence is None:
                return
            evidence = VerificationResult.model_validate(evidence)
            if (evidence.plan != state.intent.plan or evidence.run_id != state.intent.run_id
                    or state.claimed_at is None or evidence.attempted_at < state.claimed_at
                    or evidence.attempted_at > self.clock()
                    or (evidence.verification_at is not None and evidence.verification_at > self.clock())):
                raise BindingConflict("verification_invocation_mismatch")
            if evidence.calendar_source is not None:
                self.policy.require_source(context, evidence.calendar_source.reference)
            if evidence.status in ("verified", "verification_failed"):
                saved = await self.plans.get(context, state.intent.plan.plan_id, state.intent.plan.version)
                await fresh()
                if (plan_reference(saved) != state.intent.plan or evidence.checked_operations !=
                        tuple(op.operation_id for op in saved.payload.operations)):
                    raise BindingConflict("verification_operation_coverage_mismatch")
            values["verification"] = EvidenceSummary(status=evidence.status,
                verification_at=evidence.verification_at, run_id=evidence.run_id)
            if terminal.status not in ("partial", "unknown", "stale", "unavailable"):
                values["status"] = evidence.status
        except asyncio.CancelledError:
            raise
        except Exception:
            await fresh()
            values["verification"] = EvidenceSummary(status="unknown", verification_at=None,
                                                       run_id=state.intent.run_id)

    async def start(self, request, value: StartRequest | ContinueRequest):
        credential = await self.auth.load(request, mutation=True)
        with self.auth.request_scope(credential) as initial:
            plan = value.plan if isinstance(value, ContinueRequest) else None

            state = await self.registry.bind(initial, credential.session, value.request_key, plan=plan)
            await self._fresh(request, credential, initial, mutation=True)
        with self._scope(credential, state) as context:
            if not state.claimed:
                await self._allowed(request, credential, context, state.intent)
            await self._fresh(request, credential, context, mutation=True)
            state, claimed = await self.registry.claim(context, credential.session, state)
            await self._fresh(request, credential, context, mutation=True)
            if not claimed:

                return self._view(state)
            return await self._invoke(request, credential, context, state)

    async def _invoke(self, request, credential, context, state):
        started = time.monotonic()
        async def fresh():
            await self._fresh(request, credential, context, mutation=True)

        async def work():

            try:
                await self.journal.register(context)
                await fresh()
                await self.journal.append_record(context, observation(event_id="evt_" + uuid4().hex,
                    run_id=state.intent.run_id, layer="backend", occurred_at=self.clock(),
                    phase="started", status="started"))
                await fresh()
            except asyncio.CancelledError:
                raise
            except AccessDenied:
                raise
            except Exception:
                await self.registry.observation_unavailable(context, credential.session, state, checkpoint=fresh)
                raise
            await self._allowed(request, credential, context, state.intent)
            return await consume(self.remote, context, state.intent, self.limits, checkpoint=fresh)

        stopping = False

        async def disconnected():


            while not stopping:
                if await request.is_disconnected():
                    raise ConnectionError("browser_disconnected")
                await asyncio.sleep(0.05)

        task = watcher = None
        try:
            async with asyncio.timeout(self.limits.seconds):
                task = asyncio.create_task(work())
                watcher = asyncio.create_task(disconnected())
                done, _ = await asyncio.wait((task, watcher), return_when=asyncio.FIRST_COMPLETED)
                if watcher in done:
                    await watcher
                terminal = await task
                await fresh()
                values = {**state.model_dump(), "status": terminal.status, "settled": True}
                if state.intent.action == "propose" and terminal.status == "candidate":

                    original = self.proposals
                    proposals = ProposalService(_CheckedPort(original.snapshots, fresh),
                        _CheckedPort(original.plans, fresh), self.policy,
                        _CheckedPort(original.basis, fresh), clock=original.clock)
                    outcome = await proposals.propose(context, candidate_input=terminal.candidate_input,
                                                       candidate_result=terminal.candidate)
                    await fresh()
                    values["plan"] = None if outcome.plan is None else plan_reference(outcome.plan)
                    values["status"] = ("proposed" if outcome.plan is not None else
                        "no_change" if outcome.result.status in ("feasible", "optimal", "no_change")
                        else str(outcome.result.status))
                await self._project_evidence(context, state, terminal, values, fresh)
                try:
                    await self.journal.append_record(context, observation(event_id="evt_" + uuid4().hex,
                        run_id=state.intent.run_id, layer="backend", occurred_at=self.clock(),
                        phase="result", status=values["status"] if values["status"] != "proposed" else "candidate",
                        duration_ms=max(0, int((time.monotonic() - started) * 1000))))
                    await fresh()
                    values["observation_available"] = True
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await fresh()
                    await self.registry.observation_unavailable(context, credential.session, state, checkpoint=fresh)
                    values["observation_available"] = False
                    values["observation_failed"] = True
                result = RunState.model_validate(values)
                await fresh()
                saved = await self.registry.finish(context, credential.session, result)
                await fresh()
                return self._view(saved)
        except asyncio.CancelledError:

            raise
        except AccessDenied:
            raise
        except ObservationHealthUnknown:
            raise
        except Exception:


            await fresh()
            saved = await self.registry.get(context, credential.session, state.intent.run_id)
            await fresh()
            return self._view(saved)
        finally:
            stopping = True
            for pending in (task, watcher):
                if pending is not None and not pending.done():
                    pending.cancel()
            if task is not None or watcher is not None:
                await asyncio.gather(*(t for t in (task, watcher) if t is not None), return_exceptions=True)

    async def read(self, request, run_id):
        credential = await self.auth.load(request)
        with self.auth.request_scope(credential) as initial:
            state = await self.registry.get(initial, credential.session, run_id)
            await self._fresh(request, credential, initial)
        return self._view(state)

    async def trace(self, request, run_id, *, after=0):
        credential = await self.auth.load(request)
        with self.auth.request_scope(credential) as initial:
            state = await self.registry.get(initial, credential.session, run_id)
            await self._fresh(request, credential, initial)
        with self._scope(credential, state) as context:
            records = await self.journal.records_after(context, sequence=after)
            await self._fresh(request, credential, context)
            return records


def create_runs_router(service: RecoveryRuns):
    """Attach only via separately authorized startup wiring using SAME WebAuth.

    Existing T16 app keeps auth in closures. This factory deliberately does not
    modify it, duplicate login, or expose a second backend. Existing app's safe
    exception/security middleware is required when including this router.
    """
    from .app import body

    router = APIRouter()

    @router.post("/api/runs")
    async def start(request: Request):
        value = await body(request, StartRequest)
        return (await service.start(request, value)).model_dump(mode="json")

    @router.post("/api/runs/continue")
    async def continuation(request: Request):
        value = await body(request, ContinueRequest)
        return (await service.start(request, value)).model_dump(mode="json")

    @router.get("/api/runs/{run_id}")
    async def read(request: Request, run_id: str):
        if request.query_params:
            raise ValueError("unexpected_query")
        return (await service.read(request, run_id)).model_dump(mode="json")

    @router.get("/api/runs/{run_id}/trace")
    async def trace(request: Request, run_id: str):
        if (set(request.query_params) - {"after"} or len(request.query_params.getlist("after")) > 1):
            raise ValueError("invalid_trace_selector")
        raw = request.query_params.get("after", "0")
        if not raw.isascii() or not raw.isdigit() or len(raw) > 3:
            raise ValueError("invalid_trace_cursor")
        records = await service.trace(request, run_id, after=int(raw))
        return {"records": [r.model_dump(mode="json") for r in records],
                "after": int(raw), "next": int(raw) if not records else records[-1].observation.event.sequence}

    return router
