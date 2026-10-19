"""Exact backend UI consent; no public agent tool and no Calendar dispatch.

Storage schema is private to this adapter. Re-consent of a terminal version is
deliberately forbidden: publish a new immutable version and explicitly approve
it. Request retries always retain the original grant and its lifetime.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Callable, Iterator, Literal

from pydantic import TypeAdapter

from ..domain.models import Approval, Contract, Identifier, Instant, PlanReference
from ..domain.ports import BindingConflict
from ..domain.time_rules import instant
from ..security.context import AccessDenied, TrustedRequestContext
from ..storage.plans import (
    FirestorePlanStore, _Repository, _decode, _encode, _key, plan_reference,
)


class ApprovalAction(PlanReference):
    request_key: Identifier


class _RequestRecord(Contract):
    plan: PlanReference
    recorded_at: Instant
    action: Literal["approve", "revoke"] = "approve"


@dataclass
class _UIAction:
    context: TrustedRequestContext
    reference: PlanReference
    request_key: str
    at: datetime
    active: bool = True


class FirestoreApprovalStore(_Repository):
    def __init__(self, documents, policy, *, clock: Callable[[], datetime] = lambda: datetime.now(UTC),
                 lifetime: timedelta = timedelta(minutes=10), request_context=None):
        super().__init__(documents, policy, request_context=request_context)
        if not timedelta(0) < lifetime <= timedelta(minutes=15):
            raise ValueError("invalid_approval_lifetime")
        self.clock, self.lifetime = clock, lifetime
        self._actions: ContextVar[_UIAction | None] = ContextVar("ui-approval", default=None)

    def _now(self):
        return instant(self.clock())

    def _path(self, trusted, reference):
        return f"cp_approvals/{_key(trusted.owner_id, trusted.calendar_id, reference.plan_id, reference.version)}"

    def _pointer(self, trusted):
        return f"cp_approval_current/{_key(trusted.owner_id, trusted.calendar_id)}"

    @contextmanager
    def ui_action(self, context, reference: PlanReference, request_key: str) -> Iterator[_UIAction]:
        """Trusted HTTP adapter only, after session + CSRF + origin checks.

        A DTO, model boolean or backend request scope alone cannot grant this
        capability. Never expose this adapter/method through ADK or MCP.
        """
        context = self._request(context)
        self._trusted(context, writer="backend")
        if self._actions.get() is not None:
            raise AccessDenied("nested_ui_action")
        action = _UIAction(context, PlanReference.model_validate(reference),
                           TypeAdapter(Identifier).validate_python(request_key), self._now())
        reset = self._actions.set(action)
        try:
            yield action
        finally:
            action.active = False
            self._actions.reset(reset)

    def _action(self, context, reference):
        self._trusted(context, writer="backend")
        action = self._actions.get()
        if action is None or not action.active or action.context is not context or action.reference != reference:
            raise AccessDenied("explicit_ui_action_required")
        return action

    async def _exact(self, context, tx, reference, *, current=True):
        trusted = self._trusted(context)
        plan = await self._load_plan(context, tx, reference.plan_id, reference.version)
        if plan_reference(plan) != reference:
            raise BindingConflict("plan_hash_mismatch")
        pointer = await tx.get(FirestorePlanStore._current_path(self, trusted))
        current_ref = None if pointer is None else _decode(pointer, "current", PlanReference)
        if current and current_ref != reference:
            raise BindingConflict("plan_not_current")
        return trusted, current_ref

    def _approval(self, document, trusted, reference):
        value = _decode(document, "approval", Approval)
        if (value.owner_id, value.calendar_id, value.plan, value.approved_by) != (
                trusted.owner_id, trusted.calendar_id, reference, self.policy.config.student.allowed_signin_principal):
            raise BindingConflict("approval_binding_mismatch")
        return value

    def _terminal(self, value, status: Literal["revoked", "superseded", "expired"], at):
        # model_copy(update=...) skips the frozen contract's validators. Every
        # persisted transition must revalidate chronology as well as shape.
        return Approval.model_validate({**value.model_dump(), "status": status, "invalidated_at": at})

    def _effective(self, value, current, now):
        if value.status != "active":
            return value
        if now >= value.expires_at:
            return self._terminal(value, "expired", value.expires_at)
        if current != value.plan:
            return self._terminal(value, "superseded", now)
        return value

    async def approve(self, context, reference, request_key) -> Approval:
        """Called only by an authenticated explicit UI POST, not proposal flow."""
        context = self._request(context)
        trusted = self._trusted(context, writer="backend")
        reference = PlanReference.model_validate(reference)
        with self.ui_action(context, reference, request_key) as action:
            now = action.at
            await self.record_ui_approval(context, Approval(owner_id=trusted.owner_id,
                calendar_id=trusted.calendar_id, plan=reference, approved_by=trusted.principal_id,
                approved_at=now, expires_at=now + self.lifetime))
        result = await self.get(context, reference)
        if result is None:
            raise BindingConflict("approval_missing_after_record")
        return result

    async def record_ui_approval(self, context, approval: Approval) -> None:
        context = self._request(context)
        approval = Approval.model_validate(approval)
        action = self._action(context, approval.plan)
        trusted = self._trusted(context, writer="backend")
        now = self._now()
        if ((approval.owner_id, approval.calendar_id, approval.approved_by) != (
                trusted.owner_id, trusted.calendar_id, trusted.principal_id)
                or approval.approved_at != action.at or not approval.active_at(now)
                or approval.expires_at != action.at + self.lifetime):
            raise AccessDenied("server_approval_fields_required")
        path = self._path(trusted, approval.plan)
        key_path = f"cp_ui_requests/{_key(trusted.owner_id, trusted.calendar_id, action.request_key)}"

        async def record(tx):
            self._action(context, approval.plan)
            # Every check shares this transaction with proposal CAS and revoke.
            _, current = await self._exact(context, tx, approval.plan, current=False)
            key_doc = await tx.get(key_path)
            document = await tx.get(path)
            prior_pointer = await tx.get(self._pointer(trusted))
            previous = None if prior_pointer is None else _decode(prior_pointer, "approval_current", PlanReference)
            previous_doc = None
            if previous is not None and previous != approval.plan:
                previous_doc = await tx.get(self._path(trusted, previous))
            self._action(context, approval.plan)
            # Firestore may queue/retry this callback. UI action/grant time is
            # immutable; transitions of records read here use fresh time only
            # after all reads, including a later grant written while we waited.
            transition_at = self._now()
            if key_doc is not None:
                saved = _decode(key_doc, "ui_request", _RequestRecord)
                if saved.plan != approval.plan or saved.action != "approve":
                    raise BindingConflict("approval_request_key_collision")
                self._approval(document, trusted, approval.plan)
                return  # Original outcome, including terminal status; no renewal.
            if document is not None:
                self._approval(document, trusted, approval.plan)
                # A new key does not constitute a new lifetime for this version.
                tx.create(key_path, _encode("ui_request", _RequestRecord(plan=approval.plan, recorded_at=now)))
                return
            if current != approval.plan:
                raise BindingConflict("plan_not_current")
            if not approval.active_at(transition_at):
                raise BindingConflict("approval_expired_before_commit")
            self._action(context, approval.plan)
            if previous_doc is not None:
                old = self._approval(previous_doc, trusted, previous)
                old = self._effective(old, current, transition_at)
                tx.set(self._path(trusted, previous), _encode("approval", old))
            tx.create(path, _encode("approval", approval))
            tx.create(key_path, _encode("ui_request", _RequestRecord(plan=approval.plan, recorded_at=now)))
            tx.set(self._pointer(trusted), _encode("approval_current", approval.plan))

        await self.documents.transact(record)

    async def get(self, context, reference: PlanReference) -> Approval | None:
        context = self._request(context)
        reference = PlanReference.model_validate(reference)

        async def read(tx):
            trusted, current = await self._exact(context, tx, reference, current=False)
            document = await tx.get(self._path(trusted, reference))
            self._trusted(context)
            if document is None:
                return None
            original = self._approval(document, trusted, reference)
            effective = self._effective(original, current, self._now())
            if effective != original:
                tx.set(self._path(trusted, reference), _encode("approval", effective))
            return effective

        return await self.documents.transact(read)

    async def require_active(self, context, plan: PlanReference, now: datetime) -> Approval:
        context = self._request(context)
        return await self.documents.transact(lambda tx: self.transaction_guard().require_active(context, tx, plan, now))

    def transaction_guard(self):
        """Distinct T06 interface; inject with the SAME DocumentStore instance."""
        return TransactionalUIApprovalGuard(self)

    async def invalidate(self, context, plan: PlanReference, *, reason: Literal["revoked", "superseded"], at: datetime) -> None:
        context = self._request(context)
        trusted = self._trusted(context, writer="backend")
        plan = PlanReference.model_validate(plan)
        action = self._action(context, plan)
        at = instant(at)
        if reason not in ("revoked", "superseded") or at != action.at:
            raise AccessDenied("server_invalidation_required")

        async def revoke(tx):
            _, current = await self._exact(context, tx, plan, current=False)
            value = self._approval(await tx.get(self._path(trusted, plan)), trusted, plan)
            key_path = f"cp_ui_requests/{_key(trusted.owner_id, trusted.calendar_id, action.request_key)}"
            key_doc = await tx.get(key_path)
            self._action(context, plan)
            transition_at = self._now()
            if key_doc is not None:
                saved = _decode(key_doc, "ui_request", _RequestRecord)
                if saved.plan != plan or saved.action != "revoke":
                    raise BindingConflict("approval_request_key_collision")
                return
            value = self._effective(value, current, transition_at)
            if value.status == "active":
                value = self._terminal(value, reason, transition_at)
            tx.set(self._path(trusted, plan), _encode("approval", value))
            tx.create(key_path, _encode("ui_request", _RequestRecord(plan=plan, recorded_at=at, action="revoke")))

        await self.documents.transact(revoke)


class TransactionalUIApprovalGuard:
    """No nested transaction: all reads use the caller's DocumentTransaction."""
    def __init__(self, store: FirestoreApprovalStore):
        self.store = store
        self.documents = store.documents

    async def require_active(self, context, transaction, plan: PlanReference, now: datetime) -> Approval:
        context = self.store._request(context)
        plan = PlanReference.model_validate(plan)
        trusted, _ = await self.store._exact(context, transaction, plan)
        value = self.store._approval(await transaction.get(self.store._path(trusted, plan)), trusted, plan)
        self.store._trusted(context)
        if not value.active_at(instant(now)) or not value.active_at(self.store._now()):
            raise BindingConflict("active_exact_ui_approval_required")
        return value
