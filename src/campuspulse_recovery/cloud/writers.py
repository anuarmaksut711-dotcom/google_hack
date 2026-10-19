"""Durable stop receipts after real writer tasks finish; elapsed time is never proof."""
import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field

from ..application.apply import ApplyService
from ..domain.models import Contract, Instant
from ..domain.ports import BindingConflict
from ..integrations.google_clients import GoogleWorkspaceClients
from ..integrations.google_credentials import WorkspaceAccessError
from ..storage.leases import FirestoreCalendarCoordination, WriterStoppedEvidence
from ..storage.plans import _decode, _encode, _key
from .authority import utcnow


@dataclass
class WriterLifetime:
    leases: list = field(default_factory=list)
    calls: list = field(default_factory=list)
    closed: bool = False


WRITER = ContextVar('cloud_writer_lifetime', default=None)


class StopReceipt(Contract):
    owner_id: str
    calendar_id: str
    config_revision: str
    stopped: WriterStoppedEvidence


def receipt_path(lease):
    return 'cp_writer_stops/' + _key(lease.owner_id, lease.calendar_id, lease.holder_id, lease.generation)


class DurableRecoveryGuard:
    def __init__(self, documents, policy):
        self.documents, self.policy = documents, policy

    async def require_stopped(self, context, transaction, previous, now):
        trusted = self.policy.require_context(context)
        value = _decode(await transaction.get(receipt_path(previous)), 'writer_stop', StopReceipt)
        if ((value.owner_id, value.calendar_id, value.config_revision) !=
                (trusted.owner_id, trusted.calendar_id, trusted.config_revision)
                or (value.stopped.holder_id, value.stopped.generation) != (previous.holder_id, previous.generation)
                or not previous.acquired_at <= value.stopped.stopped_at <= now):
            raise BindingConflict('durable_writer_stop_mismatch')
        return value.stopped

    async def stopped(self, lease):
        receipt = StopReceipt(owner_id=lease.owner_id, calendar_id=lease.calendar_id,
            config_revision=self.policy.config.config_revision,
            stopped=WriterStoppedEvidence(holder_id=lease.holder_id, generation=lease.generation,
                stopped_at=utcnow(), evidence_reference=receipt_path(lease)))
        async def save(tx):
            prior = await tx.get(receipt_path(lease))
            if prior is None:
                tx.create(receipt_path(lease), _encode('writer_stop', receipt))
        await self.documents.transact(save)


class TrackedCoordination(FirestoreCalendarCoordination):
    async def acquire(self, context, **kwargs):
        lifetime = WRITER.get()
        if lifetime is None or lifetime.closed:
            raise BindingConflict('writer_lifetime_required')
        lease = await super().acquire(context, **kwargs)
        lifetime.leases.append(lease)
        return lease


class CloudWorkspaceClients(GoogleWorkspaceClients):
    def __init__(self, policy, grants, handle):
        super().__init__(policy, environment='cloud', read_only=False)
        self.grants, self.handle = grants, handle

    async def calendar_async_request(self, context, *, calendar_id, request_factory, mutation=False):
        lifetime = WRITER.get()
        if mutation and (lifetime is None or lifetime.closed):
            raise BindingConflict('writer_lifetime_required')
        # Fresh durable session/parent status immediately before dispatch.
        await self.grants.load(self.handle(), status='running')
        task = asyncio.create_task(asyncio.to_thread(self.calendar_request, context,
            calendar_id=calendar_id, request_factory=request_factory, mutation=mutation))
        if lifetime is not None:
            lifetime.calls.append((task, mutation))
        # Cancellation must not detach an untracked mutating worker thread.
        if mutation or lifetime is not None:
            return await asyncio.shield(task)
        return await task


class TrackedApplyService(ApplyService):
    async def apply(self, context, request):
        lifetime = WriterLifetime()
        token = WRITER.set(lifetime)
        try:
            from ..observability.adapters import _domain_call
            return await _domain_call(self, context, 'apply_plan',
                lambda: super(TrackedApplyService, self).apply(context, request))
        finally:
            lifetime.closed = True
            uncertain = False
            for task, mutation in lifetime.calls:
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                try:
                    task.result()
                except BaseException as error:
                    # A transport timeout can leave a remote write pending even
                    # after the local thread exits. It cannot create a stop proof.
                    if mutation and (not isinstance(error, WorkspaceAccessError) or error.outcome_unknown):
                        uncertain = True
            try:
                if not uncertain:
                    for lease in lifetime.leases:
                        await self.coordination.recovery_guard.stopped(lease)
            finally:
                WRITER.reset(token)
