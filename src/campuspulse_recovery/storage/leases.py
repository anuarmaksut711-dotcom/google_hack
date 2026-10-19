"""Transactional calendar claims. Fencing protects Firestore, not Calendar I/O.

Only trusted MCP startup code constructs these adapters. A takeover is read-only
until the previous writer is proven stopped AND the pinned ledger is reconciled.
No timers, process locks, Calendar calls or automatic takeover retries live here.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from pydantic import TypeAdapter

from ..domain.models import Contract, Identifier, Instant, OperationStatus, PositiveInt
from ..domain.ports import BindingConflict, CalendarLease
from .plans import _Repository, _decode, _encode, _key


class WriterStoppedEvidence(Contract):
    """Trusted runtime/operator evidence, never an LLM or browser assertion."""

    holder_id: Identifier
    generation: PositiveInt
    stopped_at: Instant
    evidence_reference: Identifier


class RecoveryGuard(Protocol):
    async def require_stopped(self, context, transaction, previous: CalendarLease,
                              now: datetime) -> WriterStoppedEvidence:
        """Read durable trusted stop evidence through transaction; no external I/O."""
        ...


class _Claim(Contract):
    lease: CalendarLease
    released: bool = False
    execution_path: str | None = None
    previous: CalendarLease | None = None
    stop_evidence: WriterStoppedEvidence | None = None
    writer_uncertain: bool = False


def _claim_path(calendar: str) -> str:

    return f"cp_calendar_claims/{_key(calendar)}"


class FirestoreCalendarCoordination(_Repository):
    def __init__(self, documents, policy, *, clock: Callable[[], datetime] | None = None,
                 recovery_guard: RecoveryGuard | None = None, request_context=None):
        super().__init__(documents, policy, request_context=request_context)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.recovery_guard = recovery_guard

    def _now(self, supplied=None):
        now = TypeAdapter(Instant).validate_python(self.clock())
        if supplied is not None:
            supplied = TypeAdapter(Instant).validate_python(supplied)
            if abs((supplied - now).total_seconds()) > 5:
                raise BindingConflict("coordination_clock_mismatch")
        return now

    def _context(self, context):
        context = self._request(context)
        return context, self._trusted(context, writer="mcp")

    async def _read(self, context, reader):
        _, trusted = self._context(context)
        document = await reader.get(_claim_path(trusted.calendar_id))
        if document is None:
            return None
        claim = _decode(document, "calendar_claim", _Claim)
        if (claim.lease.owner_id, claim.lease.calendar_id) != (trusted.owner_id, trusted.calendar_id):
            raise BindingConflict("calendar_claim_binding_conflict")
        return claim

    async def _require(self, context, reader, lease, *, active=False):
        lease = CalendarLease.model_validate(lease)
        claim = await self._read(context, reader)
        now = self._now()
        if (claim is None or claim.released or claim.lease != lease
                or now >= claim.lease.expires_at):
            raise BindingConflict("calendar_lease_stale")
        if active and lease.state != "active":
            raise BindingConflict("calendar_recovery_required")
        return claim

    async def acquire(self, context, *, holder_id, now, expires_at):
        context, trusted = self._context(context)
        TypeAdapter(Identifier).validate_python(holder_id)
        expires_at = TypeAdapter(Instant).validate_python(expires_at)

        async def claim_calendar(tx):
            current_time = self._now(now)
            if not current_time < expires_at <= current_time + timedelta(minutes=5):
                raise BindingConflict("calendar_lease_lifetime_invalid")
            old = await self._read(context, tx)
            if old and not old.released and current_time < old.lease.expires_at:
                raise BindingConflict("calendar_claim_busy")
            recovery = old is not None and not old.released
            generation = 1 if old is None else old.lease.generation + 1
            if generation > 2**63 - 1:
                raise BindingConflict("calendar_generation_exhausted")
            lease = CalendarLease(owner_id=trusted.owner_id, calendar_id=trusted.calendar_id,
                holder_id=holder_id, generation=generation, acquired_at=current_time,
                expires_at=expires_at, state="recovery_required" if recovery else "active")

            record = _Claim(lease=lease,
                execution_path=old.execution_path if recovery else None,
                previous=(old.previous or old.lease) if recovery else None,
                writer_uncertain=recovery)
            tx.set(_claim_path(trusted.calendar_id), _encode("calendar_claim", record))
            return lease

        return await self.documents.transact(claim_calendar)

    async def renew(self, context, lease, *, now, expires_at):
        context, trusted = self._context(context)
        expires_at = TypeAdapter(Instant).validate_python(expires_at)

        async def change(tx):
            current_time = self._now(now)
            claim = await self._require(context, tx, lease)
            if not lease.expires_at < expires_at <= current_time + timedelta(minutes=5):
                raise BindingConflict("calendar_lease_lifetime_invalid")
            renewed = CalendarLease.model_validate(lease.model_dump() | {"expires_at": expires_at})
            tx.set(_claim_path(trusted.calendar_id), _encode("calendar_claim", claim.model_copy(update={"lease": renewed})))
            return renewed

        return await self.documents.transact(change)

    async def assert_current(self, context, lease, *, now):
        context, _ = self._context(context)
        self._now(now)

        async def check(tx):
            claim = await self._require(context, tx, lease, active=True)
            if claim.writer_uncertain:
                raise BindingConflict("calendar_writer_recovery_required")
        await self.documents.transact(check)

    async def complete_recovery(self, context, lease, *, now):
        context, trusted = self._context(context)

        async def recover(tx):
            current_time = self._now(now)
            claim = await self._require(context, tx, lease)
            if lease.state == "active" and not claim.writer_uncertain:
                return lease
            if lease.state == "active":


                raise BindingConflict("calendar_takeover_required")
            if claim.execution_path:

                from .operations import _Ledger
                ledger = _decode(await tx.get(claim.execution_path), "execution", _Ledger)
                if any(op.status in {OperationStatus.UNKNOWN, OperationStatus.IN_FLIGHT}
                       for op in ledger.execution.operations):
                    raise BindingConflict("calendar_unreconciled_operations")
            previous = claim.previous or claim.lease
            if self.recovery_guard is None:
                raise BindingConflict("calendar_writer_stop_evidence_required")
            evidence = WriterStoppedEvidence.model_validate(await self.recovery_guard.require_stopped(
                context, tx, previous, current_time))
            if ((evidence.holder_id, evidence.generation) != (previous.holder_id, previous.generation)
                    or not previous.acquired_at <= evidence.stopped_at <= current_time):
                raise BindingConflict("calendar_writer_stop_evidence_invalid")
            active = CalendarLease.model_validate(lease.model_dump() | {"state": "active"})
            tx.set(_claim_path(trusted.calendar_id), _encode("calendar_claim",
                claim.model_copy(update={"lease": active, "previous": None, "stop_evidence": evidence,
                    "writer_uncertain": False})))
            return active

        return await self.documents.transact(recover)

    async def release(self, context, lease):
        context, trusted = self._context(context)

        async def finish(tx):
            claim = await self._require(context, tx, lease, active=True)
            if claim.execution_path:
                from .operations import _Ledger
                ledger = _decode(await tx.get(claim.execution_path), "execution", _Ledger)
                if any(op.status in {OperationStatus.UNKNOWN, OperationStatus.IN_FLIGHT}
                       for op in ledger.execution.operations):
                    raise BindingConflict("calendar_unreconciled_operations")
            if claim.writer_uncertain:
                raise BindingConflict("calendar_writer_recovery_required")

            tx.set(_claim_path(trusted.calendar_id), _encode("calendar_claim",
                claim.model_copy(update={"released": True})))

        await self.documents.transact(finish)
