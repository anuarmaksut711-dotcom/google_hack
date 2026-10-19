"""Independent T20 observations and append-only proof, never Calendar mutations.

The startup composition is trusted, the tool input is not. Readers save actual
sources before return. Proof consumption reloads sources, registry, immutable
creation intent and the canonical ledger in the owning store's SAME transaction.
An observation is a fact at its saved time, not an atomic Calendar lock.
"""

from __future__ import annotations

from ..domain.models import (
    Contract, Digest, Finding, Identifier, Instant, OperationStatus, OwnedBlockRecord,
    PlanReference, SourceKind, SourceReference, VerificationResult, VerifyPlanInput,
    calendar_event_id, canonical_hash,
)
from ..domain.ports import BindingConflict, IncompleteRead, PortError
from ..domain.time_rules import instant
from ..security.context import AccessDenied
from ..storage.operations import _Audit, _Ledger, _request_path, target_fingerprint
from ..storage.plans import _Repository, _decode, _encode, _key, plan_reference
from .preflight import _covers, _work


def _require(ok, code):
    if not ok:
        raise BindingConflict(code)


class VerificationCheck(Contract):
    code: Identifier
    passed: bool
    block_id: Identifier | None = None
    assignment_id: Identifier | None = None


class VerificationEvidence(Contract):
    """Internal immutable envelope; not an extra wire contract or public tool."""

    owner_id: Identifier
    calendar_id: str
    config_revision: Identifier
    project_id: str
    database_id: str
    expected_ledger: _Ledger
    assignment_reference: SourceReference | None
    calendar_reference: SourceReference | None
    read_failures: tuple[Identifier, ...]
    observed_at: Instant
    registry_digest: Digest
    checks: tuple[VerificationCheck, ...]
    result: VerificationResult


class _Latest(Contract):
    proof_id: Digest
    observed_at: Instant


def _root(trusted, reference):
    return "cp_verifications/" + _key(trusted.owner_id, trusted.calendar_id,
                                     reference.plan_id, reference.version)


def _registry_hash(records):
    return canonical_hash([[None if r is None else r.model_dump(mode="json"),
                            None if e is None else e.model_dump(mode="json"), valid]
                           for r, e, valid in records])


