"""Immutable plan persistence and small Firestore document adapter.

Only trusted backend proposal code publishes plans, after authoritative solver
revalidation (T15). This repository checks durable lineage and structural/hash
integrity; publishing is neither UI approval nor permission to execute.

The internal ports' TrustedContext DTO is not authentication. Concrete storage
adapters require T04's active TrustedRequestContext capability. To use the T03
DTO port signatures, startup may inject a request_context resolver; only the
identical DTO from that active scope is accepted, never a parsed/forwarded copy.
No public tools are added by this module.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, TypeAdapter, ValidationError

from ..domain.models import (
    AssignmentsSnapshot, CalendarSnapshot, Identifier, PlanReference, PlanVersion,
    PositiveInt, SourceKind, SourceReference, canonical_hash, canonical_json,
)
from ..domain.ports import BindingConflict, PortError, TrustedContext
from ..security.context import AccessDenied, TrustedRequestContext
from ..security.resources import ResourcePolicy

# Store canonical UTF-8 as a Firestore bytes field: arrays/order and microseconds
# round-trip exactly, with no nested indexing/truncation of operations. Leave
# substantial room below the 1 MiB document limit. No automatic TTL in MVP.
MAX_BODY_BYTES = 800_000
# Storage policy only; T03's wire/domain PositiveInt schema is unchanged.
# Keeps raw v{version} document IDs bounded to 20 ASCII bytes.
MAX_STORAGE_PLAN_VERSION = 2**63 - 1
T = TypeVar("T")
M = TypeVar("M", bound=BaseModel)


class DocumentTransaction(Protocol):
    async def get(self, path: str) -> dict | None: ...
    def create(self, path: str, value: dict) -> None: ...
    def set(self, path: str, value: dict) -> None: ...


class DocumentStore(Protocol):
    """Server-injected durable store, never selected by browser/model input."""

    project_id: str
    database_id: str

    async def get(self, path: str) -> dict | None: ...
    async def scan(self, collection: str) -> tuple[dict, ...]: ...
    async def transact(self, callback: Callable[[DocumentTransaction], Awaitable[T]]) -> T: ...


class _FirestoreTransaction:
    def __init__(self, client: Any, transaction: Any):
        self.client, self.transaction = client, transaction

    async def get(self, path: str) -> dict | None:
        snapshot = await self.client.document(path).get(transaction=self.transaction)
        return snapshot.to_dict() if snapshot.exists else None

    def create(self, path: str, value: dict) -> None:
        self.transaction.create(self.client.document(path), value)

    def set(self, path: str, value: dict) -> None:
        self.transaction.set(self.client.document(path), value)


class FirestoreDocuments:
    """Official google-cloud-firestore AsyncClient, with explicit target binding.

    Build the client in trusted startup code using explicit project/database and
    platform credentials (or emulator AnonymousCredentials). No default client,
    environment-selected project or in-memory production fallback is created.
    """

    def __init__(self, client: Any, *, project_id: str, database_id: str):
        if not project_id or not database_id or (
            client.project != project_id or client._database != database_id
        ):
            raise BindingConflict("firestore_target_mismatch")
        self.client = client
        self.project_id, self.database_id = project_id, database_id

    async def get(self, path: str) -> dict | None:
        try:
            snapshot = await self.client.document(path).get()
            return snapshot.to_dict() if snapshot.exists else None
        except Exception:
            raise PortError("storage_read_failed") from None

    async def scan(self, collection: str) -> tuple[dict, ...]:
        try:
            return tuple([s.to_dict() async for s in self.client.collection(collection).stream()])
        except Exception:
            raise PortError("storage_read_failed") from None

    async def transact(self, callback: Callable[[DocumentTransaction], Awaitable[T]]) -> T:
        from google.cloud.firestore_v1.async_transaction import async_transactional

        @async_transactional
        async def invoke(transaction):
            return await callback(_FirestoreTransaction(self.client, transaction))

        try:
            return await invoke(self.client.transaction())
        except (BindingConflict, AccessDenied):
            raise
        except Exception:
            # A commit timeout is ambiguous. Caller retries the same identity /
            # reads durable state; never substitute a new version or intent.
            raise PortError("storage_transaction_outcome_unknown") from None


def _key(*parts: Any) -> str:
    # IDs can contain slashes/Unicode; no supplied ID becomes a document path.
    return canonical_hash(list(parts))


def _encode(kind: str, value: BaseModel) -> dict:
    body = canonical_json(value).encode("utf-8")
    if len(body) > MAX_BODY_BYTES:
        raise BindingConflict("storage_payload_too_large")
    return {"kind": kind, "digest": canonical_hash(value), "body": body}


def _decode(document: dict | None, kind: str, model: type[M]) -> M:
    if document is None:
        raise BindingConflict("storage_record_not_found")
    try:
        if set(document) != {"kind", "digest", "body"} or document["kind"] != kind:
            raise ValueError()
        body = document["body"]
        if not isinstance(body, bytes) or len(body) > MAX_BODY_BYTES:
            raise ValueError()
        result = model.model_validate_json(body)
        if canonical_hash(result) != document["digest"] or canonical_json(result).encode("utf-8") != body:
            raise ValueError()
        return result
    except (ValueError, TypeError, KeyError, ValidationError):
        raise BindingConflict("storage_record_integrity_failed") from None


def _storage_version(version: int) -> int:
    TypeAdapter(PositiveInt).validate_python(version)
    if version > MAX_STORAGE_PLAN_VERSION:
        raise BindingConflict("storage_plan_version_out_of_range")
    return version


def _plan_path(owner: str, calendar: str, plan_id: str, version: int) -> str:
    TypeAdapter(Identifier).validate_python(plan_id)
    _storage_version(version)
    return f"cp_plans/{_key(owner, calendar, plan_id)}/versions/v{version}"


def _snapshot_path(reference: SourceReference) -> str:
    return f"cp_snapshots/{_key(reference.owner_id, reference.kind, reference.snapshot_id)}"


def plan_reference(plan: PlanVersion) -> PlanReference:
    return PlanReference(plan_id=plan.payload.plan_id, version=plan.payload.version, payload_hash=plan.payload_hash)


class _Repository:
    def __init__(self, documents: DocumentStore, policy: ResourcePolicy, *,
                 request_context: Callable[[], TrustedRequestContext] | None = None):
        if (documents.project_id, documents.database_id) != (policy.config.project_id, policy.config.firestore_database):
            raise BindingConflict("firestore_target_mismatch")
        self.documents, self.policy = documents, policy
        self._request_context = request_context

    def _request(self, context: TrustedRequestContext | TrustedContext) -> TrustedRequestContext:
        if isinstance(context, TrustedRequestContext):
            self.policy.require_context(context)
            return context
        if not isinstance(context, TrustedContext) or self._request_context is None:
            raise AccessDenied("server_request_context_required")
        request = self._request_context()
        self.policy.require_context(request)
        if context is not request.as_port_context():
            raise AccessDenied("foreign_port_context")
        return request

    def _trusted(self, context: TrustedRequestContext, *, writer: str | None = None):
        trusted = self.policy.require_context(context)
        if context.boundary not in ("backend", "mcp") or (writer and context.boundary != writer):
            raise AccessDenied("storage_caller_not_allowed")
        return trusted

    async def _load_snapshot(self, context, reader, reference, *, current_run: bool):
        self.policy.require_source(context, reference, current_run=current_run)
        model = AssignmentsSnapshot if reference.kind == SourceKind.ASSIGNMENTS else CalendarSnapshot
        snapshot = _decode(await reader.get(_snapshot_path(reference)), "snapshot_" + reference.kind, model)
        if snapshot.source.reference != reference:
            raise BindingConflict("snapshot_reference_mismatch")
        self.policy.require_source(context, snapshot.source.reference, current_run=current_run)
        return snapshot

    async def _load_plan(self, context, reader, plan_id: str, version: int) -> PlanVersion:
        context = self._request(context)
        trusted = self._trusted(context)
        plan = _decode(await reader.get(_plan_path(trusted.owner_id, trusted.calendar_id, plan_id, version)), "plan", PlanVersion)
        self.policy.require_plan(context, plan)
        if (plan.payload.plan_id, plan.payload.version) != (plan_id, version):
            raise BindingConflict("plan_identity_mismatch")
        return plan


class FirestorePlanStore(_Repository):
    """Create-only payload API. Approval/execution belong in separate stores."""

    async def create(self, context: TrustedRequestContext | TrustedContext, plan: PlanVersion) -> PlanReference:
        context = self._request(context)
        trusted = self._trusted(context, writer="backend")
        plan = PlanVersion.model_validate(plan)
        self.policy.require_plan(context, plan)
        path = _plan_path(trusted.owner_id, trusted.calendar_id, plan.payload.plan_id, plan.payload.version)
        document = _encode("plan", plan)

        async def publish(tx):
            self._trusted(context, writer="backend")
            existing = await tx.get(path)
            if existing is not None:
                _decode(existing, "plan", PlanVersion)
                if existing != document:
                    raise BindingConflict("immutable_version_conflict")
                return plan_reference(plan)
            for reference in plan.payload.source_references:
                snapshot = await self._load_snapshot(context, tx, reference, current_run=True)
                source = snapshot.source
                if not source.complete or reference.read_at > plan.payload.created_at:
                    raise BindingConflict("plan_source_not_complete_or_future")
                horizon = plan.payload.constraints.horizon
                if reference.kind == SourceKind.CALENDAR:
                    coverage = source.covered_interval
                    if coverage.start > horizon.start or coverage.end < horizon.end:
                        raise BindingConflict("plan_source_coverage_mismatch")
                elif source.timezone != trusted.timezone:
                    raise BindingConflict("plan_source_timezone_mismatch")
            # All reads precede writes. Transaction callbacks perform no Calendar I/O.
            self._trusted(context, writer="backend")
            tx.create(path, document)
            return plan_reference(plan)

        return await self.documents.transact(publish)

    async def get(self, context: TrustedRequestContext | TrustedContext, plan_id: str, version: int) -> PlanVersion:
        return await self._load_plan(context, self.documents, plan_id, version)

    def _current_path(self, trusted) -> str:
        return f"cp_current/{_key(trusted.owner_id, trusted.calendar_id)}"

    async def get_current(self, context: TrustedRequestContext | TrustedContext) -> PlanReference | None:
        context = self._request(context)
        trusted = self._trusted(context)
        document = await self.documents.get(self._current_path(trusted))
        if document is None:
            return None
        reference = _decode(document, "current", PlanReference)
        plan = await self.get(context, reference.plan_id, reference.version)
        if plan_reference(plan) != reference:
            raise BindingConflict("current_plan_hash_mismatch")
        return reference

    async def set_current(self, context: TrustedRequestContext | TrustedContext, reference: PlanReference, *, expected: PlanReference | None) -> PlanReference:
        """Backend CAS of proposal reference; does not approve or execute it.

        Explicit expected=None means no current plan. Creating a historical
        version never implicitly moves this pointer or overwrites a newer one.
        """
        context = self._request(context)
        trusted = self._trusted(context, writer="backend")
        reference = PlanReference.model_validate(reference)
        expected = None if expected is None else PlanReference.model_validate(expected)
        _storage_version(reference.version)
        if expected is not None:
            _storage_version(expected.version)
        path = self._current_path(trusted)

        async def change(tx):
            plan = await self._load_plan(context, tx, reference.plan_id, reference.version)
            if plan_reference(plan) != reference:
                raise BindingConflict("current_plan_hash_mismatch")
            document = await tx.get(path)
            current = None if document is None else _decode(document, "current", PlanReference)
            if current == reference:
                return reference
            if current != expected:
                raise BindingConflict("current_plan_cas_conflict")
            self._trusted(context, writer="backend")
            tx.set(path, _encode("current", reference))
            return reference

        return await self.documents.transact(change)
