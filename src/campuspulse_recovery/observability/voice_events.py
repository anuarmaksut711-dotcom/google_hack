"""Closed technical voice journal schema. No conversational/source payloads."""
from typing import Literal

from pydantic import Field, StrictBool, StrictInt

from ..domain.models import Contract, Instant
from .events import TechnicalId, Ref, reference
from ..storage.plans import _encode


class VoiceEvent(Contract):
    session_id: TechnicalId
    run_id: TechnicalId
    event_id: TechnicalId
    occurred_at: Instant
    kind: Literal["ready", "tool_started", "tool_finished", "closed"]
    evidence_class: Literal["windows_local", "cloud_live"]
    call_id: Ref | None = None
    tool: Literal["read_assignments", "read_calendar", "solve_repair"] | None = None
    status: Literal["started", "success", "failed", "unavailable", "cancelled",
        "stopped", "disconnected", "revoked", "expired", "timeout", "backpressure",
        "protocol_error", "upstream_error", "ready"]
    duration_ms: StrictInt = Field(ge=0, le=540000)
    cleanup_failed: StrictBool = False


def project(packet, *, lease, now, evidence_class):
    """Project only enumerated fields; never dump arbitrary provider objects."""
    if packet.type not in ("ready", "tool_started", "tool_finished", "closed"):
        return None
    return VoiceEvent(session_id=lease.session_id, run_id=lease.run_id,
        event_id=packet.event_id, occurred_at=now, kind=packet.type,
        evidence_class=evidence_class, call_id=reference(getattr(packet, "call_id", None)),
        tool=getattr(packet, "tool", None), status=("started" if packet.type == "tool_started"
            else getattr(packet, "status", getattr(packet, "reason", "ready"))),
        duration_ms=getattr(packet, "duration_ms", packet.elapsed_ms))


class FirestoreVoiceJournal:
    """Same injected durable documents port; technical events only, no replay."""
    def __init__(self, documents):
        self.documents = documents

    async def append(self, event: VoiceEvent):
        event = VoiceEvent.model_validate(event)
        async def save(tx):
            tx.set("cp_voice_events/" + event.run_id + "_" + event.event_id,
                _encode("voice_event", event))
        await self.documents.transact(save)
