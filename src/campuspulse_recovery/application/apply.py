"""T19 saved-approved-plan executor; no public tool or final verification here.

Calendar calls occur outside Firestore callbacks. One successful dispatch CAS
permits one writer invocation. Unknown commits/responses are read/reconciled,
never treated as a new permit. Runtime stop integration is injected into T06;
its absence deliberately prevents uncertain-writer retry/clean release.
"""

import asyncio
from datetime import timedelta
from uuid import uuid4

from ..domain.models import ApplyPlanInput, ApplyResult, ExecutionStatus, Finding, OperationStatus, OwnedBlockRecord
from ..domain.ports import BindingConflict
from ..domain.time_rules import instant
from ..integrations.calendar_write import CalendarWriter, TargetComparison, compare_target
from ..security.context import AccessDenied
from ..storage.plans import plan_reference
from .reconcile import ReconciliationService, changed


class _DispatchWriter(CalendarWriter):
    """Per-dispatch T17 composition with a final async application guard.

    T17's registry/source reads can await after the application handoff. Guard
    once more at its I/O boundary, after those reads, without editing/shared
    monkeypatching the accepted writer. Original T17 request/response/ownership
    implementation and original If-Match remain unchanged. This still cannot
    atomically lock Google or cancel an already accepted remote request.
    """

    def __init__(self, writer, before_io):
        super().__init__(writer.clients, writer.registry, writer.coordination,
                         writer.plans, clock=writer.clock)
        self.before_io = before_io

    async def _call(self, context, factory, *, mutation=False):
        if mutation:
            await self.before_io()
        return await super()._call(context, factory, mutation=mutation)


