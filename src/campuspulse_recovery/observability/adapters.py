"""Opt-in instrumentation factories; production entrypoints are not changed.

Domain adapters call original implementations and preserve their guards. ADK
callbacks execute policy FIRST; they are client observations, not MCP auth or
dispatch proof. No arbitrary raw arguments, model text or exceptions are logged.
"""
from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import dataclass
from uuid import uuid4

from ..domain.models import ApplyResult, AssignmentsSnapshot, CalendarSnapshot, canonical_hash
from ..domain.time_rules import instant
from ..security.context import AccessDenied
from .events import SafeSummary, normalized_status, observation


@dataclass
class ObservationHealth:
    """Request-local delivery health, NOT durable progress or success proof.

    Startup/UI must surface delivery failure independently. A failed post-write
    observation cannot undo the write, replace its result, or authorize retry.
    """
    failed: bool = False
    failure_count: int = 0


class JournalEmitter:
    def __init__(self, store, *, clock, health: ObservationHealth):
        self.store, self.clock, self.health = store, clock, health

    async def emit(self, context, *, layer, call_id=None, slot=None, tool=None,
                   phase="result", status="unknown", summary=None,
                   operation_id=None, attempt_id=None, duration_ms=None, code=None,
                   required=False):
        try:
            # Authenticate before even constructing a trusted run identifier.
            request, trusted = self.store._scope(context)
            event_id = "evt_" + canonical_hash(["trace-event-v1", trusted.run_id,
                layer, call_id, slot or phase, operation_id, attempt_id])
            value = observation(event_id=event_id, run_id=trusted.run_id, layer=layer,
                occurred_at=instant(self.clock()), tool=tool, phase=phase, status=status,
                call_id=call_id, operation_id=operation_id, attempt_id=attempt_id,
                duration_ms=duration_ms, summary=summary, code=code)
            def same_delivery(previous):
                # Redelivery timing isn't a new observed tool outcome. Keep the
                # FIRST measured timing; never change semantic result/identity.
                def semantic(item):
                    data = item.model_dump(mode="json")
                    for key in ("occurred_at", "duration_ms", "sequence"):
                        data["event"].pop(key)
                    return data
                return semantic(previous.observation) == semantic(value)
            previous = await self.store.get_record(request, event_id=event_id)
            if previous is not None and same_delivery(previous):
                return previous
            try:
                return await self.store.append_record(request, value)
            except Exception:
                # A concurrent identical callback may have won since get. This
                # rereads one identity only; no second append/business retry.
                previous = await self.store.get_record(request, event_id=event_id)
                if previous is not None and same_delivery(previous):
                    return previous
                raise
        except asyncio.CancelledError:
            raise
        except Exception:
            self.health.failed = True
            self.health.failure_count += 1
            if required:
                # Fixed safe code, with no exception/credential/source details.
                raise RuntimeError("journal_unavailable_before_dispatch") from None
            return None


def summary_for(value):
    """Only typed normalized counts/times. Even validated IDs can contain PII."""
    if isinstance(value, AssignmentsSnapshot):
        return ("complete" if value.source.complete else "incomplete", SafeSummary(
            complete=value.source.complete, item_count=len(value.assignments)))
    if isinstance(value, CalendarSnapshot):
        return ("complete" if value.source.complete else "incomplete", SafeSummary(
            complete=value.source.complete, item_count=len(value.events), horizon=value.source.covered_interval))
    if isinstance(value, ApplyResult):
        old = value.verification
        return ("applied_unverified" if value.execution.status == "verified" else str(value.execution.status), SafeSummary(
            operation_count=len(value.execution.operations),
            completed_count=sum(o.status == "completed" for o in value.execution.operations),
            historical_verification=old is not None,
            verification_status=old.status if old else None,
            verification_at=old.verification_at if old else None,
            # The old run may be a resource-like Identifier. Only opaque ref is
            # safe here; the exact original remains in authenticated evidence.
            verification_run=_safe_run(old.run_id) if old else None))
    from ..domain.models import VerificationResult
    if isinstance(value, VerificationResult):
        return (value.status, SafeSummary(verification_status=value.status,
            verification_at=value.verification_at, verification_run=_safe_run(value.run_id)))
    return "unknown", SafeSummary()


def _safe_run(value):
    # Don't expose arbitrary validated Identifier values from provider results.
    # Journal run_id itself is separately checked against the trusted context.
    from .events import reference
    return reference(value)


