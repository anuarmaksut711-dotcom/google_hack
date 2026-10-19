"""Read-only live policy. No text recovery callbacks or serialized authority.

Automatic SDK calls are bridged into W04's typed ToolRequest path. The
coordinator must durably retire each ID before calling execute(). Never replace
that path with coordinator.authorize_tool(), which only checks authorization.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import timedelta
import json
import time
from zoneinfo import ZoneInfo

from ..domain.models import (
    AssignmentsSnapshot, CalendarSnapshot, PlanningConstraints, ReadAssignmentsInput,
    ReadCalendarInput, SolveRepairInput,
)
from ..domain.solver import solve_repair
from ..domain.time_rules import planning_horizon
from ..security.context import AccessDenied
from ..voice.ports import ToolRequest, ToolResult

READS = ("read_assignments", "read_calendar")
TOOLS = (*READS, "solve_repair")
MAX_TOOLS = 24
MAX_TURNS = 32
TOOL_SECONDS = 18
SESSION_SECONDS = 540
TURN_SECONDS = 60
AUTHORIZATION_SECONDS = 2
SOURCE_AGE = timedelta(minutes=2)
UNCONFIRMED = "Результат не подтверждён. Календарь не изменён."
UNSAVED_PROGRESS = "Оценка оставшейся работы — только заметка разговора, не сохранена в Sheets."
VOICE_LOG_ACTIVE = ContextVar("campuspulse_voice_logs", default=False)


@contextmanager
def voice_logs():
    token = VOICE_LOG_ACTIVE.set(True)
    try:
        yield
    finally:
        VOICE_LOG_ACTIVE.reset(token)


def blocked(code="voice_policy_rejected"):
    return {"status": "blocked", "code": code}


class VoicePolicy:
    """One connection's state; authority is reloaded for every boundary.

    request_scope is an independently authenticated agent RequestContextFactory
    scope, installed by trusted server code. Neither VoiceAccess nor lease JSON
    implements that scope. W06 must supply the actual remote identity channel.
    """

    def __init__(self, *, sessions, lease, request_scope, constraints, now, lease_provider=None):
        self.sessions, self._lease, self.request_scope = sessions, lease, request_scope
        self.lease_provider = lease_provider
        self.resources = sessions.policy
        self.constraints = PlanningConstraints.model_validate(constraints)
        self.now = now
        self.started = time.monotonic()
        self.assignments = self.calendar = self.candidate = None
        self.remote = {}
        self.calls = 0
        self.turns = 0
        self.observations = []

    @property
    def lease(self):
        value = self.lease_provider() if self.lease_provider else self._lease
        # Renewal may replace only lease_until. A successor/rebound connection
        # can never be adopted through this server-only accessor.
        if value.model_dump(exclude={"lease_until"}) != self._lease.model_dump(exclude={"lease_until"}):
            raise AccessDenied("voice_lease_fence_changed")
        return value

    def __reduce__(self):
        raise TypeError("voice_authority_not_serializable")

    def active(self):
        now = self.sessions.now()
        if (now >= self.lease.deadline or time.monotonic() - self.started >= SESSION_SECONDS):
            raise AccessDenied("voice_expired")

    @contextmanager
    def context(self):
        self.active()
        with self.request_scope() as context:
            trusted = self.resources.require_context(context)
            b = self.lease.binding
            if (context.boundary != "agent" or
                    (trusted.owner_id, context.session_id, trusted.run_id, trusted.config_revision) !=
                    (b.owner_id, b.session_id, b.run_id, b.config_revision)):
                raise AccessDenied("voice_agent_binding_rejected")
            if (self.constraints.config_revision != trusted.config_revision or
                    self.constraints.timezone != trusted.timezone or
                    (self.constraints.horizon.start, self.constraints.horizon.end) != planning_horizon(self.now)):
                raise AccessDenied("voice_planning_basis_rejected")
            yield context

    async def authorize(self, name=None):
        # W04 replaces the complete DTO on renewal. A check queued with the old
        # lease_until may fail even though this exact holder is still valid.
        # One fresh durable retry is allowed only for that precise race; no
        # cached access or successor lease can substitute for validation.
        async with asyncio.timeout(AUTHORIZATION_SECONDS):
            for attempt in range(2):
                self.active()
                captured = self.lease
                try:
                    access = await self.sessions.check(captured, tool=name)
                except AccessDenied as error:
                    self.active()  # Deadlines may have elapsed while awaiting.
                    fresh = self.lease  # Rejects any changed immutable fence.
                    if (attempt or str(error) != "voice_stream_stale"
                            or fresh.model_dump(exclude={"lease_until"}) != captured.model_dump(exclude={"lease_until"})
                            or fresh.lease_until <= captured.lease_until
                            or self.sessions.now() >= fresh.lease_until):
                        raise
                    continue
                self.active()
                fresh = self.lease
                if (access.binding != fresh.binding or access.device_id != fresh.device_id
                        or self.sessions.now() >= min(fresh.lease_until, access.expires_at)):
                    raise AccessDenied("voice_binding_rejected")
                with self.context():
                    pass
                return access

    def validate(self, name, args):
        self.active()
        if name not in TOOLS or not isinstance(args, dict):
            raise ValueError("voice_tool_rejected")
        if len(json.dumps(args, allow_nan=False).encode("utf-8")) > 16384:
            raise ValueError("voice_arguments_limit")
        if name in READS:
            schema = ReadAssignmentsInput if name == READS[0] else ReadCalendarInput
            dto = schema.model_validate(args)
            if dto.horizon != self.constraints.horizon:
                raise ValueError("voice_server_horizon_required")
            return dto.model_dump(mode="json")
        if args != {"request": {}}:
            raise ValueError("voice_authoritative_solver_only")
        return {"request": {}}

    def source(self, name, envelope):
        schema = AssignmentsSnapshot if name == READS[0] else CalendarSnapshot
        if not isinstance(envelope, dict) or "error" in envelope:
            raise ValueError("voice_source_unavailable")
        value = schema.model_validate(envelope.get("structuredContent"))
        ref = value.source.reference
        with self.context() as context:
            self.resources.require_source(context, ref)
        age = self.sessions.now() - ref.read_at
        if not timedelta(0) <= age <= SOURCE_AGE:
            raise ValueError("voice_source_stale")
        if envelope.get("isError") and value.source.complete:
            raise ValueError("voice_source_unconfirmed")
        if name == READS[0]:
            self.assignments = value
            # External titles/descriptions are never model instructions or output.
            data = [{"assignment_id": a.assignment_id, "deadline": a.deadline.model_dump(mode="json")
                     if a.deadline else None, "remaining_minutes": a.remaining_minutes}
                    for a in value.assignments]
        else:
            if value.source.covered_interval != self.constraints.horizon:
                raise ValueError("voice_calendar_coverage_required")
            self.calendar = value
            data = {"busy_intervals": [e.interval.model_dump(mode="json") for e in value.events if e.busy],
                    "owned_blocks": len(value.owned_blocks)}
        self.candidate = None
        return {"status": "read" if value.source.complete else "incomplete",
                "read_at": ref.read_at.isoformat(), "complete": value.source.complete,
                "timezone": value.source.timezone, "issues": [i.code for i in value.source.issues], "data": data,
                "speech": self.speech(), "saved": False}

    def solve(self):
        now = self.sessions.now()
        if (self.constraints.horizon.start, self.constraints.horizon.end) != planning_horizon(now):
            raise ValueError("voice_refresh_server_planning_basis_required")
        if self.assignments is None or self.calendar is None:
            raise ValueError("voice_source_reads_required")
        for name, value in ((READS[0], self.assignments), (READS[1], self.calendar)):
            self.source(name, {"structuredContent": value.model_dump(mode="json")})
        request = SolveRepairInput(assignments=self.assignments, calendar=self.calendar,
            constraints=self.constraints, now=now, plan_id="voice_candidate", version=1)
        self.candidate = solve_repair(request)
        return {"status": str(self.candidate.status), "saved": False,
                "candidate": self.candidate.model_dump(mode="json"), "speech": self.speech()}

    def speech(self):
        """Finite evidence-rendered speech contract, checked before audio release."""
        zone = ZoneInfo(self.constraints.timezone)
        suffix = " Календарь не изменён. " + UNSAVED_PROGRESS
        if self.candidate is not None:
            if any(not timedelta(0) <= self.sessions.now() - x.source.reference.read_at <= SOURCE_AGE
                   for x in (self.assignments, self.calendar)):
                return "Данные устарели. Нужны свежие чтения." + suffix
            c = self.candidate
            deficits = "; ".join(f"дефицит {f.deficit_minutes} минут до {f.deadline.astimezone(zone).isoformat()}"
                                 for f in c.findings if f.deficit_minutes and f.deadline)
            labels = {"optimal": "предложение рассчитано", "feasible": "предложение рассчитано",
                "no_change": "изменения не нужны", "infeasible": "обнаружен дефицит времени",
                "needs_input": "нужно уточнение", "needs_review": "нужна проверка источников", "unknown": "результат не определён"}
            schedule = "; ".join(f"Блок {i}: {b.interval.start.astimezone(zone).isoformat()} — {b.interval.end.astimezone(zone).isoformat()}"
                                 for i, b in enumerate(c.blocks[:8], 1))
            return (f"В горизонте 72 часа {labels[str(c.status)]}. {deficits} {schedule}"
                    + " Учтены дедлайны, занятость и заданные рабочие окна." + suffix)
        if self.assignments is None:
            return UNCONFIRMED
        a = self.assignments
        age = self.sessions.now() - a.source.reference.read_at
        if not a.source.complete or not timedelta(0) <= age <= SOURCE_AGE:
            return "Данные неполны или устарели. Нужны свежие чтения." + suffix
        if any(x.remaining_minutes is None or x.deadline is None for x in a.assignments):
            return "Уточните срок и оставшуюся работу. Прошедший блок не подтверждает выполнение." + suffix
        deadlines = "; ".join(f"Задание {i}: {x.deadline.at.astimezone(zone).isoformat()}, осталось {x.remaining_minutes} минут"
                              for i, x in enumerate(a.assignments, 1))
        result = (f"Чтение {a.source.reference.read_at.isoformat()}. Часовой пояс {self.constraints.timezone}. "
                  + (deadlines or "Заданий нет") + "." + suffix)
        return result if len(result) <= 4096 else "Прочитано много заданий. Нужен более узкий запрос." + suffix


class LiveToolBridge:
    """SDK tool -> typed coordinator dispatch -> guarded MCP -> SDK response.

    Coordinator is the ONLY durable retirement writer. pending is per-call,
    connection-local correlation, never authority or resumption state.
    """

    def __init__(self, policy, emit):
        self.policy, self.emit = policy, emit
        self.pending = {}
        self.used = set()

    async def before_tool(self, tool, args, tool_context):
        try:
            self.policy.validate(tool.name, args)
            await self.policy.authorize(tool.name)
            if (tool_context.user_id != self.policy.lease.binding.owner_id or
                    tool_context.session.id != self.policy.lease.session_id):
                raise AccessDenied("voice_sdk_binding_rejected")
        except (ValueError, TypeError, AccessDenied):
            return blocked()

    async def call(self, name, args, tool_context):
        try:
            args = self.policy.validate(name, args)
            if (tool_context.user_id != self.policy.lease.binding.owner_id or
                    tool_context.session.id != self.policy.lease.session_id):
                raise AccessDenied("voice_sdk_binding_rejected")
            call_id = tool_context.function_call_id
            from ..voice.protocol import WireId
            from pydantic import TypeAdapter
            TypeAdapter(WireId).validate_python(call_id)
            if (call_id in self.used or len(self.pending) >= 4 or self.policy.calls >= MAX_TOOLS):
                return blocked("voice_tool_budget_or_replay")
            # Reserve before any await: live SDK callbacks execute concurrently.
            self.used.add(call_id)
            self.policy.calls += 1
            future = asyncio.get_running_loop().create_future()
            request = ToolRequest(call_id, name, args)
            self.pending[call_id] = (request, future, tool_context, False)
            await self.policy.authorize(name)
            async with asyncio.timeout(TOOL_SECONDS):
                await self.emit(request)
                result = await future
            await self.policy.authorize(name)
            return result.value if result.status == "success" else blocked("voice_tool_unavailable")
        except asyncio.CancelledError:
            raise
        except Exception:
            return blocked("voice_tool_unavailable")
        finally:
            if 'call_id' in locals() and call_id in self.pending and 'future' in locals():
                self.pending.pop(call_id, None)

    async def execute(self, access, request):
        """W04 ReadToolHandler; do not invoke without W04 durable dispatch."""
        started = time.monotonic()
        status = "unavailable"
        try:
            fresh = await self.policy.authorize(request.tool)
            entry = self.pending.get(request.call_id)
            if fresh != access or entry is None or entry[0] != request or entry[3]:
                raise AccessDenied("voice_dispatch_binding_rejected")
            self.pending[request.call_id] = (*entry[:3], True)
            args = self.policy.validate(request.tool, request.arguments)
            async with asyncio.timeout(TOOL_SECONDS):
                if request.tool == "solve_repair":
                    value = self.policy.solve()
                else:
                    from google.adk.tools.mcp_tool.mcp_tool import McpTool
                    remote = self.policy.remote.get(request.tool)
                    method = getattr(McpTool._run_async_impl, "__wrapped__", None)
                    if not isinstance(remote, McpTool) or method is None:
                        raise ValueError("voice_exact_mcp_sdk_required")
                    # No blind SDK retry. Keep audience-bound MCP auth and its
                    # independent server resource policy intact.
                    with self.policy.context(), voice_logs():
                        envelope = await method(remote, args=args, tool_context=entry[2], credential=None)
                    await self.policy.authorize(request.tool)
                    value = self.policy.source(request.tool, envelope)
            await self.policy.authorize(request.tool)
            status = "success"
            return ToolResult(status, value)
        except asyncio.CancelledError:
            raise
        except Exception:
            if request.tool == READS[0]: self.policy.assignments = None
            if request.tool == READS[1]: self.policy.calendar = None
            self.policy.candidate = None
            return ToolResult(status, blocked("voice_tool_unavailable"))
        finally:
            self.policy.observations.append({"run_id": self.policy.lease.run_id,
                "call_id": request.call_id, "tool": request.tool if request.tool in TOOLS else "rejected",
                "status": status, "duration_ms": int((time.monotonic() - started) * 1000),
                "operation_ids": []})

    async def tool_result(self, call_id, result):
        entry = self.pending.get(call_id)
        if entry is None or entry[1].done():
            raise AccessDenied("voice_result_binding_rejected")
        entry[1].set_result(result)

    async def after_tool(self, tool, args, tool_context, tool_response):
        await self.policy.authorize(tool.name)
        return tool_response

    async def on_tool_error(self, tool, args, tool_context, error):
        return blocked()