class ApplyService:
    """Trusted startup API: await apply(active_mcp_context, ApplyPlanInput).

    Per-call timeouts bound awaited work, not remote request lifetime. There are
    zero hidden mutation retries. A new invocation reloads the durable ledger.
    """

    def __init__(self, preflight, writer, *, clock, max_attempts=3,
                 deadline_seconds=90.0, write_timeout_seconds=20.0, verification_reader=None):
        if (type(max_attempts) is not int or not 1 <= max_attempts <= 10
                or not 0 < write_timeout_seconds <= deadline_seconds <= 240):
            raise ValueError("invalid apply limits")
        for name in ("registry", "coordination", "plans"):
            if getattr(writer, name) is not getattr(preflight, name):
                raise BindingConflict("apply_writer_authority_mismatch")
        if writer.clients.policy is not preflight.policy:
            raise BindingConflict("apply_writer_authority_mismatch")
        self.preflight, self.writer, self.clock = preflight, writer, clock
        self.executions, self.coordination = preflight.executions, preflight.coordination
        self.registry, self.plans, self.approvals = preflight.registry, preflight.plans, preflight.approvals
        self.policy = preflight.policy
        if verification_reader is not None and (
                verification_reader.documents is not self.plans.documents
                or verification_reader.policy is not self.policy
                or verification_reader.executions is not self.executions
                or getattr(self.executions.verification_guard, "store", None) is not verification_reader):
            raise BindingConflict("stored_verification_reader_authority_mismatch")
        self.verification_reader = verification_reader
        self.max_attempts = max_attempts
        self.deadline_seconds, self.write_timeout_seconds = deadline_seconds, write_timeout_seconds
        self.reconciler = ReconciliationService(self.executions, self.registry, writer,
            clock=clock, max_attempts=max_attempts)

    def _now(self):
        return instant(self.clock())

    def _scope(self, context):
        self.policy.require_context(context)
        if context.boundary != "mcp":
            raise AccessDenied("mcp_boundary_required")

    async def _transition(self, context, execution, status, lease):
        return await self.executions.compare_and_set(context,
            expected_revision=execution.revision, execution=changed(execution,
                revision=execution.revision + 1, updated_at=self._now(), status=status), lease=lease)

    async def _send_guard(self, context, plan, lease, request_key):






        execution = await self.executions.get(context, request_key)
        if execution.plan != plan_reference(plan):
            raise BindingConflict("apply_guard_execution_plan_mismatch")


        await self.coordination.assert_current(context, lease, now=self._now())
        approval = await self.approvals.require_active(context, plan_reference(plan), self._now())

        self._scope(context)
        now = self._now()
        if not approval.active_at(now):
            raise BindingConflict("apply_consent_expired_before_send")
        if not lease.acquired_at <= now < lease.expires_at:
            raise BindingConflict("apply_lease_expired_before_send")
        completed = {s.operation_id for s in execution.operations if s.status == OperationStatus.COMPLETED}
        if any(b.interval.start <= now for b in plan.payload.blocks) or any(
                o.kind == "update" and o.operation_id not in completed and o.before.start <= now
                for o in plan.payload.operations):
            raise BindingConflict("apply_target_started_before_send")

    async def _ownership(self, context, plan, operation):
        if operation.kind == "create":
            return await self.registry.reserve(context, OwnedBlockRecord(
                owner_id=plan.payload.owner_id, calendar_id=plan.payload.calendar_id,
                block_id=operation.target.block_id, assignment_id=operation.target.assignment_id,
                event_id=operation.event_id, state="reserved", intent_plan=plan_reference(plan),
                intent_operation_id=operation.operation_id, reserved_at=self._now()))
        record = await self.registry.get_by_block(context, operation.target.block_id)
        if record is None or record != await self.registry.get_by_event(context, operation.event_id):
            raise BindingConflict("apply_registry_join_mismatch")
        return record

    async def _unknown(self, context, execution, operation_id, lease):


        execution = await self.executions.get(context, execution.request_key)
        state = next(s for s in execution.operations if s.operation_id == operation_id)
        if state.status == OperationStatus.IN_FLIGHT:
            execution = await self.executions.record_operation(context,
                request_key=execution.request_key, expected_revision=execution.revision,
                operation=changed(state, status="unknown", updated_at=self._now(),
                                  error_code="apply_write_outcome_unknown"), lease=lease)
        return execution

    async def _write(self, context, plan, operation, record, lease, request_key):
        writer = _DispatchWriter(self.writer, lambda: self._send_guard(context, plan, lease, request_key))
        return await getattr(writer, operation.kind)(context, operation, record, lease)

    async def apply(self, context, request: ApplyPlanInput) -> ApplyResult:
        self._scope(context)
        request = ApplyPlanInput.model_validate(request)

        plan = await self.plans.get(context, request.plan_id, request.version)
        execution = await self.executions.bind_request(context, request_key=request.request_key,
            plan=plan_reference(plan), at=self._now())
        if execution.status == ExecutionStatus.VERIFIED:
            if self.verification_reader is None:
                raise BindingConflict("stored_verification_replay_integration_required")
            verification = await self.verification_reader.get_verified(context, execution)
            latest = await self.verification_reader.latest(context, execution.plan)
            self._scope(context)
            return ApplyResult(execution=execution, verification=verification,
                findings=() if latest.result == verification else (
                    Finding(code="verify_later_observation_available"), *latest.result.findings))
        if execution.status in (ExecutionStatus.APPLIED_UNVERIFIED, ExecutionStatus.FAILED,
                                ExecutionStatus.REJECTED, ExecutionStatus.VERIFICATION_FAILED):
            return ApplyResult(execution=execution)
        lease = None
        findings = []
        try:
            async with asyncio.timeout(self.deadline_seconds):
                now = self._now()
                lease = await self.coordination.acquire(context, holder_id="apply_" + uuid4().hex,
                    now=now, expires_at=now + timedelta(seconds=self.deadline_seconds + 5))


                execution = (await self.executions.get_ledger(context, request.request_key)).execution
                if any(s.status in (OperationStatus.IN_FLIGHT, OperationStatus.UNKNOWN) for s in execution.operations):
                    execution, recovered = await self.reconciler.reconcile(context, plan, execution, lease)
                    findings.extend(recovered)
                if any(s.status in (OperationStatus.IN_FLIGHT, OperationStatus.UNKNOWN) for s in execution.operations):
                    findings.append(Finding(code="apply_reconciliation_required"))
                else:
                    lease = await self.coordination.complete_recovery(context, lease, now=self._now())
                    execution, pending_findings = await self._pending(context, request, plan, execution, lease)
                    findings.extend(pending_findings)
        except asyncio.CancelledError:


            raise
        except Exception:

            findings.append(Finding(code="apply_interrupted_recovery_required"))
        finally:
            if lease is not None:
                try:
                    await self.coordination.release(context, lease)
                except Exception:

                    findings.append(Finding(code="apply_lease_recovery_required"))


        execution = await self.executions.get(context, request.request_key)
        return ApplyResult(execution=execution, findings=tuple(findings))

    async def _pending(self, context, request, plan, execution, lease):
        for operation in plan.payload.operations:
            state = next(s for s in execution.operations if s.operation_id == operation.operation_id)
            if state.status == OperationStatus.COMPLETED:
                continue
            if state.status != OperationStatus.PENDING:
                return execution, (Finding(code="apply_replan_required"),)

            ready = await self.preflight.check(context, request, lease)
            execution = ready.execution
            if ready.status != "ready":
                return execution, ready.findings + (Finding(code="apply_replan_required"),)
            if state.attempts >= self.max_attempts:
                return execution, (Finding(code="apply_attempt_limit", operation_id=operation.operation_id),)
            if execution.status == ExecutionStatus.PROPOSED:
                execution = await self._transition(context, execution, "approved", lease)
            if execution.status == ExecutionStatus.APPROVED:
                execution = await self._transition(context, execution, "applying", lease)
            record = await self._ownership(context, plan, operation)
            await self._send_guard(context, plan, lease, request.request_key)


            execution = await self.executions.record_operation(context,
                request_key=request.request_key, expected_revision=execution.revision,
                operation=changed(state, status="in_flight", attempts=state.attempts + 1,
                    updated_at=self._now(), fencing_generation=lease.generation,
                    observed_event=None, reconciled_at=None, error_code=None), lease=lease)
            try:
                await self._send_guard(context, plan, lease, request.request_key)
                observed = await asyncio.wait_for(self._write(
                    context, plan, operation, record, lease, request.request_key), timeout=self.write_timeout_seconds)
                await self.coordination.assert_current(context, lease, now=self._now())
                if compare_target(operation, record, observed) != TargetComparison.MATCHED:
                    raise BindingConflict("apply_response_target_mismatch")
                dispatched = next(s for s in execution.operations if s.operation_id == operation.operation_id)
                execution = await self.executions.record_operation(context,
                    request_key=request.request_key, expected_revision=execution.revision,
                    operation=changed(dispatched, status="completed", updated_at=self._now(),
                                      observed_event=observed), lease=lease)
            except BaseException as error:


                try:
                    execution = await self._unknown(context, execution, operation.operation_id, lease)
                    if not isinstance(error, asyncio.CancelledError):
                        execution, findings = await self.reconciler.reconcile(context, plan, execution, lease)
                    else:
                        findings = ()
                except Exception:
                    findings = (Finding(code="apply_outcome_checkpoint_unresolved"),)
                if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                    raise
                return execution, findings + (Finding(code="apply_reconciliation_required"),)

        if not plan.payload.operations:
            ready = await self.preflight.check(context, request, lease)
            if ready.findings:
                return execution, ready.findings
            if execution.status == ExecutionStatus.PROPOSED:
                execution = await self._transition(context, execution, "approved", lease)
            if execution.status == ExecutionStatus.APPROVED:
                execution = await self._transition(context, execution, "applying", lease)
            if execution.status == ExecutionStatus.APPLYING:
                execution = await self._transition(context, execution, "applied_unverified", lease)
        return execution, ()