def _evaluate(plan, ledger, old_a, old_c, fresh_a, fresh_c, records, attempted, at, run_id,
              failures):
    """Pure whole-plan semantic checks; work never decreases from Calendar age."""
    findings, checks = [], []

    def check(code, ok, *, block=None, assignment=None, deficit=None, deadline=None):
        checks.append(VerificationCheck(code=code, passed=bool(ok), block_id=block,
                                        assignment_id=assignment))
        if not ok:
            findings.append(Finding(code=code, block_id=block, assignment_id=assignment,
                                    deficit_minutes=deficit, deadline=deadline))

    horizon = plan.payload.constraints.horizon
    complete = all(s is not None and s.source.complete for s in (fresh_a, fresh_c))
    coverage = (fresh_c is not None and fresh_c.recurrence_expanded
                and _covers(fresh_c.source.covered_interval, horizon)
                and (fresh_a is not None and (fresh_a.source.covered_interval is None
                     or _covers(fresh_a.source.covered_interval, horizon))))
    check("verify_complete_sources", complete and coverage and not failures)
    for failure in failures:
        check(failure, False)
    all_completed = (ledger.execution.status in ("applied_unverified", "verified", "verification_failed")
                     and all(s.status == OperationStatus.COMPLETED for s in ledger.execution.operations))
    check("verify_execution_incomplete", all_completed)
    if fresh_a is not None:
        check("verify_assignments_stale_replan", _work(old_a) == _work(fresh_a))
        resolved = all(a.deadline is not None and a.remaining_minutes is not None
                       for a in fresh_a.assignments)
        check("verify_work_or_deadline_unknown", resolved)
    else:
        resolved = False
    events = {} if fresh_c is None else {e.event_id: e for e in fresh_c.events}
    owned = {} if fresh_c is None else {b.block_id: b for b in fresh_c.owned_blocks}
    old_blocks = {b.block_id: b for b in old_c.owned_blocks}
    operations = {o.target.block_id: o for o in plan.payload.operations}
    states = {s.operation_id: s for s in ledger.execution.operations}
    target_ids = {b.block_id for b in plan.payload.blocks}
    for bid, old in old_blocks.items():
        if old.interval.end > plan.payload.created_at and old.interval.start < horizon.end:
            check("verify_preserved_block_omitted", bid in target_ids, block=bid)
    matches = {}
    for target, (record, indexed, intent_valid) in zip(plan.payload.blocks, records):
        bid = target.block_id
        eid = calendar_event_id(plan.payload.owner_id, plan.payload.calendar_id, bid)
        actual = events.get(eid)
        operation = operations.get(bid)
        identity = (record is not None and indexed == record and record.state == "confirmed"
                    and (record.owner_id, record.calendar_id, record.block_id, record.assignment_id,
                         record.event_id) == (plan.payload.owner_id, plan.payload.calendar_id, bid,
                                              target.assignment_id, eid) and intent_valid)
        check("verify_registry_target_mismatch", identity, block=bid)
        marker = None if actual is None else actual.campuspulse_marker
        matching = (identity and actual is not None and actual.etag is not None
                    and actual.interval == target.interval and actual.status == "confirmed"
                    and actual.transparency == "opaque" and actual.recurrence is None
                    and actual.all_day_start is None and marker is not None
                    and (marker.owner_id, marker.calendar_id, marker.block_id, marker.assignment_id,
                         marker.create_operation_id) == (record.owner_id, record.calendar_id, bid,
                             target.assignment_id, record.intent_operation_id)
                    and bid in owned and owned[bid].interval == target.interval)
        check("verify_event_target_mismatch", matching, block=bid)
        if operation is None:
            old = old_blocks.get(bid)
            check("verify_preserved_block_changed", old is not None
                  and (old.event_id, old.assignment_id, old.interval) == (eid, target.assignment_id,
                                                                       target.interval), block=bid)
        else:
            state = states[operation.operation_id]
            observed = state.observed_event
            check("verify_operation_target_mismatch", operation.target == target
                  and ledger.target_fingerprints[plan.payload.operations.index(operation)] == target_fingerprint(operation)
                  and observed is not None and observed.interval == target.interval
                  and observed.event_id == eid and matching
                  and observed.campuspulse_marker == actual.campuspulse_marker
                  and observed.status == "confirmed" and observed.transparency == "opaque"
                  and observed.recurrence is None and observed.all_day_start is None
                  and (operation.kind != "update" or actual.etag != operation.expected_etag)
                  and (operation.kind != "create" or (record.intent_plan == plan_reference(plan)
                       and record.intent_operation_id == operation.operation_id)), block=bid)
        check("verify_block_started_replan", target.interval.start > at, block=bid)
        matches[bid] = matching
    expected_ids = {calendar_event_id(plan.payload.owner_id, plan.payload.calendar_id, b.block_id)
                    for b in plan.payload.blocks}
    external = [e for e in events.values() if e.event_id not in expected_ids and e.busy]
    for b in plan.payload.blocks:
        check("verify_external_busy_conflict", not any(e.interval is not None
              and b.interval.start < e.interval.end and e.interval.start < b.interval.end
              for e in external), block=b.block_id)
    if resolved:
        assignments = {a.assignment_id: a for a in fresh_a.assignments}
        allocations = {a.assignment_id: a for a in plan.payload.allocations}
        check("verify_assignment_set_stale_replan", set(assignments) == set(allocations))
        for aid, a in assignments.items():
            allocation = allocations.get(aid)
            deferred = a.deadline.at > horizon.end
            check("verify_work_allocation_stale_replan", allocation is not None
                  and allocation.remaining_minutes == a.remaining_minutes
                  and allocation.required_minutes == a.required_minutes
                  and allocation.deferred == deferred, assignment=aid)
            blocks = [b for b in plan.payload.blocks if b.assignment_id == aid]
            check("verify_deadline_mismatch", all(b.interval.end <= a.deadline.at for b in blocks),
                  assignment=aid, deadline=a.deadline.at)

            minutes = sum(int(b.interval.minutes) for b in blocks if matches[b.block_id]
                          and b.interval.start > at and b.interval.end <= a.deadline.at)
            deficit = max(0, a.required_minutes - minutes)
            check("verify_remaining_capacity_deficit", deferred or deficit == 0,
                  assignment=aid, deficit=deficit, deadline=a.deadline.at)


        for deadline in sorted({a.deadline.at for a in assignments.values() if a.deadline.at <= horizon.end}):
            required = sum(a.required_minutes for a in assignments.values() if a.deadline.at <= deadline)
            slots = 0
            from datetime import timedelta


            start = horizon.start
            while start + timedelta(minutes=15) <= min(horizon.end, deadline):
                end = start + timedelta(minutes=15)
                if (start > at and any(window.start <= start and end <= window.end
                        for window in plan.payload.constraints.working_windows or ())
                        and not any(e.interval is not None and start < e.interval.end
                                    and e.interval.start < end for e in external)):
                    slots += 15
                start = end
            check("verify_deadline_capacity_deficit", slots >= required,
                  deficit=max(0, required - slots), deadline=deadline)
    if not all_completed:
        status = "unknown" if any(s.status in ("unknown", "in_flight") for s in ledger.execution.operations) else "applied_unverified"
    elif not complete or not coverage or failures or not resolved:
        status = "applied_unverified"
    else:
        status = "verification_failed" if findings else "verified"
    return VerificationResult(plan=plan_reference(plan), run_id=run_id, status=status,
        attempted_at=attempted, verification_at=at,
        calendar_source=None if fresh_c is None else fresh_c.source, findings=tuple(findings),
        observed_events=tuple(e for eid, e in sorted(events.items()) if eid in expected_ids),
        checked_operations=tuple(o.operation_id for o in plan.payload.operations)
                           if fresh_c is not None and fresh_c.source.complete else ()), tuple(checks)