async def _domain_call(owner, context, tool, invoke, *, horizon=None):
    policy = owner.clients.policy if hasattr(owner, "clients") else owner.policy
    request = context if hasattr(context, "trusted") else owner._request_context()
    policy.require_context(request)
    if context is not request and context is not request.as_port_context():
        raise AccessDenied("foreign_port_context")
    if request.boundary != "mcp":
        raise AccessDenied("mcp_boundary_required")
    if owner.trace.store.policy is not policy:
        raise AccessDenied("journal_authority_mismatch")
    # This ID is an instrumentation span, explicitly not JSON-RPC/ADK/attempt ID.
    span = "domain_" + uuid4().hex
    start = time.monotonic()
    await owner.trace.emit(request, layer="domain_adapter", call_id=span, tool=tool,
        phase="started", status="started", summary=SafeSummary(horizon=horizon), required=True)
    try:
        value = await invoke()
    except asyncio.CancelledError:
        await owner.trace.emit(request, layer="domain_adapter", call_id=span, tool=tool,
            phase="error", status="unknown" if tool == "apply_plan" else "cancelled", code="cancelled")
        raise
    except Exception:
        await owner.trace.emit(request, layer="domain_adapter", call_id=span, tool=tool,
            phase="error", status="unknown" if tool == "apply_plan" else "unavailable",
            code="execution_unavailable", duration_ms=max(0, round((time.monotonic()-start)*1000)))
        raise
    status, summary = summary_for(value)
    await owner.trace.emit(request, layer="domain_adapter", call_id=span, tool=tool,
        phase="result", status=status, summary=summary,
        duration_ms=max(0, round((time.monotonic()-start)*1000)))
    if isinstance(value, ApplyResult):
        attempts = {}
        try:
            # Original durable T06 ledger read, not ADK function-call IDs,
            # transport ordinals or state.remote_attempts masquerading as IDs.
            ledger = await owner.executions.get_ledger(request, value.execution.request_key)
            attempts = {a.operation_id: a.attempt_id for a in ledger.attempts}
        except asyncio.CancelledError:
            raise
        except Exception:
            owner.trace.health.failed = True
            owner.trace.health.failure_count += 1
        for op in value.execution.operations:
            await owner.trace.emit(request, layer="domain_adapter", call_id=span, tool=tool,
                slot="operation_result", phase="result", status=normalized_status(op.status),
                operation_id=op.operation_id, attempt_id=attempts.get(op.operation_id))
    return value


def instrument_assignments(reader, emitter):
    from ..integrations.assignments import AssignmentsReader
    if type(reader) is not AssignmentsReader:
        raise TypeError("original_assignments_reader_required")
    class InstrumentedAssignments(AssignmentsReader):
        async def read(self, context, horizon):
            return await _domain_call(self, context, "read_assignments",
                lambda: super(InstrumentedAssignments, self).read(context, horizon), horizon=horizon)
    result = InstrumentedAssignments(reader.clients, reader.snapshots, clock=reader.clock,
        request_context=reader._request_context)
    result.trace = emitter
    return result


def instrument_calendar(reader, emitter):
    from ..integrations.calendar_read import CalendarReader
    if type(reader) is not CalendarReader:
        raise TypeError("original_calendar_reader_required")
    class InstrumentedCalendar(CalendarReader):
        async def read(self, context, horizon):
            return await _domain_call(self, context, "read_calendar",
                lambda: super(InstrumentedCalendar, self).read(context, horizon), horizon=horizon)
    result = InstrumentedCalendar(reader.clients, reader.snapshots, reader.registry, clock=reader.clock,
        request_context=reader._request_context, max_pages=reader.max_pages, max_events=reader.max_events)
    result.trace = emitter
    return result


def instrument_apply(service, emitter):
    from ..application.apply import ApplyService
    if type(service) is not ApplyService:
        raise TypeError("original_apply_service_required")
    class InstrumentedApply(ApplyService):
        async def apply(self, context, request):
            return await _domain_call(self, context, "apply_plan",
                lambda: super(InstrumentedApply, self).apply(context, request))
    result = InstrumentedApply(service.preflight, service.writer, clock=service.clock,
        max_attempts=service.max_attempts, deadline_seconds=service.deadline_seconds,
        write_timeout_seconds=service.write_timeout_seconds, verification_reader=service.verification_reader)
    result.trace = emitter
    return result


def instrument_verification(service, emitter):
    from ..application.verify import VerificationService
    if type(service) is not VerificationService:
        raise TypeError("original_verification_service_required")
    class InstrumentedVerification(VerificationService):
        async def verify(self, context, request):
            return await _domain_call(self, context, "verify_plan",
                lambda: super(InstrumentedVerification, self).verify(context, request))
    result = InstrumentedVerification(service.plans, service.snapshots, service.executions,
        service.registry, service.assignments, service.calendar, service.policy, clock=service.clock)
    result.trace = emitter
    return result


async def _invoke(callback, **kwargs):
    value = callback(**kwargs)
    return await value if inspect.isawaitable(value) else value


