"""Moodle input freshness in the same transaction as consent/dispatch checks."""
import re

from ..domain.models import PlanReference, SourceKind
from ..domain.ports import BindingConflict
from ..storage.plans import plan_reference
from ..web.approval import FirestoreApprovalStore, TransactionalUIApprovalGuard
from .planning import current_cache, input_version, project_assignments


class MoodleApprovalStore(FirestoreApprovalStore):
    def __init__(self, documents, policy, *, inputs, snapshots, clock):
        super().__init__(documents, policy, clock=clock)
        if any(item.documents is not documents or item.policy is not policy for item in (inputs, snapshots)):
            raise BindingConflict('moodle_approval_authority_mismatch')
        self.inputs, self.snapshots = inputs, snapshots

    def transaction_guard(self):
        # Inherited require_active also uses this guard, so the last application
        # check immediately before Calendar I/O sees current student input.
        return MoodleApprovalGuard(self)

    async def validate_approval_basis(self, context, transaction, reference, now):
        await self.transaction_guard().require_planning(context, transaction, reference)


class MoodleApprovalGuard(TransactionalUIApprovalGuard):
    async def require_active(self, context, transaction, reference, now):
        reference = PlanReference.model_validate(reference)
        approval = await super().require_active(context, transaction, reference, now)
        await self.require_planning(context, transaction, reference)
        return approval

    async def require_planning(self, context, transaction, reference):
        store = self.store
        plan = await store._load_plan(context, transaction, reference.plan_id, reference.version)
        if plan_reference(plan) != reference:
            raise BindingConflict('moodle_planning_changed')
        state = await store.inputs.get(context, reader=transaction)
        source = next(r for r in plan.payload.source_references if r.kind == SourceKind.ASSIGNMENTS)
        original = await store.snapshots._load_snapshot(context, transaction, source, current_run=False)
        version = original.source.source_version or ''
        expected = re.escape(input_version(state)) + r'[0-9a-f]{64}'
        if (re.fullmatch(expected, version) is None or plan.payload.constraints !=
                store.inputs.constraints(state, now=plan.payload.constraints.horizon.start)):
            raise BindingConflict('moodle_planning_changed')
        cached = await store.inputs.cached(context, reader=transaction)
        if not current_cache(cached, store.clock()):
            raise BindingConflict('moodle_planning_changed')
        assignments = project_assignments(cached.snapshot, state, resource_id=store.inputs.resource_id,
                                          timezone=store.policy.config.student.timezone)
        def work(items):
            return {a.assignment_id: (None if a.deadline is None else a.deadline.at, a.remaining_minutes)
                    for a in items}
        # A routine sync refresh with unchanged work does not revoke consent.
        if work(assignments) != work(original.assignments):
            raise BindingConflict('moodle_planning_changed')
        store.policy.require_context(context)
