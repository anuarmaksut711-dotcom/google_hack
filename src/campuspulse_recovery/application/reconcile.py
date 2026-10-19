"""Actual point-read recovery, independent of UI consent and availability preflight.

Only trusted startup composes this service. No caller-supplied observation or
writer-stopped boolean is accepted. Stop evidence is checked by T06 in its own
transaction; absence/read failure never establishes that a writer has stopped.
"""

from ..domain.models import Finding, OperationState, OperationStatus
from ..domain.ports import BindingConflict
from ..domain.time_rules import instant
from ..integrations.calendar_write import TargetComparison, compare_target
from ..storage.operations import OutcomeEvidence, target_fingerprint


def changed(value, **fields):
    return type(value).model_validate(value.model_dump() | fields)


class ReconciliationService:
    def __init__(self, executions, registry, writer, *, clock, max_attempts=3):
        self.executions, self.registry, self.writer = executions, registry, writer
        self.clock, self.max_attempts = clock, max_attempts

    async def reconcile(self, context, plan, execution, lease):
        """Return durable facts plus findings; never send a mutation.

        A matched read can finish an operation while writer_uncertain remains
        set. No-effect reset is attempted only through the stopped-writer CAS.
        A later valid stop requires a NEW read, not reuse of this observation.
        """
        findings = []
        for operation in plan.payload.operations:
            state = next(s for s in execution.operations if s.operation_id == operation.operation_id)
            if state.status not in (OperationStatus.IN_FLIGHT, OperationStatus.UNKNOWN):
                continue
            if state.status == OperationStatus.IN_FLIGHT:
                execution = await self.executions.record_operation(context,
                    request_key=execution.request_key, expected_revision=execution.revision,
                    operation=changed(state, status="unknown", updated_at=instant(self.clock()),
                                      error_code="apply_dispatch_unresolved"), lease=lease)
                state = next(s for s in execution.operations if s.operation_id == operation.operation_id)
            record = await self.registry.get_by_block(context, operation.target.block_id)
            indexed = await self.registry.get_by_event(context, operation.event_id)
            if record is None or record != indexed:
                findings.append(Finding(code="reconcile_registry_mismatch", operation_id=operation.operation_id))
                continue
            ledger = await self.executions.get_ledger(context, execution.request_key)
            if ledger.execution != execution:
                raise BindingConflict("reconcile_execution_changed")
            attempt = next(a for a in ledger.attempts if a.operation_id == operation.operation_id)
            # get_event is authoritative; bounded list observations are not used.
            observed = await self.writer.get_event(context, operation.event_id)
            at = instant(self.clock())
            comparison = compare_target(operation, record, observed)
            if comparison == TargetComparison.MATCHED:
                if operation.kind == "create" and record.state == "reserved":
                    await self.registry.confirm(context, record, observed=observed, at=at)
                evidence = OutcomeEvidence(operation_id=operation.operation_id,
                    target_fingerprint=target_fingerprint(operation), kind="read_target", at=at,
                    reference="get_" + attempt.attempt_id, observed_event=observed)
                after = changed(state, status="completed", observed_event=observed,
                                error_code=None, updated_at=instant(self.clock()), reconciled_at=at)
            elif (comparison == TargetComparison.NO_EFFECT or (
                    comparison == TargetComparison.MISSING and operation.kind == "create"
                    and record.state == "reserved")):
                evidence = OutcomeEvidence(operation_id=operation.operation_id,
                    target_fingerprint=target_fingerprint(operation), kind="read_no_effect", at=at,
                    reference="get_" + attempt.attempt_id, observed_event=observed)
                exhausted = state.attempts >= self.max_attempts
                after = changed(state, status="failed" if exhausted else "pending",
                    error_code="apply_attempt_limit" if exhausted else None,
                    updated_at=instant(self.clock()), reconciled_at=at)
            else:
                findings.append(Finding(code="reconcile_target_unresolved", operation_id=operation.operation_id))
                continue
            # T06 checks CURRENT recovery token and original stop/read chronology.
            execution = await self.executions.record_operation(context,
                request_key=execution.request_key, expected_revision=execution.revision,
                operation=OperationState.model_validate(after), lease=lease, evidence=evidence)
        return execution, tuple(findings)