def instrument_agent(agent, emitter):
    """Compose the original T22 callback slots on an unshared newly built Agent.

    Require exact original callbacks to prevent stacking/reordering. Policy
    continues to own budgets, cancellation, sanitization and denial. No SDK retry
    or toolset behavior changes. The model/session never selects the sink/scope.
    """
    from ..agent import policy
    expected = {"before_agent_callback": policy.before_agent,
        "before_model_callback": policy.before_model, "after_model_callback": policy.after_model,
        "before_tool_callback": policy.before_tool, "after_tool_callback": policy.after_tool,
        "on_tool_error_callback": policy.on_tool_error, "on_model_error_callback": policy.on_model_error}
    if any(getattr(agent, name) is not callback for name, callback in expected.items()):
        raise ValueError("original_policy_callbacks_required")

    def observation_state(callback_context, *, required=False):
        try:
            return policy.current(callback_context)
        except AccessDenied:
            # A safe policy denial must survive absent/foreign authority. Never
            # invent a scope merely to log it. Allowed dispatch still requires
            # active authority and a required pre-dispatch journal observation.
            if required:
                raise
            return None

    async def before_agent(callback_context):
        result = await _invoke(policy.before_agent, callback_context=callback_context)
        if result is None:
            state = policy.current(callback_context)
            await emitter.emit(state.context, layer="adk_callback", call_id="invocation",
                tool=None, phase="started", status="accepted", required=True)
        return result

    async def before_tool(tool, args, tool_context):
        result = await _invoke(policy.before_tool, tool=tool, args=args, tool_context=tool_context)
        # Even a denied request is only a policy decision, not MCP dispatch.
        state = observation_state(tool_context, required=result is None)
        if state is None:
            return result
        await emitter.emit(state.context, layer="adk_callback", call_id=tool_context.function_call_id,
            tool=tool.name if tool.name in policy.PUBLIC_TOOLS else None,
            phase="decision" if result is not None else "started",
            status="denied" if result is not None else "accepted",
            code="policy_rejected" if result is not None else None,
            required=result is None)
        return result

    async def after_tool(tool, args, tool_context, tool_response):
        result = await _invoke(policy.after_tool, tool=tool, args=args,
            tool_context=tool_context, tool_response=tool_response)
        state = observation_state(tool_context)
        if state is None:
            return result
        # after_tool has already validated and sanitized solver args/results.
        # Read typed state, never raw tool_response, args or model output.
        typed = {"read_assignments": state.assignments, "read_calendar": state.calendar,
                 "apply_plan": state.applied, "verify_plan": state.verification}.get(tool.name)
        _, summary = summary_for(typed)
        denied = isinstance(result, dict) and result.get("code") == "policy_rejected"
        await emitter.emit(state.context, layer="adk_callback", call_id=tool_context.function_call_id,
            tool=tool.name, phase="decision" if denied else "result",
            status="denied" if denied else normalized_status(state.status), summary=summary,
            duration_ms=max(0, round((time.monotonic()-state.tool_started)*1000)))
        return result

    async def on_tool_error(tool, args, tool_context, error):
        result = await _invoke(policy.on_tool_error, tool=tool, args=args,
            tool_context=tool_context, error=error)
        state = observation_state(tool_context)
        if state is None:
            return result
        await emitter.emit(state.context, layer="client_observation", call_id=tool_context.function_call_id,
            tool=tool.name, phase="error", status="unknown" if tool.name == "apply_plan" else "unavailable",
            code="execution_unavailable")
        return result

    agent.before_agent_callback = before_agent
    agent.before_tool_callback = before_tool
    agent.after_tool_callback = after_tool
    agent.on_tool_error_callback = on_tool_error
    return agent


def build_instrumented_recovery_agent(config, emitter, *, headers=None, local_transport=None):
    from ..agent.agent import build_recovery_agent
    return instrument_agent(build_recovery_agent(config, headers=headers, local_transport=local_transport), emitter)


async def run_instrumented_recovery(runner, emitter, *, new_message):
    """Backend invocation seam; run registration is a separate backend step."""
    from ..agent import policy
    from ..agent.agent import run_recovery
    state = policy.current()
    span = "backend_" + uuid4().hex
    await emitter.emit(state.context, layer="backend", call_id=span,
        phase="started", status="started", required=True)
    try:
        result = await run_recovery(runner, new_message=new_message)
    except asyncio.CancelledError:
        await emitter.emit(state.context, layer="backend", call_id=span,
            phase="error", status=state.status if state.apply_dispatched else "cancelled", code="cancelled")
        raise
    await emitter.emit(state.context, layer="backend", call_id=span,
        phase="result", status=normalized_status(result.status))
    return result
