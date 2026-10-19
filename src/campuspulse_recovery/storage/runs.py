"""Atomic bounded run journal over the existing explicit-target DocumentStore.

No default clients, process locks, external calls, execution/approval mutation or
automatic business retries. One bounded document per run makes allocation and
dedup one transaction, including across repository instances. Expiry is logical;
physical deletion/TTL requires separate authorized deployment and is deferred.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Callable

from pydantic import Field, StrictInt

from ..domain.models import Contract, Instant, RunEvent, canonical_hash, canonical_json
from ..domain.ports import BindingConflict, PortError, TrustedContext
from ..domain.time_rules import instant
from ..observability.events import Observation, RecordedEvent, TechnicalId
from ..security.context import AccessDenied, TrustedRequestContext
from .plans import _Repository, _decode, _encode, _key


class JournalLimits(Contract):
    max_events: Annotated[StrictInt, Field(ge=1, le=512)] = 256
    max_event_bytes: Annotated[StrictInt, Field(ge=512, le=8192)] = 4096
    max_run_bytes: Annotated[StrictInt, Field(ge=1024, le=700_000)] = 600_000
    page_size: Annotated[StrictInt, Field(ge=1, le=100)] = 50
    retention_seconds: Annotated[StrictInt, Field(ge=60, le=604800)] = 604800


class _Run(Contract):
    run_id: TechnicalId
    binding: str  # hash of owner/calendar/config/session; never raw email
    created_at: Instant
    expires_at: Instant
    limits: JournalLimits
    records: tuple[RecordedEvent, ...] = ()
    related: tuple[TechnicalId, ...] = ()


class FirestoreRunEventStore(_Repository):
    """T03 append/list_after are preserved; append_record returns allocated row.

    Backend registers an authenticated run before invoking the agent. Agent and
    MCP independently reconstruct active server capabilities for that same run.
    They cannot register arbitrary model-supplied lineage. No raw snapshot store.
    """
    def __init__(self, documents, policy, *, clock: Callable, limits=None, request_context=None):
        super().__init__(documents, policy, request_context=request_context)
        self.clock, self.limits = clock, JournalLimits.model_validate(limits or JournalLimits())

    def _scope(self, context):
        request = self._request(context)
        t = self.policy.require_context(request)
        if request.boundary not in ("backend", "agent", "mcp"):
            raise AccessDenied("journal_boundary_denied")
        # Technical run IDs must originate from server binding, never source IDs.
        from pydantic import TypeAdapter
        TypeAdapter(TechnicalId).validate_python(t.run_id)
        return request, t

    @staticmethod
    def _binding(context):
        t = context.trusted
        return _key(t.owner_id, t.calendar_id, t.config_revision, context.session_id)

    def _path(self, context, run_id=None):
        t = context.trusted
        return "cp_run_journals/" + _key(t.owner_id, t.calendar_id, t.config_revision, run_id or t.run_id)

    def _load(self, context, doc, *, related=False):
        # Often called immediately after an awaited read. The capability may
        # have expired or its shared request lease may have been revoked there.
        self._scope(context)
        value = _decode(doc, "run_journal", _Run)
        if value.binding != self._binding(context) or (not related and value.run_id != context.trusted.run_id):
            raise AccessDenied("journal_binding_mismatch")
        if instant(self.clock()) >= value.expires_at:
            raise BindingConflict("journal_expired")
        if len(value.records) > value.limits.max_events:
            raise BindingConflict("journal_integrity_failed")
        if len(canonical_json(value).encode()) > value.limits.max_run_bytes:
            raise BindingConflict("journal_integrity_failed")
        for seq, record in enumerate(value.records, 1):
            e = record.observation.event
            raw = record.observation.model_dump(mode="json")
            raw["event"]["sequence"] = 0
            if (e.sequence != seq or e.run_id != value.run_id
                    or canonical_hash(raw) != record.fingerprint):
                raise BindingConflict("journal_integrity_failed")
        self._scope(context)
        return value

    async def register(self, context) -> None:
        context, t = self._scope(context)
        if context.boundary != "backend":
            raise AccessDenied("journal_backend_registration_required")
        now = instant(self.clock())
        value = _Run(run_id=t.run_id, binding=self._binding(context), created_at=now,
            expires_at=now + timedelta(seconds=self.limits.retention_seconds), limits=self.limits)
        path = self._path(context)
        async def create(tx):
            self._scope(context)
            existing = await tx.get(path)
            self._scope(context)
            if existing is not None:
                self._load(context, existing)
                self._scope(context)
                return
            document = _encode("run_journal", value)
            self._scope(context)
            tx.create(path, document)
            self._scope(context)
        try:
            await self.documents.transact(create)
            self._scope(context)
        except (BindingConflict, AccessDenied):
            raise
        except PortError:
            # Same identity only. A second register never extends retention.
            self._scope(context)
            self._load(context, await self.documents.get(path))
            self._scope(context)

    async def append(self, context: TrustedContext, event: RunEvent) -> None:
        request, _ = self._scope(context)
        layer = {"backend": "backend", "agent": "adk_callback", "mcp": "domain_adapter"}[request.boundary]
        await self.append_record(request, Observation(event=event, layer=layer))

    async def append_record(self, context, value: Observation) -> RecordedEvent:
        context, t = self._scope(context)
        value = Observation.model_validate(value)
        e = value.event
        if e.run_id != t.run_id:
            raise AccessDenied("journal_run_mismatch")
        allowed = {"backend": {"backend", "adk_callback", "client_observation"},
                   "agent": {"adk_callback", "client_observation"}, "mcp": {"domain_adapter"}}
        if value.layer not in allowed[context.boundary]:
            raise AccessDenied("journal_layer_mismatch")
        raw = value.model_dump(mode="json")
        raw["event"]["sequence"] = 0
        value = Observation.model_validate(raw)
        fingerprint, path = canonical_hash(value), self._path(context)
        now = instant(self.clock())

        def duplicate(run):
            match = next((r for r in run.records if r.observation.event.event_id == e.event_id), None)
            if match is not None and match.fingerprint != fingerprint:
                raise BindingConflict("journal_event_identity_conflict")
            return match

        async def publish(tx):
            self._scope(context)
            run = self._load(context, await tx.get(path))
            previous = duplicate(run)
            if previous is not None:
                self._scope(context)
                return previous
            if len(run.records) >= run.limits.max_events:
                raise BindingConflict("journal_event_limit")
            if len(canonical_json(value).encode()) > run.limits.max_event_bytes:
                raise BindingConflict("journal_event_bytes_limit")
            data = value.model_dump(mode="json")
            data["event"]["sequence"] = len(run.records) + 1
            record = RecordedEvent(observation=Observation.model_validate(data), recorded_at=now, fingerprint=fingerprint)
            updated = _Run.model_validate({**run.model_dump(), "records": (*run.records, record)})
            if len(canonical_json(updated).encode()) > run.limits.max_run_bytes:
                raise BindingConflict("journal_run_bytes_limit")
            document = _encode("run_journal", updated)
            self._scope(context)
            tx.set(path, document)
            self._scope(context)
            return record
        try:
            record = await self.documents.transact(publish)
            self._scope(context)
            return record
        except (BindingConflict, AccessDenied):
            raise
        except PortError:
            # Ambiguous commit: reconcile only this observation, never re-run a
            # business tool or allocate another event identity.
            self._scope(context)
            match = duplicate(self._load(context, await self.documents.get(path)))
            if match is None:
                raise PortError("journal_append_outcome_unknown") from None
            self._scope(context)
            return match

    async def records_after(self, context, *, sequence: int = 0) -> tuple[RecordedEvent, ...]:
        if type(sequence) is not int or sequence < 0:
            raise BindingConflict("journal_cursor_invalid")
        context, _ = self._scope(context)
        run = self._load(context, await self.documents.get(self._path(context)))
        self._scope(context)
        return run.records[sequence:sequence + run.limits.page_size]

    async def list_after(self, context: TrustedContext, *, sequence: int) -> tuple[RunEvent, ...]:
        return tuple(r.observation.event for r in await self.records_after(context, sequence=sequence))

    async def get_record(self, context, *, event_id: str) -> RecordedEvent | None:
        context, _ = self._scope(context)
        run = self._load(context, await self.documents.get(self._path(context)))
        self._scope(context)
        return next((r for r in run.records if r.observation.event.event_id == event_id), None)

    async def correlate(self, context, related_context) -> None:
        """Both real run capabilities must be active in the same server session.

        Correlation is server-created, never model-provided and never overwrites
        the genuine run IDs of proposal/apply/verification. Session changes need
        a separate backend authorization design; this narrow seam denies them.
        """
        context, _ = self._scope(context)
        other, _ = self._scope(related_context)
        if context.boundary != "backend" or self._binding(context) != self._binding(other):
            raise AccessDenied("journal_correlation_denied")
        path, other_path = self._path(context), self._path(other)
        def require_both():
            self._scope(context)
            self._scope(other)
        async def link(tx):
            require_both()
            document = await tx.get(path)
            require_both()
            run = self._load(context, document)
            document = await tx.get(other_path)
            require_both()
            related = self._load(other, document)
            if related.run_id in run.related:
                require_both()
                return
            if len(run.related) >= 8:
                raise BindingConflict("journal_correlation_limit")
            updated = _Run.model_validate({**run.model_dump(), "related": (*run.related, related.run_id)})
            if len(canonical_json(updated).encode()) > run.limits.max_run_bytes:
                raise BindingConflict("journal_run_bytes_limit")
            document = _encode("run_journal", updated)
            require_both()
            tx.set(path, document)
            require_both()
        await self.documents.transact(link)
        require_both()

    async def related_after(self, context, *, run_id: str, sequence: int = 0):
        context, _ = self._scope(context)
        run = self._load(context, await self.documents.get(self._path(context)))
        if run_id not in run.related:
            raise AccessDenied("journal_correlation_required")
        if type(sequence) is not int or sequence < 0:
            raise BindingConflict("journal_cursor_invalid")
        other = self._load(context, await self.documents.get(self._path(context, run_id)), related=True)
        self._scope(context)
        return other.records[sequence:sequence + other.limits.page_size]
