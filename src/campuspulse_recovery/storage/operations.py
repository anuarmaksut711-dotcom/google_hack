"""Durable apply ledger: immutable request binding, CAS, attempt/audit evidence.

Not an apply orchestrator or a Calendar adapter. This module never performs
external I/O. Approval/recovery readers are trusted startup-injected Firestore
transaction readers, not model-provided booleans or calls to other services.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

from pydantic import TypeAdapter

from ..domain.models import (
    Approval, CalendarEvent, Contract, Digest, ExecutionState, ExecutionStatus,
    Identifier, Instant, OperationState, OperationStatus, PlanReference,
    canonical_hash, validate_execution_transition, validate_operation_transition,
)
from ..domain.ports import BindingConflict
from .leases import FirestoreCalendarCoordination, _claim_path
from .plans import _Repository, _decode, _encode, _key, _storage_version, plan_reference


class TransactionalApprovalGuard(Protocol):
    async def require_active(self, context, transaction, plan: PlanReference,
                             now: datetime) -> Approval:
        """Read actual backend UI approval via supplied tx; no external I/O.

        Must use the same documents/database and transaction so revocation races
        with dispatch CAS. ApprovalStore's separate transaction API is NOT this
        interface. Implemented by the approval task; missing guard fails closed.
        """
        ...


class OutcomeEvidence(Contract):
    """Evidence from trusted adapter/reconciler, not public tool input.

    `read_no_effect` means confirmed create-not-found or the exact unchanged
    update (before interval AND expected etag). Read failures never mean absence.
    """

    operation_id: Identifier
    target_fingerprint: Digest
    kind: Literal["response_no_effect", "read_target", "read_no_effect"]
    at: Instant
    reference: Identifier
    observed_event: CalendarEvent | None = None


class Attempt(Contract):
    operation_id: Identifier
    attempt_id: Identifier
    ordinal: int
    fencing_generation: int
    holder_id: Identifier
    started_at: Instant
    target_fingerprint: Digest


class _Ledger(Contract):
    execution: ExecutionState
    target_fingerprints: tuple[Digest, ...]
    attempts: tuple[Attempt, ...] = ()


class _Audit(Contract):
    ledger: _Ledger
    recorded_at: Instant
    fencing_generation: int | None = None
    holder_id: Identifier | None = None
    evidence: tuple[OutcomeEvidence, ...] = ()


def _request_path(owner, calendar, key):
    TypeAdapter(Identifier).validate_python(key)
    return f"cp_apply_requests/{_key(owner, calendar, key)}"


def _plan_binding_path(owner, calendar, reference):
    _storage_version(reference.version)

    return f"cp_apply_plans/{_key(owner, calendar, reference.plan_id, reference.version)}"


def target_fingerprint(operation):
    """Entire immutable intent, including expected etag/before/event/target."""
    return canonical_hash(operation)


class FirestoreExecutionStore(_Repository):
    def __init__(self, documents, policy, *, coordination: FirestoreCalendarCoordination,
                 approval_guard: TransactionalApprovalGuard | None = None, request_context=None,
                 verification_guard=None):
        super().__init__(documents, policy, request_context=request_context)
        if coordination.documents is not documents or coordination.policy is not policy:
            raise BindingConflict("coordination_store_mismatch")
        self.coordination, self.approval_guard = coordination, approval_guard
        if verification_guard is not None and (verification_guard.documents is not documents
                or verification_guard.policy is not policy):
            raise BindingConflict("verification_guard_store_mismatch")
        self.verification_guard = verification_guard

    def _context(self, context, *, write=False):
        context = self._request(context)
        return context, self._trusted(context, writer="mcp" if write else None)

    async def _load(self, context, reader, request_key):
        _, trusted = self._context(context)
        path = _request_path(trusted.owner_id, trusted.calendar_id, request_key)
        ledger = _decode(await reader.get(path), "execution", _Ledger)
        state = ledger.execution
        if (state.owner_id, state.calendar_id, state.request_key) != (
                trusted.owner_id, trusted.calendar_id, request_key):
            raise BindingConflict("execution_binding_conflict")
        plan = await self._load_plan(context, reader, state.plan.plan_id, state.plan.version)
        if (plan_reference(plan) != state.plan
                or tuple(op.operation_id for op in state.operations) != tuple(op.operation_id for op in plan.payload.operations)
                or ledger.target_fingerprints != tuple(map(target_fingerprint, plan.payload.operations))):
            raise BindingConflict("execution_saved_intent_mismatch")
        return path, ledger, plan

    async def bind_request(self, context, *, request_key, plan, at):
        context, trusted = self._context(context, write=True)
        plan = PlanReference.model_validate(plan)
        _storage_version(plan.version)
        at = TypeAdapter(Instant).validate_python(at)
        path = _request_path(trusted.owner_id, trusted.calendar_id, request_key)
        binding_path = _plan_binding_path(trusted.owner_id, trusted.calendar_id, plan)

        async def bind(tx):
            self.coordination._now(at)
            saved = await self._load_plan(context, tx, plan.plan_id, plan.version)
            if plan_reference(saved) != plan:
                raise BindingConflict("execution_plan_hash_mismatch")
            document, binding = await tx.get(path), await tx.get(binding_path)
            if document is not None:
                _, ledger, _ = await self._load(context, tx, request_key)
                if ledger.execution.plan != plan or binding != {"request_path": path}:
                    raise BindingConflict("request_key_payload_conflict")
                return ledger.execution

            if binding is not None:
                raise BindingConflict("plan_already_bound_to_request")
            if at < saved.payload.created_at:
                raise BindingConflict("execution_predates_plan")
            execution = ExecutionState(owner_id=trusted.owner_id, calendar_id=trusted.calendar_id,
                plan=plan, request_key=request_key, run_id=trusted.run_id,
                status="proposed", updated_at=at, operations=tuple(
                    OperationState(operation_id=op.operation_id, status="pending", updated_at=at)
                    for op in saved.payload.operations))
            ledger = _Ledger(execution=execution,
                target_fingerprints=tuple(map(target_fingerprint, saved.payload.operations)))
            tx.create(path, _encode("execution", ledger))
            tx.create(binding_path, {"request_path": path})
            tx.create(path + "/history/r0", _encode("execution_audit", _Audit(ledger=ledger, recorded_at=at)))
            return execution

        return await self.documents.transact(bind)

    async def get(self, context, request_key):
        context, _ = self._context(context)
        _, ledger, _ = await self._load(context, self.documents, request_key)
        return ledger.execution

    async def get_ledger(self, context, request_key):
        """Internal recovery/observability: original attempt IDs and fingerprints."""
        context, _ = self._context(context)
        _, ledger, _ = await self._load(context, self.documents, request_key)
        return ledger

    async def _for_plan(self, context, reader, reference):
        """Resolve only the existing exact binding; never create a request."""
        _, trusted = self._context(context)
        reference = PlanReference.model_validate(reference)
        binding = await reader.get(_plan_binding_path(trusted.owner_id, trusted.calendar_id, reference))
        if not isinstance(binding, dict) or set(binding) != {"request_path"}:
            raise BindingConflict("verification_execution_binding_missing")
        path = binding["request_path"]
        if (not isinstance(path, str) or not path.startswith("cp_apply_requests/")
                or path.count("/") != 1 or len(path.rsplit("/", 1)[-1]) != 64
                or set(path.rsplit("/", 1)[-1]) - set("0123456789abcdef")):
            raise BindingConflict("verification_execution_binding_invalid")
        candidate = _decode(await reader.get(path), "execution", _Ledger)
        actual_path, ledger, plan = await self._load(context, reader, candidate.execution.request_key)
        if path != actual_path or ledger.execution.plan != reference:
            raise BindingConflict("verification_execution_binding_invalid")
        return actual_path, ledger, plan

    async def get_ledger_for_plan(self, context, reference):
        context, _ = self._context(context)
        async def read(tx):
            _, ledger, _ = await self._for_plan(context, tx, reference)
            self._trusted(context)
            return ledger
        return await self.documents.transact(read)

    def _verification_guard(self):
        guard = self.verification_guard
        if guard is None or guard.documents is not self.documents or guard.policy is not self.policy:
            raise BindingConflict("verification_store_evidence_required")
        return guard

    async def accept_verification(self, context, *, reference, proof_id):
        """Distinct read-only proof transition. No consent, lease or write permit.

        The injected guard reloads immutable proof/sources/checks via THIS tx.
        Consumption and the unchanged-shape ledger/audit commit together. Ordinary
        CAS still rejects verification DTOs. A commit replay never adds a revision.
        """
        context, _ = self._context(context, write=True)
        TypeAdapter(Digest).validate_python(proof_id)
        guard = self._verification_guard()
        async def accept(tx):
            path, ledger, plan = await self._for_plan(context, tx, reference)
            consumed_path = path + "/verification/" + proof_id
            consumed = await tx.get(consumed_path)
            if consumed is not None:
                await guard.require_consumed(context, tx, proof_id, ledger, plan, consumed)
                self._trusted(context, writer="mcp")
                return ledger.execution
            result = await guard.require_current(context, tx, proof_id, ledger, plan)
            old = ledger.execution
            if (result.plan != old.plan or result.status not in {"verified", "verification_failed"}
                    or any(s.status != OperationStatus.COMPLETED for s in old.operations)):
                raise BindingConflict("verification_conclusive_completed_evidence_required")
            try:
                validate_execution_transition(old.status, ExecutionStatus(result.status))
            except ValueError:
                raise BindingConflict("execution_transition_invalid") from None
            now = self.coordination._now()
            if now < max(old.updated_at, result.verification_at) or old.revision >= 2**63 - 1:
                raise BindingConflict("execution_revision_or_time_invalid")
            execution = ExecutionState.model_validate(old.model_dump() | {
                "revision": old.revision + 1, "updated_at": now, "status": result.status})
            updated = _Ledger(execution=execution, target_fingerprints=ledger.target_fingerprints,
                              attempts=ledger.attempts)
            link = {"proof_id": proof_id, "before_revision": old.revision,
                    "after_revision": execution.revision, "ledger_digest": canonical_hash(updated)}
            self._trusted(context, writer="mcp")
            tx.set(path, _encode("execution", updated))
            tx.create(path + f"/history/r{execution.revision}",
                      _encode("execution_audit", _Audit(ledger=updated, recorded_at=now)))
            tx.create(consumed_path, link)
            tx.set(path + "/verification/accepted", link)
            return execution
        return await self.documents.transact(accept)

    async def saved_verification(self, context, request_key):
        """Load the accepted historical fact, preserving its actual time/run."""
        context, _ = self._context(context, write=True)
        guard = self._verification_guard()
        async def read(tx):
            path, ledger, plan = await self._load(context, tx, request_key)
            link = await tx.get(path + "/verification/accepted")
            if link is None:
                raise BindingConflict("stored_verification_evidence_missing")
            result = await guard.require_consumed(context, tx, link.get("proof_id"), ledger, plan, link)
            self._trusted(context, writer="mcp")
            return ledger.execution, result
        return await self.documents.transact(read)

    async def _approval(self, context, tx, reference, now):
        if self.approval_guard is None:
            raise BindingConflict("transactional_ui_approval_guard_required")
        approval = Approval.model_validate(await self.approval_guard.require_active(context, tx, reference, now))
        trusted = self._trusted(context, writer="mcp")
        if ((approval.owner_id, approval.calendar_id, approval.plan, approval.approved_by) != (
                trusted.owner_id, trusted.calendar_id, reference, self.policy.config.student.allowed_signin_principal)
                or not approval.active_at(now)):
            raise BindingConflict("active_exact_ui_approval_required")
        return approval

    @staticmethod
    def _observed(operation, event, owner, calendar):
        marker = None if event is None else event.campuspulse_marker
        if (event is None or event.event_id != operation.event_id or event.etag is None
                or event.interval != operation.target.interval or event.status != "confirmed"
                or event.transparency != "opaque" or event.all_day_start is not None
                or event.recurrence is not None or marker is None
                or (marker.owner_id, marker.calendar_id, marker.block_id, marker.assignment_id) != (
                    owner, calendar, operation.target.block_id, operation.target.assignment_id)
                or (operation.kind == "create" and marker.create_operation_id != operation.operation_id)
                or (operation.kind == "update" and event.etag == operation.expected_etag)):
            raise BindingConflict("operation_target_evidence_mismatch")

    async def compare_and_set(self, context, *, expected_revision, execution, lease,
                              evidence: tuple[OutcomeEvidence, ...] = ()):
        context, trusted = self._context(context, write=True)
        execution = ExecutionState.model_validate(execution)
        evidence = tuple(OutcomeEvidence.model_validate(e) for e in evidence)
        if type(expected_revision) is not int or expected_revision < 0:
            raise BindingConflict("execution_revision_invalid")
        if (execution.owner_id, execution.calendar_id) != (trusted.owner_id, trusted.calendar_id):
            raise BindingConflict("execution_binding_conflict")

        async def change(tx):
            now = self.coordination._now(execution.updated_at)
            path, ledger, plan = await self._load(context, tx, execution.request_key)
            old = ledger.execution
            claim = await self.coordination._require(context, tx, lease)
            if (execution.plan, execution.run_id, tuple(op.operation_id for op in execution.operations)) != (
                    old.plan, old.run_id, tuple(op.operation_id for op in old.operations)):
                raise BindingConflict("execution_saved_intent_mismatch")

            if execution == old:
                if any(op.status == OperationStatus.IN_FLIGHT for op in old.operations):


                    raise BindingConflict("execution_dispatch_already_claimed")
                return old
            if old.revision != expected_revision or execution.revision != expected_revision + 1:
                raise BindingConflict("execution_revision_conflict")
            if execution.revision > 2**63 - 1 or execution.updated_at < old.updated_at:
                raise BindingConflict("execution_revision_or_time_invalid")
            try:
                validate_execution_transition(old.status, execution.status)
            except ValueError:
                raise BindingConflict("execution_transition_invalid") from None

            if execution.status in {ExecutionStatus.VERIFIED, ExecutionStatus.VERIFICATION_FAILED}:
                raise BindingConflict("verification_store_evidence_required")
            changed = [(before, after, operation) for before, after, operation in
                zip(old.operations, execution.operations, plan.payload.operations) if before != after]
            if len(changed) > 1:
                raise BindingConflict("one_operation_transition_per_cas")
            evidences = {e.operation_id: e for e in evidence}
            if len(evidences) != len(evidence) or set(evidences) - {a.operation_id for _, a, _ in changed}:
                raise BindingConflict("operation_evidence_binding_conflict")
            attempts = {a.operation_id: a for a in ledger.attempts}
            dispatch = False
            reconciliation_only = bool(changed) and all(
                before.status == OperationStatus.UNKNOWN for before, _, _ in changed)
            if changed and claim.execution_path not in (None, path):
                raise BindingConflict("calendar_other_execution_pinned")
            for before, after, operation in changed:
                ev = evidences.get(after.operation_id)
                if before.status == after.status:
                    raise BindingConflict("same_state_evidence_rewrite_forbidden")
                if before.status == OperationStatus.UNKNOWN and ev is None:
                    raise BindingConflict("actual_reconciliation_evidence_required")
                try:
                    validate_operation_transition(before.status, after.status, reconciled=ev is not None)
                except ValueError:
                    raise BindingConflict("operation_transition_invalid") from None
                if not before.updated_at <= after.updated_at <= execution.updated_at:
                    raise BindingConflict("operation_time_invalid")
                if ev and (ev.target_fingerprint != target_fingerprint(operation)
                           or not before.updated_at <= ev.at <= after.updated_at):
                    raise BindingConflict("operation_evidence_binding_conflict")
                dispatch = after.status == OperationStatus.IN_FLIGHT
                if dispatch:
                    if (after.attempts != before.attempts + 1 or after.fencing_generation != lease.generation
                            or after.observed_event is not None or after.reconciled_at is not None
                            or after.error_code is not None):
                        raise BindingConflict("operation_attempt_invalid")
                    if lease.state != "active" or claim.writer_uncertain:
                        raise BindingConflict("calendar_recovery_required")
                    if any(op.status in {OperationStatus.UNKNOWN, OperationStatus.IN_FLIGHT}
                           for op in old.operations):
                        raise BindingConflict("execution_reconciliation_required")
                    attempts[after.operation_id] = Attempt(operation_id=after.operation_id,
                        attempt_id="att_" + _key(path, after.operation_id, after.attempts, lease.generation),
                        ordinal=after.attempts, fencing_generation=lease.generation,
                        holder_id=lease.holder_id, started_at=after.updated_at,
                        target_fingerprint=target_fingerprint(operation))
                elif (after.attempts, after.fencing_generation) != (before.attempts, before.fencing_generation):
                    raise BindingConflict("operation_attempt_rewrite_forbidden")
                if before.status == OperationStatus.UNKNOWN:
                    if (ev is None or ev.kind not in {"read_target", "read_no_effect"}
                            or after.reconciled_at != ev.at):
                        raise BindingConflict("actual_reconciliation_evidence_required")
                    if after.status in {OperationStatus.PENDING, OperationStatus.FAILED}:
                        if ev.kind != "read_no_effect" or self.coordination.recovery_guard is None:
                            raise BindingConflict("stopped_writer_and_no_effect_required")
                        previous = claim.previous or claim.lease
                        stopped = await self.coordination.recovery_guard.require_stopped(context, tx, previous, now)
                        from .leases import WriterStoppedEvidence
                        stopped = WriterStoppedEvidence.model_validate(stopped)
                        if ((stopped.holder_id, stopped.generation) != (previous.holder_id, previous.generation)
                                or not previous.acquired_at <= stopped.stopped_at <= ev.at):
                            raise BindingConflict("stopped_writer_evidence_invalid")
                        if operation.kind == "create":
                            if ev.observed_event is not None:
                                raise BindingConflict("create_absence_evidence_required")
                        elif (ev.observed_event is None or ev.observed_event.event_id != operation.event_id
                              or ev.observed_event.interval != operation.before
                              or ev.observed_event.etag != operation.expected_etag):
                            raise BindingConflict("update_no_effect_evidence_required")
                elif after.reconciled_at is not None:
                    raise BindingConflict("unexpected_reconciliation_timestamp")
                if after.status == OperationStatus.COMPLETED:
                    self._observed(operation, after.observed_event, trusted.owner_id, trusted.calendar_id)
                    if before.status == OperationStatus.UNKNOWN and (
                            ev.kind != "read_target" or ev.observed_event != after.observed_event):
                        raise BindingConflict("reconciled_target_evidence_required")
                elif after.observed_event is not None:
                    raise BindingConflict("unexpected_operation_event")
                if before.status == OperationStatus.IN_FLIGHT:
                    if before.fencing_generation != lease.generation and after.status != OperationStatus.UNKNOWN:
                        raise BindingConflict("stale_dispatch_requires_reconciliation")
                    if after.status == OperationStatus.FAILED and (ev is None or ev.kind != "response_no_effect"):
                        raise BindingConflict("known_no_effect_response_required")
                if after.status in {OperationStatus.UNKNOWN, OperationStatus.FAILED} and after.error_code is None:
                    raise BindingConflict("safe_operation_error_code_required")
            approval = None
            if dispatch or (not reconciliation_only and execution.status in {
                    ExecutionStatus.APPROVED, ExecutionStatus.APPLYING}):
                if lease.state != "active":
                    raise BindingConflict("calendar_recovery_required")
                approval = await self._approval(context, tx, execution.plan, self.coordination._now())
            states = {op.status for op in execution.operations}
            if OperationStatus.COMPLETED in states and states != {OperationStatus.COMPLETED}:
                required = ExecutionStatus.PARTIAL
            elif states & {OperationStatus.UNKNOWN, OperationStatus.IN_FLIGHT}:
                required = ExecutionStatus.UNKNOWN if OperationStatus.UNKNOWN in states else ExecutionStatus.APPLYING
            elif states == {OperationStatus.COMPLETED}:
                required = ExecutionStatus.APPLIED_UNVERIFIED
            else:
                required = None
            if required is not None and execution.status != required:
                raise BindingConflict("execution_aggregate_mismatch")
            if dispatch and old.status not in {ExecutionStatus.APPROVED, ExecutionStatus.APPLYING, ExecutionStatus.PARTIAL}:
                raise BindingConflict("execution_not_approved_for_dispatch")
            updated = _Ledger(execution=execution, target_fingerprints=ledger.target_fingerprints,
                attempts=tuple(attempts.values()))
            encoded = _encode("execution", updated)
            audit = _encode("execution_audit", _Audit(ledger=updated, recorded_at=now,
                fencing_generation=lease.generation, holder_id=lease.holder_id, evidence=evidence))


            final_now = self.coordination._now()
            if final_now >= lease.expires_at:
                raise BindingConflict("calendar_lease_stale")
            if approval is not None and not approval.active_at(final_now):
                raise BindingConflict("active_exact_ui_approval_required")
            self._trusted(context, writer="mcp")
            tx.set(path, encoded)
            tx.create(path + f"/history/r{execution.revision}", audit)
            if changed:
                uncertain = claim.writer_uncertain or any(
                    after.status == OperationStatus.UNKNOWN for _, after, _ in changed)
                tx.set(_claim_path(trusted.calendar_id), _encode("calendar_claim",
                    claim.model_copy(update={"execution_path": path, "writer_uncertain": uncertain})))
            return execution

        return await self.documents.transact(change)

    async def record_operation(self, context, *, request_key, expected_revision, operation,
                               lease, evidence: OutcomeEvidence | None = None):
        """CAS one operation and derive aggregate without dropping completed work."""
        operation = OperationState.model_validate(operation)
        state = await self.get(context, request_key)
        if operation.operation_id not in {op.operation_id for op in state.operations}:
            raise BindingConflict("operation_not_in_saved_plan")
        operations = tuple(operation if op.operation_id == operation.operation_id else op for op in state.operations)
        if operations == state.operations:
            return await self.compare_and_set(context, expected_revision=expected_revision,
                execution=state, lease=lease)
        statuses = {op.status for op in operations}
        if OperationStatus.COMPLETED in statuses:
            status = "applied_unverified" if statuses == {OperationStatus.COMPLETED} else "partial"
        elif OperationStatus.UNKNOWN in statuses:
            status = "unknown"
        elif OperationStatus.IN_FLIGHT in statuses:
            status = "applying"
        elif OperationStatus.FAILED in statuses:
            status = "failed"
        else:
            status = state.status
            if status == ExecutionStatus.UNKNOWN:
                status = "applying"


        execution = ExecutionState.model_validate(state.model_dump() | {
            "operations": operations, "status": status, "revision": expected_revision + 1,
            "updated_at": operation.updated_at})
        return await self.compare_and_set(context, expected_revision=expected_revision,
            execution=execution, lease=lease, evidence=() if evidence is None else (evidence,))
