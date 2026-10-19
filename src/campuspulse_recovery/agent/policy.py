"""Fail-closed per-invocation policy, independent of model and ADK session state.

The Python scope below is a trusted startup/invocation seam, NOT a production
authenticator or a wire API. Production binding/basis/approval adapters are still
required. MCP independently enforces durable exact-version approval. Nothing in
this module persists, approves, mutates Calendar or replaces ProposalService.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
import time

from google.adk.models.llm_response import LlmResponse
from google.genai import types

from ..domain.models import (
    ApplyPlanInput, ApplyResult, AssignmentsSnapshot, CalendarSnapshot,
    PlanReference, PlanningConstraints, ReadAssignmentsInput, ReadCalendarInput,
    RepairResult, SolveRepairInput, VerificationResult, VerifyPlanInput,
)
from ..domain.time_rules import instant, planning_horizon
from ..security.context import AccessDenied, TrustedRequestContext
from ..security.resources import ResourcePolicy

REMOTE_TOOLS = ("read_assignments", "read_calendar", "apply_plan", "verify_plan")
PUBLIC_TOOLS = (*REMOTE_TOOLS, "solve_repair")
MAX_LLM_CALLS = 8
MAX_TOOL_CALLS = 8
MAX_REMOTE_ATTEMPTS = 10
RUN_TIMEOUT_SECONDS = 45


@dataclass(repr=False)
class RecoveryState:
    context: TrustedRequestContext
    policy: ResourcePolicy
    mode: str
    constraints: PlanningConstraints
    now: datetime
    proposal_id: str
    proposal_version: int
    allowed_plan: PlanReference | None
    allowed_apply: ApplyPlanInput | None
    status: str = "not_started"
    llm_calls: int = 0
    tool_calls: int = 0
    remote_attempts: int = 0
    pending: str | None = None
    assignments: AssignmentsSnapshot | None = None
    calendar: CalendarSnapshot | None = None
    candidate_input: SolveRepairInput | None = None
    candidate: RepairResult | None = None
    applied: ApplyResult | None = None
    verification: VerificationResult | None = None
    apply_dispatched: bool = False
    verify_calls: int = 0
    verify_started_at: datetime | None = None
    stale: bool = False
    read_counts: dict = field(default_factory=dict)
    observations: list[dict] = field(default_factory=list)
    started: float = field(default_factory=time.monotonic)
    tool_started: float = 0

    def __reduce__(self):
        raise TypeError("invocation_authority_is_not_serializable")

    def require_active(self, callback_context=None):
        trusted = self.policy.require_context(self.context)
        if callback_context is not None and (
            callback_context.user_id != trusted.owner_id
            or callback_context.session.id != self.context.session_id
        ):
            raise AccessDenied("adk_invocation_binding_mismatch")
        return trusted

    def request(self):
        if self.assignments is None or self.calendar is None:
            raise ValueError("source_reads_required")
        return SolveRepairInput(assignments=self.assignments, calendar=self.calendar,
            constraints=self.constraints, now=self.now, plan_id=self.proposal_id,
            version=self.proposal_version)


_CURRENT: ContextVar[RecoveryState | None] = ContextVar("campuspulse_recovery", default=None)


def current(callback_context=None) -> RecoveryState:
    value = _CURRENT.get()
    if value is None:
        raise AccessDenied("trusted_invocation_required")
    value.require_active(callback_context)
    return value


@contextmanager
def backend_invocation(context: TrustedRequestContext, policy: ResourcePolicy, *,
                       mode: str, constraints: PlanningConstraints, now: datetime,
                       proposal_id: str, proposal_version: int,
                       allowed_plan: PlanReference | None = None,
                       allowed_apply: ApplyPlanInput | None = None):
    """Called only by trusted server code after resolving its invocation record.

    Accept an active backend/agent capability, never JSON/state/prompt bindings.
    constraints/now/proposal identity must be loaded from server policy/run basis.
    allowed_* must be loaded from the backend's exact saved approval channel;
    supplying them here does NOT attest consent. MCP still checks actual consent.
    No implementation of that production channel is claimed by this seam.
    """
    trusted = policy.require_context(context)
    if context.boundary not in ("backend", "agent") or _CURRENT.get() is not None:
        raise AccessDenied("trusted_invocation_scope_required")
    if mode not in ("deadlines", "recovery", "apply"):
        raise ValueError("invalid_recovery_mode")
    constraints = PlanningConstraints.model_validate(constraints)
    now = instant(now)
    if ((constraints.horizon.start, constraints.horizon.end) != planning_horizon(now)
            or constraints.config_revision != trusted.config_revision
            or constraints.timezone != trusted.timezone):
        raise AccessDenied("server_planning_basis_mismatch")

    VerifyPlanInput(plan_id=proposal_id, version=proposal_version)
    if allowed_plan is not None:
        allowed_plan = PlanReference.model_validate(allowed_plan)
    if allowed_apply is not None:
        allowed_apply = ApplyPlanInput.model_validate(allowed_apply)
        if (mode != "apply" or allowed_plan is None or
                (allowed_apply.plan_id, allowed_apply.version) !=
                (allowed_plan.plan_id, allowed_plan.version)):
            raise AccessDenied("exact_saved_reference_required")
    state = RecoveryState(context, policy, mode, constraints, now, proposal_id,
                          proposal_version, allowed_plan, allowed_apply)
    token = _CURRENT.set(state)
    try:
        yield state
    finally:
        _CURRENT.reset(token)


def safe_error(code):
    return {"status": "blocked", "code": code}


def final_text(state: RecoveryState | None) -> str:
    if state is None:
        return "status=blocked: требуется доверенная привязка запуска backend."
    prefix = f"run_id={state.context.trusted.run_id}; status={state.status}. "
    if state.status == "verified" and state.verification is not None:
        v = state.verification
        links = " ".join(e.event_link for e in v.observed_events if e.event_link)
        return prefix + f"Проверено чтением Calendar в {v.verification_at.isoformat()}; verification_run={v.run_id}. " + links
    messages = {
        "deadlines": "Сроки прочитаны; изменения календаря не выполнялись.",
        "no_change": "По прочитанным данным и заданному горизонту изменения не нужны.",
        "needs_input": "Нужны явные оценки оставшейся работы или ограничения времени.",
        "infeasible": "В заданном горизонте обнаружен дефицит времени; календарь не изменён.",
        "candidate": "Кандидат рассчитан. Ожидается backend ingestion и подтверждение точной версии в UI.",
        "stale": "Источники изменились; нужны свежие данные, новый план и подтверждение.",
        "partial": "Выполнена часть операций. Сохранённый прогресс требует сверки; полный успех не подтверждён.",
        "unknown": "Исход не определён. Нужна сверка durable ledger и Calendar; повтор записи запрещён.",
        "applied_unverified": "Применение ещё не подтверждено свежей успешной проверкой.",
        "verification_failed": "Свежая проверка обнаружила несоответствие; результат не подтверждён.",
        "needs_review": "Нужна проверка исходных данных.",
        "budget_exhausted": "Достигнут предел вызовов; результат не подтверждён.",
        "blocked": "Действие заблокировано policy; подтверждённого результата нет.",
        "unavailable": "Источник или runtime недоступен; результат не подтверждён.",
        "not_started": "Недостаточно наблюдений для результата.",
    }
    suffix = messages.get(state.status, "Подтверждённого результата нет.")
    if state.status == "deadlines" and state.assignments is not None:
        suffix += " " + "; ".join(f"{a.assignment_id}: {a.deadline.at.isoformat() if a.deadline else 'срок не указан'}"
                                   for a in state.assignments.assignments)
    if state.candidate is not None:
        deficits = [f"{f.deficit_minutes} мин до {f.deadline.isoformat()}"
                    for f in state.candidate.findings if f.deficit_minutes and f.deadline]
        if deficits:
            suffix += " Дефицит: " + "; ".join(deficits) + "."
    if state.applied is not None:
        suffix += " Операции: " + ", ".join(f"{o.operation_id}={o.status}" for o in state.applied.execution.operations) + "."
        if state.applied.verification is not None:
            old = state.applied.verification
            suffix += f" Историческая проверка: {old.verification_at.isoformat() if old.verification_at else 'нет подтверждения'}, run={old.run_id}; текущую проверку не заменяет."
    return prefix + suffix


def response(state=None):
    return LlmResponse(content=types.Content(role="model", parts=[types.Part(text=final_text(state))]))


def before_agent(callback_context):
    try:
        state = current(callback_context)
        config = callback_context.run_config
        if config is None or not 0 < config.max_llm_calls <= MAX_LLM_CALLS:
            raise AccessDenied("bounded_run_config_required")
        return None
    except (AccessDenied, ValueError):
        return response().content


def before_model(callback_context, llm_request):
    state = current(callback_context)
    if state.llm_calls >= MAX_LLM_CALLS or state.tool_calls >= MAX_TOOL_CALLS:

        if state.status not in ("unknown", "partial", "verification_failed", "applied_unverified"):
            state.status = "budget_exhausted"
        return response(state)
    state.llm_calls += 1
    return None


def after_model(callback_context, llm_response):
    state = current(callback_context)
    parts = llm_response.content.parts if llm_response.content else []
    calls = [p.function_call for p in parts or [] if p.function_call]
    if not calls:
        return response(state)
    if len(calls) != 1 or calls[0].name not in PUBLIC_TOOLS:
        if state.status not in ("partial", "unknown", "verification_failed", "applied_unverified"):
            state.status = "blocked"
        return response(state)
    call = calls[0]
    try:
        normalized = validate_call(state, call.name, call.args or {})
    except (ValueError, TypeError, AccessDenied):
        if state.status not in ("partial", "unknown", "verification_failed", "applied_unverified", "stale"):
            state.status = "blocked"
        return response(state)



    visible = {"request": {}} if call.name == "solve_repair" else normalized
    return LlmResponse(content=types.Content(role="model", parts=[types.Part(
        function_call=types.FunctionCall(id=call.id, name=call.name, args=visible))]))


def validate_call(state, name, args):
    state.require_active()
    if state.tool_calls >= MAX_TOOL_CALLS or state.pending is not None:
        raise ValueError("tool_budget_or_inflight")
    if name.startswith("read_"):
        if state.mode == "deadlines" and name != "read_assignments":
            raise ValueError("deadlines_only")
        if (state.apply_dispatched and not state.stale) or state.candidate is not None:
            raise ValueError("new_invocation_required")
        if state.read_counts.get(name, 0) >= 2:
            raise ValueError("read_budget")
        dto = (ReadAssignmentsInput if name == "read_assignments" else ReadCalendarInput).model_validate(args)
        if dto.horizon != state.constraints.horizon:
            raise ValueError("server_horizon_required")
        return dto.model_dump(mode="json")
    if name == "solve_repair":
        if state.mode == "deadlines" or (state.apply_dispatched and not state.stale) or state.candidate is not None:
            raise ValueError("solver_phase")
        if set(args) != {"request"} or not isinstance(args["request"], dict):
            raise ValueError("invalid_solver_call")
        state.request()
        return {"request": {}}
    if name == "apply_plan":
        dto = ApplyPlanInput.model_validate(args)
        if (state.mode != "apply" or state.allowed_apply is None
                or dto != state.allowed_apply or state.apply_dispatched or state.stale
                or state.candidate is not None):
            raise AccessDenied("exact_backend_approval_channel_required")
        return dto.model_dump(mode="json")
    if name == "verify_plan":
        dto = VerifyPlanInput.model_validate(args)
        if (state.allowed_plan is None or not state.apply_dispatched or state.verify_calls >= 1
                or (dto.plan_id, dto.version) != (state.allowed_plan.plan_id, state.allowed_plan.version)):
            raise AccessDenied("exact_applied_plan_required")
        return dto.model_dump(mode="json")
    raise ValueError("unknown_tool")


def before_tool(tool, args, tool_context):
    try:
        state = current(tool_context)
        validate_call(state, tool.name, args)
        state.tool_calls += 1
        state.pending = tool.name
        state.tool_started = time.monotonic()
        if tool.name == "apply_plan":
            state.apply_dispatched = True
            state.status = "unknown"
        elif tool.name == "verify_plan":
            state.verify_calls += 1
            state.verify_started_at = instant(state.context._factory._clock())
        elif tool.name.startswith("read_"):
            state.read_counts[tool.name] = state.read_counts.get(tool.name, 0) + 1
        else:
            state.candidate_input = state.request()
            args["request"] = state.candidate_input.model_dump(mode="json")
        return None
    except (ValueError, TypeError, AccessDenied):
        return safe_error("policy_rejected")


def after_tool(tool, args, tool_context, tool_response):
    state = current(tool_context)
    if tool.name == "solve_repair":


        args.clear()
        args["request"] = {}
    if state.pending != tool.name:
        return safe_error("policy_rejected")
    state.pending = None
    failed = isinstance(tool_response, dict) and bool(tool_response.get("isError") or "error" in tool_response)
    try:
        if tool.name == "solve_repair":
            value = RepairResult.model_validate(tool_response)
            state.candidate = value
            state.status = "candidate" if value.status in ("feasible", "optimal") else str(value.status)
            result = {"status": state.status, "candidate": value.model_dump(mode="json"), "saved": False}
        elif tool.name.startswith("read_"):
            schema = AssignmentsSnapshot if tool.name == "read_assignments" else CalendarSnapshot
            value = schema.model_validate(tool_response.get("structuredContent"))
            state.policy.require_source(state.context, value.source.reference)



            if failed and value.source.complete:
                raise ValueError("failed_complete_read")
            if tool.name == "read_assignments":
                state.assignments = value
                incomplete_status = "needs_input" if any(i.code.startswith("needs_input_") for i in value.source.issues) else "unknown"
                state.status = ("deadlines" if value.source.complete else incomplete_status) if state.mode == "deadlines" else (
                    "needs_input" if any(a.remaining_minutes is None or a.deadline is None for a in value.assignments)
                    else "not_started" if value.source.complete else "unknown")
                result = {"status": state.status, "assignments": [
                    {"assignment_id": a.assignment_id, "deadline": a.deadline.model_dump(mode="json") if a.deadline else None,
                     "remaining_minutes": a.remaining_minutes} for a in value.assignments]}
            else:
                state.calendar = value
                result = {"status": "read", "events": len(value.events), "owned_blocks": len(value.owned_blocks)}
        elif tool.name == "apply_plan":


            value = ApplyResult.model_validate(tool_response.get("structuredContent"))
            t = state.context.trusted
            if (value.execution.plan != state.allowed_plan or value.execution.request_key != state.allowed_apply.request_key
                    or (value.execution.owner_id, value.execution.calendar_id) != (t.owner_id, t.calendar_id)):
                raise ValueError("apply_binding_mismatch")
            state.applied = value
            state.status = str(value.execution.status)
            if state.status == "verified":


                state.status = "applied_unverified"
            if failed and state.status in ("applied_unverified", "verified"):
                state.status = "unknown"
            if any("stale" in f.code or "replan" in f.code for f in value.findings):
                state.stale = True
                state.assignments = state.calendar = None
                if state.status not in ("partial", "unknown"):
                    state.status = "stale"
            result = {"status": state.status, "apply": value.model_dump(mode="json")}
        else:
            value = VerificationResult.model_validate(tool_response.get("structuredContent"))
            if value.calendar_source is not None:
                state.policy.require_source(state.context, value.calendar_source.reference)
            if (value.plan != state.allowed_plan or value.run_id != state.context.trusted.run_id
                    or state.verify_started_at is None or value.attempted_at < state.verify_started_at):
                raise ValueError("verification_binding_mismatch")
            state.verification = value
            previous = state.status
            state.status = value.status
            if failed and value.status == "verified":
                state.status = "applied_unverified"
            if previous in ("unknown", "partial", "stale") and value.status != "verified":
                state.status = previous
            result = {"status": state.status, "verification": value.model_dump(mode="json")}
    except (ValueError, TypeError, AccessDenied, AttributeError):
        if tool.name == "apply_plan":
            codes = [x.get("text") for x in tool_response.get("content", []) if isinstance(x, dict)] if isinstance(tool_response, dict) else []
            state.stale = "stale_source_replan_required" in codes
            if state.stale:
                state.assignments = state.calendar = None
            state.status = "stale" if state.stale else "unknown"
        elif tool.name == "verify_plan":
            if state.status not in ("partial", "unknown"):
                state.status = "applied_unverified"
        else:
            state.status = "unavailable"
        result = {"status": state.status, "code": "tool_result_not_confirmed"}
    state.observations.append({"tool": tool.name, "status": state.status,
        "run_id": state.context.trusted.run_id, "elapsed_ms": round((time.monotonic() - state.started) * 1000),
        "duration_ms": round((time.monotonic() - state.tool_started) * 1000),
        "call_id": tool_context.function_call_id,
        "operation_ids": [o.operation_id for o in state.applied.execution.operations] if state.applied else [],
        "remote_attempts": state.remote_attempts})
    return result


def on_tool_error(tool, args, tool_context, error):
    state = current(tool_context)

    return {"isError": True, "code": "tool_execution_unavailable"}


def on_model_error(callback_context, llm_request, error):
    state = current(callback_context)
    if state.status not in ("partial", "unknown", "verification_failed", "applied_unverified"):
        state.status = "unavailable"
    return response(state)
