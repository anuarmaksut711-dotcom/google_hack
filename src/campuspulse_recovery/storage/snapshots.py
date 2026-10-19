"""Authoritative immutable source snapshots, not a cache of model arguments."""

from __future__ import annotations

from ..domain.models import AssignmentsSnapshot, CalendarSnapshot, OwnedBlockRecord, PlanReference, SourceKind, SourceReference
from ..domain.ports import BindingConflict, TrustedContext
from ..security.context import TrustedRequestContext
from .plans import _Repository, _decode, _encode, _key, _snapshot_path, plan_reference


class FirestoreSnapshotStore(_Repository):
    async def _put(self, context, snapshot, model):
        context = self._request(context)
        self._trusted(context, writer="mcp")
        snapshot = model.model_validate(snapshot)
        reference = snapshot.source.reference
        self.policy.require_source(context, reference)
        document = _encode("snapshot_" + reference.kind, snapshot)
        path = _snapshot_path(reference)

        async def publish(tx):
            self._trusted(context, writer="mcp")
            self.policy.require_source(context, reference)
            existing = await tx.get(path)
            if existing is not None:
                _decode(existing, "snapshot_" + reference.kind, model)
                if existing != document:
                    raise BindingConflict("immutable_snapshot_conflict")
            else:
                if isinstance(snapshot, CalendarSnapshot):
                    for block in snapshot.owned_blocks:
                        root = f"cp_calendars/{_key(reference.resource_id)}"
                        record = _decode(await tx.get(f"{root}/blocks/{_key(block.block_id)}"), "owned_block", OwnedBlockRecord)
                        event_record = _decode(await tx.get(f"{root}/events/{_key(block.event_id)}"), "owned_block", OwnedBlockRecord)
                        event = next(event for event in snapshot.events if event.event_id == block.event_id)
                        if record != event_record or record.state != "confirmed" or (
                            record.owner_id, record.calendar_id, record.block_id, record.assignment_id, record.event_id,
                            record.intent_operation_id
                        ) != (block.owner_id, block.calendar_id, block.block_id, block.assignment_id, block.event_id,
                              event.campuspulse_marker.create_operation_id):
                            raise BindingConflict("snapshot_ownership_registry_mismatch")
                self._trusted(context, writer="mcp")
                tx.create(path, document)
            return reference

        return await self.documents.transact(publish)

    async def put_assignments(self, context: TrustedRequestContext | TrustedContext, snapshot: AssignmentsSnapshot) -> SourceReference:
        return await self._put(context, snapshot, AssignmentsSnapshot)

    async def put_calendar(self, context: TrustedRequestContext | TrustedContext, snapshot: CalendarSnapshot) -> SourceReference:
        return await self._put(context, snapshot, CalendarSnapshot)

    async def get_assignments(self, context: TrustedRequestContext | TrustedContext, reference: SourceReference) -> AssignmentsSnapshot:
        context = self._request(context)
        self._trusted(context)
        reference = SourceReference.model_validate(reference)
        if reference.kind != SourceKind.ASSIGNMENTS:
            raise BindingConflict("snapshot_kind_mismatch")
        return await self._load_snapshot(context, self.documents, reference, current_run=True)

    async def get_calendar(self, context: TrustedRequestContext | TrustedContext, reference: SourceReference) -> CalendarSnapshot:
        context = self._request(context)
        self._trusted(context)
        reference = SourceReference.model_validate(reference)
        if reference.kind != SourceKind.CALENDAR:
            raise BindingConflict("snapshot_kind_mismatch")
        return await self._load_snapshot(context, self.documents, reference, current_run=True)

    async def get_for_plan(self, context: TrustedRequestContext | TrustedContext, plan: PlanReference) -> tuple[AssignmentsSnapshot, CalendarSnapshot]:
        """Historical reads only through an exact owned persisted plan lineage.

        Do not rewrite source run IDs to the new execution run. This is original
        evidence for approval/stale comparison, never fresh preflight evidence.
        """
        context = self._request(context)
        self._trusted(context)
        reference = PlanReference.model_validate(plan)
        saved = await self._load_plan(context, self.documents, reference.plan_id, reference.version)
        if plan_reference(saved) != reference:
            raise BindingConflict("plan_reference_mismatch")
        sources = {}
        for source in saved.payload.source_references:
            sources[source.kind] = await self._load_snapshot(context, self.documents, source, current_run=False)
        return sources[SourceKind.ASSIGNMENTS], sources[SourceKind.CALENDAR]