class VerificationService(_Repository):
    """Concrete service + evidence adapter. Startup passes identical stores/readers.

    `verify(context, VerifyPlanInput)` performs new reads. `resume` consumes one
    saved proof after ambiguous commits without inventing another observation.
    `append(context,result)` only acknowledges an already captured persisted
    result; arbitrary DTOs cannot manufacture proof. No public input extensions.
    """

    def __init__(self, plans, snapshots, executions, registry, assignments, calendar, policy, *, clock):
        super().__init__(plans.documents, policy)
        for store in (plans, snapshots, executions, registry):
            _require(store.documents is self.documents and store.policy is policy,
                     "verification_store_authority_mismatch")
        for reader in (assignments, calendar):
            _require(reader.clients.policy is policy and reader.snapshots is snapshots,
                     "verification_reader_authority_mismatch")
        _require(calendar.registry is registry, "verification_registry_mismatch")
        self.plans, self.snapshots, self.executions, self.registry = plans, snapshots, executions, registry
        self.assignments, self.calendar, self.clock = assignments, calendar, clock

    def _mcp(self, context):
        trusted = self.policy.require_context(context)
        if context.boundary != "mcp":
            raise AccessDenied("mcp_boundary_required")
        return trusted

    def transaction_guard(self):
        return _VerificationGuard(self)

    async def _inputs(self, context, reader, plan, a_ref, c_ref, *, current):
        originals = {ref.kind: await self._load_snapshot(context, reader, ref, current_run=False)
                     for ref in plan.payload.source_references}
        a = None if a_ref is None else await self._load_snapshot(context, reader, a_ref, current_run=current)
        c = None if c_ref is None else await self._load_snapshot(context, reader, c_ref, current_run=current)
        _require(a_ref is None or a_ref.kind == SourceKind.ASSIGNMENTS, "verification_source_kind_mismatch")
        _require(c_ref is None or c_ref.kind == SourceKind.CALENDAR, "verification_source_kind_mismatch")
        records = []
        trusted = self._mcp(context)
        old_a, old_c = originals[SourceKind.ASSIGNMENTS], originals[SourceKind.CALENDAR]
        _require(old_a.source.complete and old_c.source.complete and old_c.recurrence_expanded
            and _covers(old_c.source.covered_interval, plan.payload.constraints.horizon)
            and old_a.source.timezone == trusted.timezone
            and (a is None or a.source.timezone == trusted.timezone),
            "verification_source_configuration_mismatch")
        for target in plan.payload.blocks:
            eid = calendar_event_id(trusted.owner_id, trusted.calendar_id, target.block_id)
            root = "cp_calendars/" + _key(trusted.calendar_id)
            left, right = await reader.get(root + "/blocks/" + _key(target.block_id)), await reader.get(root + "/events/" + _key(eid))
            left = None if left is None else _decode(left, "owned_block", OwnedBlockRecord)
            right = None if right is None else _decode(right, "owned_block", OwnedBlockRecord)
            valid = False
            if left is not None and right == left:
                try:
                    self.registry._bound(context, left)
                    await self.registry._intent(context, reader, left)
                    valid = True
                except BindingConflict:
                    pass
            records.append((left, right, valid))
        return originals[SourceKind.ASSIGNMENTS], originals[SourceKind.CALENDAR], a, c, tuple(records)

    async def _validate(self, context, tx, evidence, plan, ledger, *, current):
        trusted = self._mcp(context)
        _require((evidence.owner_id, evidence.calendar_id, evidence.config_revision,
                  evidence.project_id, evidence.database_id, evidence.result.plan) == (
            trusted.owner_id, trusted.calendar_id, trusted.config_revision,
            self.documents.project_id, self.documents.database_id, plan_reference(plan)),
            "verification_proof_binding_mismatch")
        _require(evidence.expected_ledger == ledger, "verification_execution_revision_changed")
        self._ledger(ledger)
        if current:
            _require(evidence.result.run_id == trusted.run_id, "verification_proof_run_mismatch")
        inputs = await self._inputs(context, tx, plan, evidence.assignment_reference,
                                    evidence.calendar_reference, current=current)
        for source in inputs[2:4]:
            if source is not None:
                ref = source.source.reference
                _require(ref.run_id == evidence.result.run_id
                    and evidence.result.attempted_at <= ref.read_at <= evidence.observed_at
                    and ref.snapshot_id not in {r.snapshot_id for r in plan.payload.source_references},
                    "verification_fresh_source_required")
        _require(evidence.observed_at <= instant(self.clock())
                 and evidence.result.attempted_at >= ledger.execution.updated_at,
                 "verification_proof_time_invalid")
        expected, checks = _evaluate(plan, ledger, *inputs, evidence.result.attempted_at,
                                     evidence.observed_at, evidence.result.run_id, evidence.read_failures)
        _require((expected, checks, _registry_hash(inputs[4])) == (
            evidence.result, evidence.checks, evidence.registry_digest), "verification_proof_checks_mismatch")
        self._mcp(context)
        return evidence.result

    @staticmethod
    def _ledger(ledger):
        state = ledger.execution
        attempts = {a.operation_id: a for a in ledger.attempts}
        _require(len(attempts) == len(ledger.attempts)
                 and set(attempts) <= {s.operation_id for s in state.operations},
                 "verification_ledger_attempt_invalid")
        path = _request_path(state.owner_id, state.calendar_id, state.request_key)
        for i, op in enumerate(state.operations):
            a = attempts.get(op.operation_id)
            if op.attempts == 0:
                _require(a is None and op.status in ("pending", "failed"),
                         "verification_ledger_attempt_invalid")
            else:
                _require(a is not None and a.ordinal == op.attempts
                    and a.fencing_generation == op.fencing_generation
                    and a.target_fingerprint == ledger.target_fingerprints[i]
                    and a.started_at <= op.updated_at <= state.updated_at
                    and a.attempt_id == "att_" + _key(path, op.operation_id, a.ordinal, a.fencing_generation),
                    "verification_ledger_attempt_invalid")

    async def verify(self, context, request):
        trusted = self._mcp(context)
        request = VerifyPlanInput.model_validate(request)
        plan = await self.plans.get(context, request.plan_id, request.version)
        ledger = await self.executions.get_ledger_for_plan(context, plan_reference(plan))
        attempted = instant(self.clock())
        a_ref, c_ref, failures = None, None, []
        for name, reader in (("assignments", self.assignments), ("calendar", self.calendar)):
            try:
                fresh = await reader.read(context, plan.payload.constraints.horizon)


                self.policy.require_source(context, fresh.source.reference)
                _require(fresh.source.reference.kind == name, "verification_source_kind_mismatch")
                if name == "assignments":
                    a_ref = fresh.source.reference
                else:
                    c_ref = fresh.source.reference
            except (IncompleteRead, PortError) as error:
                if isinstance(error, BindingConflict):
                    raise
                failures.append("verify_" + name + "_read_unavailable")
        self._mcp(context)

        async def capture(tx):
            _, current, saved = await self.executions._for_plan(context, tx, plan_reference(plan))
            _require(current == ledger, "verification_execution_revision_changed")
            self._ledger(ledger)
            inputs = await self._inputs(context, tx, saved, a_ref, c_ref, current=True)
            self._mcp(context)
            at = instant(self.clock())
            for source in inputs[2:4]:
                if source is not None:
                    ref = source.source.reference
                    _require(attempted <= ref.read_at <= at
                        and ref.snapshot_id not in {r.snapshot_id for r in plan.payload.source_references},
                        "verification_fresh_source_required")
            result, checks = _evaluate(saved, ledger, *inputs, attempted, at, trusted.run_id, tuple(failures))
            return VerificationEvidence(owner_id=trusted.owner_id, calendar_id=trusted.calendar_id,
                config_revision=trusted.config_revision, project_id=self.documents.project_id,
                database_id=self.documents.database_id, expected_ledger=ledger,
                assignment_reference=a_ref, calendar_reference=c_ref, read_failures=tuple(failures),
                observed_at=at, registry_digest=_registry_hash(inputs[4]), checks=checks, result=result)
        evidence = await self.documents.transact(capture)
        proof_id = await self._append_evidence(context, evidence)
        if evidence.result.status in ("verified", "verification_failed"):
            try:
                await self.executions.accept_verification(context, reference=plan_reference(plan), proof_id=proof_id)
            except PortError as error:
                if isinstance(error, BindingConflict):
                    raise


                await self.executions.accept_verification(context, reference=plan_reference(plan), proof_id=proof_id)
        self._mcp(context)
        return evidence.result

    async def _append_evidence(self, context, evidence):
        trusted = self._mcp(context)
        proof_id = canonical_hash(evidence)
        root = _root(trusted, evidence.result.plan)
        document = _encode("verification", evidence)
        async def append(tx):
            existing = await tx.get(root + "/observations/" + proof_id)
            latest_doc = await tx.get(root + "/index/latest")
            _, ledger, plan = await self.executions._for_plan(context, tx, evidence.result.plan)
            if existing is not None:
                _require(existing == document, "verification_immutable_proof_conflict")
                if ledger != evidence.expected_ledger:
                    state = ledger.execution
                    path = _request_path(state.owner_id, state.calendar_id, state.request_key)
                    historical = _decode(await tx.get(path + f"/history/r{evidence.expected_ledger.execution.revision}"),
                                         "execution_audit", _Audit)
                    _require(historical.ledger == evidence.expected_ledger,
                             "verification_execution_revision_changed")
                    ledger = historical.ledger
                await self._validate(context, tx, evidence, plan, ledger, current=False)
            else:
                await self._validate(context, tx, evidence, plan, ledger, current=True)
            latest = None if latest_doc is None else _decode(latest_doc, "verification_latest", _Latest)
            value = _Latest(proof_id=proof_id, observed_at=evidence.observed_at)
            self._mcp(context)
            if existing is None:
                tx.create(root + "/observations/" + proof_id, document)



            if latest is None or (existing is None and value.observed_at >= latest.observed_at):
                tx.set(root + "/index/latest", _encode("verification_latest", value))
            return proof_id
        try:
            return await self.documents.transact(append)
        except PortError as error:
            if isinstance(error, BindingConflict):
                raise

            _require(await self.documents.get(root + "/observations/" + proof_id) == document,
                     "verification_append_outcome_unknown")
            self._mcp(context)
            return proof_id

    async def get_evidence(self, context, reference, proof_id):
        self._mcp(context)
        async def read(tx):
            _, ledger, plan = await self.executions._for_plan(context, tx, reference)
            evidence = await self._read(context, tx, plan_reference(plan), proof_id)
            if ledger != evidence.expected_ledger:
                state = ledger.execution
                path = _request_path(state.owner_id, state.calendar_id, state.request_key)
                historical = _decode(await tx.get(path + f"/history/r{evidence.expected_ledger.execution.revision}"),
                                     "execution_audit", _Audit)
                _require(historical.ledger == evidence.expected_ledger, "verification_execution_revision_changed")
                ledger = historical.ledger
            await self._validate(context, tx, evidence, plan, ledger, current=False)
            self._mcp(context)
            return evidence
        return await self.documents.transact(read)

    async def _read(self, context, reader, reference, proof_id):
        from pydantic import TypeAdapter
        TypeAdapter(Digest).validate_python(proof_id)
        reference = PlanReference.model_validate(reference)
        trusted = self._mcp(context)
        value = _decode(await reader.get(_root(trusted, reference) + "/observations/" + proof_id),
                        "verification", VerificationEvidence)
        _require(canonical_hash(value) == proof_id and value.result.plan == reference,
                 "verification_proof_identity_mismatch")
        _require((value.owner_id, value.calendar_id, value.config_revision,
                  value.project_id, value.database_id) == (trusted.owner_id, trusted.calendar_id,
                  trusted.config_revision, self.documents.project_id, self.documents.database_id),
                  "verification_proof_binding_mismatch")
        return value

    async def latest(self, context, reference):
        trusted = self._mcp(context)
        async def read(tx):
            _, ledger, plan = await self.executions._for_plan(context, tx, reference)
            pointer = _decode(await tx.get(_root(trusted, reference) + "/index/latest"),
                              "verification_latest", _Latest)
            evidence = await self._read(context, tx, plan_reference(plan), pointer.proof_id)
            _require(pointer.observed_at == evidence.observed_at, "verification_latest_mismatch")
            if evidence.expected_ledger != ledger:
                state = ledger.execution
                path = _request_path(state.owner_id, state.calendar_id, state.request_key)
                historical = _decode(await tx.get(path + f"/history/r{evidence.expected_ledger.execution.revision}"),
                                     "execution_audit", _Audit)
                _require(historical.ledger == evidence.expected_ledger, "verification_execution_revision_changed")
                ledger = historical.ledger
            await self._validate(context, tx, evidence, plan, ledger, current=False)
            self._mcp(context)
            return evidence
        return await self.documents.transact(read)

    async def append(self, context, result):
        """Protocol acknowledgment only; a result DTO is never new read evidence."""
        result = VerificationResult.model_validate(result)
        evidence = await self.latest(context, result.plan)
        _require(evidence.result == result and result.run_id == self._mcp(context).run_id,
                 "verification_captured_persisted_evidence_required")

    async def resume(self, context, reference, proof_id):
        """Resume one actual immutable proof; no new read/time/success is invented."""
        await self.executions.accept_verification(context, reference=reference, proof_id=proof_id)
        return (await self.get_evidence(context, reference, proof_id)).result

    async def get_verified(self, context, execution):
        saved, result = await self.executions.saved_verification(context, execution.request_key)
        _require(saved == execution and saved.status == result.status == "verified",
                 "stored_verification_execution_mismatch")
        self._mcp(context)
        return result


