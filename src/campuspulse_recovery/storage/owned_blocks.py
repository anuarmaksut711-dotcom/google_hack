"""Durable create intent is authority; Calendar metadata alone is never adopted."""

from __future__ import annotations

from pydantic import TypeAdapter

from ..domain.models import CalendarEvent, OwnedBlockRecord, OwnershipMarker, ResourceId
from ..domain.ports import BindingConflict, TrustedContext
from ..domain.time_rules import instant
from ..security.context import TrustedRequestContext
from .plans import _Repository, _decode, _encode, _key, _storage_version, plan_reference


def _stable_intent(record: OwnedBlockRecord) -> tuple:
    """Identity of the create, independent of retry/confirmation timestamps."""
    return (record.owner_id, record.calendar_id, record.block_id,
            record.assignment_id, record.event_id, record.intent_plan,
            record.intent_operation_id)


class FirestoreOwnedBlockRegistry(_Repository):
    def _root(self, trusted):

        return f"cp_calendars/{_key(trusted.calendar_id)}"

    def _paths(self, trusted, record):
        root = self._root(trusted)
        return f"{root}/blocks/{_key(record.block_id)}", f"{root}/events/{_key(record.event_id)}"

    def _bound(self, context, record):
        self.policy.require_owner(context, record.owner_id)
        self.policy.require_calendar(context, record.calendar_id)

    async def _intent(self, context, reader, record):
        plan = await self._load_plan(context, reader, record.intent_plan.plan_id, record.intent_plan.version)
        if plan_reference(plan) != record.intent_plan or record.reserved_at < plan.payload.created_at:
            raise BindingConflict("ownership_intent_plan_mismatch")
        operation = next((op for op in plan.payload.operations if op.operation_id == record.intent_operation_id), None)
        if operation is None or operation.kind != "create" or (
            operation.event_id, operation.target.block_id, operation.target.assignment_id
        ) != (record.event_id, record.block_id, record.assignment_id):
            raise BindingConflict("ownership_create_intent_mismatch")
        return operation

    async def reserve(self, context: TrustedRequestContext | TrustedContext, record: OwnedBlockRecord) -> OwnedBlockRecord:
        context = self._request(context)
        trusted = self._trusted(context, writer="mcp")
        record = OwnedBlockRecord.model_validate(record)
        self._bound(context, record)
        if record.state != "reserved":
            raise BindingConflict("ownership_reservation_required")
        _storage_version(record.intent_plan.version)
        block_path, event_path = self._paths(trusted, record)

        async def reserve(tx):
            block, event = await tx.get(block_path), await tx.get(event_path)
            if block is not None or event is not None:
                existing = _decode(block, "owned_block", OwnedBlockRecord)
                indexed = _decode(event, "owned_block", OwnedBlockRecord)
                self._bound(context, existing)
                if existing != indexed or _stable_intent(existing) != _stable_intent(record):
                    raise BindingConflict("ownership_collision")


                await self._intent(context, tx, existing)
                return existing
            await self._intent(context, tx, record)
            self._trusted(context, writer="mcp")
            value = _encode("owned_block", record)
            tx.create(block_path, value)
            tx.create(event_path, value)
            return record

        return await self.documents.transact(reserve)

    async def _get(self, context, path):
        document = await self.documents.get(path)
        if document is None:
            return None
        record = _decode(document, "owned_block", OwnedBlockRecord)
        self._bound(context, record)
        return record

    async def get_by_block(self, context: TrustedRequestContext | TrustedContext, block_id: str) -> OwnedBlockRecord | None:
        context = self._request(context)
        trusted = self._trusted(context)
        TypeAdapter(ResourceId).validate_python(block_id)
        record = await self._get(context, f"{self._root(trusted)}/blocks/{_key(block_id)}")
        if record is not None and record.block_id != block_id:
            raise BindingConflict("ownership_index_mismatch")
        return record

    async def get_by_event(self, context: TrustedRequestContext | TrustedContext, event_id: str) -> OwnedBlockRecord | None:
        context = self._request(context)
        trusted = self._trusted(context)
        TypeAdapter(ResourceId).validate_python(event_id)
        record = await self._get(context, f"{self._root(trusted)}/events/{_key(event_id)}")
        if record is not None and record.event_id != event_id:
            raise BindingConflict("ownership_index_mismatch")
        return record

    async def list_for_calendar(self, context: TrustedRequestContext | TrustedContext) -> tuple[OwnedBlockRecord, ...]:
        context = self._request(context)
        trusted = self._trusted(context)
        documents = await self.documents.scan(f"{self._root(trusted)}/blocks")
        records = tuple(_decode(doc, "owned_block", OwnedBlockRecord) for doc in documents)
        for record in records:
            self._bound(context, record)
        return tuple(sorted(records, key=lambda record: record.block_id))

    async def confirm(self, context: TrustedRequestContext | TrustedContext, reserved: OwnedBlockRecord, *, observed: CalendarEvent, at) -> OwnedBlockRecord:
        """Trusted Calendar adapter supplies an actual read/write response.

        Caller must perform actual fresh I/O outside this transaction. DTOs alone
        cannot prove a read happened. Confirmation records creation evidence;
        later updates/etags live in snapshots and execution ledger, never here.
        """
        context = self._request(context)
        trusted = self._trusted(context, writer="mcp")
        reserved, observed, at = OwnedBlockRecord.model_validate(reserved), CalendarEvent.model_validate(observed), instant(at)
        self._bound(context, reserved)
        _storage_version(reserved.intent_plan.version)
        block_path, event_path = self._paths(trusted, reserved)

        async def confirm(tx):
            operation = await self._intent(context, tx, reserved)
            stored = _decode(await tx.get(block_path), "owned_block", OwnedBlockRecord)
            indexed = _decode(await tx.get(event_path), "owned_block", OwnedBlockRecord)
            self._bound(context, stored)
            if stored != indexed:
                raise BindingConflict("ownership_index_mismatch")
            original = stored.model_dump() | {"state": "reserved", "confirmed_at": None, "confirmed_etag": None}
            if reserved not in (stored, OwnedBlockRecord.model_validate(original)):
                raise BindingConflict("ownership_reservation_changed")
            marker = OwnershipMarker(owner_id=stored.owner_id, calendar_id=stored.calendar_id,
                block_id=stored.block_id, assignment_id=stored.assignment_id, create_operation_id=stored.intent_operation_id)
            if (observed.event_id != stored.event_id or observed.interval != operation.target.interval
                or observed.campuspulse_marker != marker or observed.etag is None
                or observed.status != "confirmed" or observed.transparency != "opaque"
                or observed.recurrence is not None or observed.all_day_start is not None or at < stored.reserved_at):
                raise BindingConflict("ownership_observation_mismatch")
            if stored.state == "confirmed":
                if observed.etag != stored.confirmed_etag:
                    raise BindingConflict("ownership_confirmation_changed")
                return stored
            result = OwnedBlockRecord.model_validate(stored.model_dump() | {
                "state": "confirmed", "confirmed_at": at, "confirmed_etag": observed.etag})
            self._trusted(context, writer="mcp")
            value = _encode("owned_block", result)
            tx.set(block_path, value)
            tx.set(event_path, value)
            return result

        return await self.documents.transact(confirm)
