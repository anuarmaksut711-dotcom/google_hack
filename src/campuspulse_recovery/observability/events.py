"""Private journal values and a deliberately small UI projection.

These values describe observations, never execution permission or business proof.
T03 RunEvent is unchanged; sequence is allocated by storage, not callbacks.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StrictBool, StrictInt, StringConstraints, model_validator

from ..domain.models import Contract, Instant, Interval, RunEvent, canonical_hash

TechnicalId = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-zA-Z0-9_-]{1,96}$")]
Ref = Annotated[str, StringConstraints(strict=True, pattern=r"^ref_[a-f0-9]{64}$")]
Count = Annotated[StrictInt, Field(ge=0, le=1_000_000)]
Layer = Literal["backend", "adk_callback", "client_observation", "domain_adapter"]
Status = Literal[
    "started", "accepted", "denied", "read", "complete", "incomplete", "result",
    "candidate", "optimal", "feasible", "no_change", "needs_input", "infeasible",
    "needs_review", "not_started", "deadlines", "stale", "blocked", "unavailable",
    "unknown", "partial", "applied_unverified", "verified", "verification_failed",
    "budget_exhausted", "cancelled", "error", "pending", "completed", "failed",
]
STATUSES = frozenset(Status.__args__)
CODES = frozenset(("policy_rejected", "tool_result_not_confirmed", "observation_unavailable",
    "execution_unavailable", "cancelled", "source_incomplete", "journal_unavailable"))


def reference(value: str | None) -> str | None:
    """Opaque correlation of an observed ID; not a raw source/URL projection.

Only generated operation IDs already conforming to op_<sha256> stay readable
through technical_operation_id. Other identifiers are authenticated opaque refs.
"""
    return None if value is None else "ref_" + canonical_hash(["trace-ref-v1", value])


def technical_operation_id(value: str | None) -> str | None:
    import re
    if value is not None and re.fullmatch(r"op_[a-f0-9]{64}", value):
        return value
    return reference(value)


def normalized_status(value: object) -> str:
    return value if isinstance(value, str) and value in STATUSES else "unknown"


class SafeSummary(Contract):
    """No arbitrary text, dictionaries, exception messages or source identities."""
    horizon: Interval | None = None
    item_count: Count | None = None
    operation_count: Count | None = None
    completed_count: Count | None = None
    complete: StrictBool | None = None
    verification_status: Literal["verified", "verification_failed", "applied_unverified", "unknown"] | None = None
    verification_at: Instant | None = None
    verification_run: TechnicalId | None = None
    historical_verification: StrictBool = False




class Observation(Contract):
    event: RunEvent
    layer: Layer
    attempt_id: TechnicalId | None = None
    summary: SafeSummary = Field(default_factory=SafeSummary)

    @model_validator(mode="after")
    def safe_event(self):
        import re
        e = self.event
        for value in (e.event_id, e.run_id):
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,96}", value):
                raise ValueError("trace_technical_identity_required")
        if e.call_id is not None and not re.fullmatch(r"ref_[a-f0-9]{64}", e.call_id):
            raise ValueError("trace_opaque_call_required")
        if e.operation_id is not None and not re.fullmatch(r"(?:ref_|op_)[a-f0-9]{64}", e.operation_id):
            raise ValueError("trace_safe_operation_required")
        if self.attempt_id is not None and not re.fullmatch(r"att_[a-f0-9]{64}", self.attempt_id):
            raise ValueError("trace_real_attempt_identity_required")
        if (e.status not in STATUSES or (e.code is not None and e.code not in CODES)
                or e.source_references or e.plan is not None
                or e.arguments_reference is not None or e.result_reference is not None):
            raise ValueError("trace_raw_fields_forbidden")
        return self


class RecordedEvent(Contract):
    observation: Observation
    recorded_at: Instant
    fingerprint: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]


def observation(*, event_id: str, run_id: str, layer: Layer, occurred_at,
                tool=None, phase="result", status="unknown", call_id=None,
                operation_id=None, attempt_id=None, duration_ms=None,
                summary: SafeSummary | None = None, code=None) -> Observation:
    return Observation(event=RunEvent(event_id=event_id, run_id=run_id,
        call_id=reference(call_id), operation_id=technical_operation_id(operation_id),
        sequence=0, occurred_at=occurred_at, tool=tool, phase=phase,
        status=normalized_status(status), duration_ms=duration_ms,
        code=code if code in CODES else None), layer=layer,
        attempt_id=attempt_id, summary=summary or SafeSummary())


class CallView(Contract):
    run_id: TechnicalId
    layer: Layer
    call_id: str | None
    tool: str | None
    operation_id: str | None = None
    attempt_id: TechnicalId | None = None
    status: Status
    started_at: Instant | None = None
    result_at: Instant | None = None
    duration_ms: Count | None = None
    summary: SafeSummary = Field(default_factory=SafeSummary)


def project_calls(records: tuple[RecordedEvent, ...]) -> tuple[CallView, ...]:
    """Arrival order is not occurrence order. Late starts cannot regress results.

    Aggregate calls (operation_id=None) and individual operation attempts are
    separate views. Run lineage is part of the grouping identity, including for
    explicitly correlated runs with a repeated call ID. Unknown later outcomes
    supersede previous results only within that exact observation identity.
    """
    groups = {}
    for record in records:
        o, e = record.observation, record.observation.event
        groups.setdefault((e.run_id, o.layer, e.call_id, e.tool, e.operation_id, o.attempt_id), []).append(record)
    views = []
    for (run_id, layer, call, tool, operation_id, attempt_id), items in groups.items():
        starts = [r for r in items if r.observation.event.phase == "started"]
        ends = [r for r in items if r.observation.event.phase != "started"]
        latest = max(ends or starts, key=lambda r: (r.observation.event.occurred_at, r.observation.event.sequence))
        e = latest.observation.event
        views.append(CallView(run_id=run_id, layer=layer, call_id=call, tool=tool,
            operation_id=operation_id, attempt_id=attempt_id, status=e.status,
            started_at=min((r.observation.event.occurred_at for r in starts), default=None),
            result_at=e.occurred_at if ends else None, duration_ms=e.duration_ms,
            summary=latest.observation.summary))
    return tuple(views)