class _VerificationGuard:
    """Trusted transactional reader, never a model callback or nested tx."""

    def __init__(self, store):
        self.store, self.documents, self.policy = store, store.documents, store.policy

    async def require_current(self, context, tx, proof_id, ledger, plan):
        evidence = await self.store._read(context, tx, plan_reference(plan), proof_id)
        latest = _decode(await tx.get(_root(self.store._mcp(context), plan_reference(plan)) + "/index/latest"),
                         "verification_latest", _Latest)
        _require(latest.proof_id == proof_id and latest.observed_at == evidence.observed_at,
                 "verification_newer_observation_exists")


        return await self.store._validate(context, tx, evidence, plan, ledger, current=False)

    async def require_consumed(self, context, tx, proof_id, ledger, plan, link):
        _require(isinstance(link, dict) and set(link) == {
            "proof_id", "before_revision", "after_revision", "ledger_digest"}
            and link["proof_id"] == proof_id and type(link["before_revision"]) is int
            and type(link["after_revision"]) is int and link["after_revision"] == link["before_revision"] + 1,
            "verification_consumption_invalid")
        path = _request_path(ledger.execution.owner_id, ledger.execution.calendar_id, ledger.execution.request_key)
        actual_link = await tx.get(path + "/verification/" + proof_id)
        before = _decode(await tx.get(path + f"/history/r{link['before_revision']}"), "execution_audit", _Audit)
        after = _decode(await tx.get(path + f"/history/r{link['after_revision']}"), "execution_audit", _Audit)
        _require(actual_link == link and canonical_hash(after.ledger) == link["ledger_digest"]
                 and before.ledger.execution.revision == link["before_revision"]
                 and after.ledger.execution.revision == link["after_revision"]
                 and after.ledger.execution.operations == before.ledger.execution.operations
                 and after.ledger.attempts == before.ledger.attempts
                 and after.ledger.target_fingerprints == before.ledger.target_fingerprints
                 and after.ledger.execution.run_id == before.ledger.execution.run_id
                 and after.ledger.execution.plan == before.ledger.execution.plan == ledger.execution.plan
                 and after.ledger.execution.request_key == ledger.execution.request_key
                 and after.ledger.execution.revision <= ledger.execution.revision,
                 "verification_consumption_invalid")
        evidence = await self.store._read(context, tx, plan_reference(plan), proof_id)
        result = await self.store._validate(context, tx, evidence, plan, before.ledger, current=False)
        _require(after.ledger.execution.status == result.status
                 and result.status in ("verified", "verification_failed"), "verification_consumption_invalid")
        return result
