"""Voice-only durable fencing, using the existing FirestoreDocuments port.

No Calendar mutation lease semantics, SDK clients, packet persistence or local
authority cache. Transaction callbacks only read/write documents. Keep claim
tombstones: TTL deletion would reset the uint32 connection fence.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import Field, StrictBool, StrictInt, StringConstraints, model_validator

from ..domain.models import Contract, Digest, Identifier, Instant, canonical_hash
from ..domain.ports import BindingConflict, PortError
from ..security.context import AccessDenied, ServerRunBinding
from ..storage.plans import _decode, _encode, _key
from ..web.auth import WebSession, digest
from .device_auth import DeviceAuth, VoiceAccess, VoiceCredential, _secret
from .protocol import MAX_SESSION_MS, UINT32_MAX, WireId

LEASE_SECONDS = 15
HISTORY_ENTRIES = 16
HISTORY_BYTES = 8192
ShortText = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=512)]


class HistoryNote(Contract):
    """Untrusted final conversational text, never a tool instruction or result."""

    speaker: Literal["user", "assistant"]
    text: ShortText = Field(repr=False)


class ResumeState(Contract):
    history: tuple[HistoryNote, ...] = Field(default=(), max_length=HISTORY_ENTRIES, repr=False)
    seen_call_ids: tuple[WireId, ...] = Field(default=(), max_length=64, repr=False)
    transport_token: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=4096)] | None = Field(default=None, repr=False)
    transport_expires_at: Instant | None = None

    @model_validator(mode="after")
    def bounded(self):
        if sum(len(n.text.encode("utf-8")) for n in self.history) > HISTORY_BYTES:
            raise ValueError("voice_history_limit")
        if (self.transport_token is None) != (self.transport_expires_at is None):
            raise ValueError("voice_transport_expiry_required")
        if len(set(self.seen_call_ids)) != len(self.seen_call_ids):
            raise ValueError("voice_duplicate_call_id")
        return self

    def changed(self, **fields):
        return ResumeState.model_validate(self.model_dump() | fields)


class StreamLease(Contract):
    """Server-side handle; serializing it does not authenticate a remote caller."""

    device_id: Identifier
    holder_id: Identifier
    connection_generation: Annotated[StrictInt, Field(ge=1, le=UINT32_MAX)]
    session_id: Identifier
    run_id: Identifier
    binding: ServerRunBinding = Field(repr=False)
    credential_hash: Digest = Field(repr=False)
    resource_hash: Digest = Field(repr=False)
    deadline: Instant
    lease_until: Instant


class _Claim(Contract):
    lease: StreamLease
    released: StrictBool = False
    resume: ResumeState = Field(default_factory=ResumeState, repr=False)


def _path(device_id):
    return "cp_voice_streams/" + _key(device_id)


class FirestoreVoiceSessions:
    """Inject the same DeviceAuth/store/policy as the native handshake.

    acquire/renew/check/checkpoint re-read credential and browser parent within
    the claim transaction. Both expirations are checked after the final read.
    An ambiguous acquisition only reconciles its original candidate holder.
    """

    def __init__(self, devices: DeviceAuth, *, transaction_seconds: float = 2):
        if not 0 < transaction_seconds <= 2:
            raise ValueError("invalid_voice_storage_budget")
        self.devices = devices
        self.documents = devices.documents
        self.policy = devices.policy
        self.now = devices.auth.now
        self.transaction_seconds = transaction_seconds

    def _resources(self):
        cfg = self.policy.config
        return canonical_hash([cfg.project_id, cfg.firestore_database, cfg.config_revision,
            cfg.student.model_dump(mode="json")])

    async def _authority(self, tx, token_hash):
        value = _decode(await tx.get(self.devices._credential_path(token_hash)),
                        "voice_credential", VoiceCredential)
        parent = _decode(await tx.get("cp_web_sessions/" + value.parent_hash), "web_session", WebSession)
        return value, parent

    def _validate_authority(self, value, parent, now):
        self.devices._check_parent(parent, now)
        if (value.revoked or now >= value.expires_at or value.binding != parent.binding
                or value.scope != "voice:read"):
            raise AccessDenied("voice_access_denied")
        return VoiceAccess(value.device_id, value.binding, min(value.expires_at, parent.expires_at))

    async def _run(self, callback):
        try:
            async with asyncio.timeout(self.transaction_seconds):
                return await self.documents.transact(callback)
        except (AccessDenied, BindingConflict):
            raise
        except Exception:
            raise PortError("voice_storage_unavailable") from None

    async def acquire(self, token: str) -> tuple[StreamLease, ResumeState]:
        token_hash = digest(_secret(token))
        holder, session, run = (prefix + uuid4().hex for prefix in ("holder_", "voice_", "vrun_"))

        async def claim(tx):
            value, parent = await self._authority(tx, token_hash)
            raw = await tx.get(_path(value.device_id))
            old = None if raw is None else _decode(raw, "voice_stream", _Claim)
            now = self.now()
            access = self._validate_authority(value, parent, now)
            if old and not old.released and now < old.lease.lease_until:
                raise BindingConflict("voice_stream_busy")
            generation = 1 if old is None else old.lease.connection_generation + 1
            if generation > UINT32_MAX:
                raise BindingConflict("voice_generation_exhausted")
            resume = ResumeState()
            session_id = session
            if (old and old.lease.credential_hash == token_hash and old.lease.binding == access.binding
                    and old.lease.resource_hash == self._resources() and now < old.lease.deadline):
                session_id, resume = old.lease.session_id, self._resume(old.resume, now)
            deadline = min(access.expires_at, now + timedelta(milliseconds=MAX_SESSION_MS))
            lease = StreamLease(device_id=value.device_id, holder_id=holder,
                connection_generation=generation, session_id=session_id, run_id=run,
                binding=access.binding, credential_hash=token_hash, resource_hash=self._resources(),
                deadline=deadline, lease_until=min(deadline, now + timedelta(seconds=LEASE_SECONDS)))
            tx.set(_path(value.device_id), _encode("voice_stream", _Claim(lease=lease, resume=resume)))
            return lease, resume

        try:
            return await self._run(claim)
        except BindingConflict:
            raise
        except PortError:

            async def reconcile(tx):
                value, parent = await self._authority(tx, token_hash)
                raw = await tx.get(_path(value.device_id))
                current = None if raw is None else _decode(raw, "voice_stream", _Claim)
                now = self.now()
                self._validate_authority(value, parent, now)
                if (current is None or current.lease.holder_id != holder or current.released
                        or current.lease.credential_hash != token_hash
                        or current.lease.resource_hash != self._resources()
                        or now >= min(current.lease.lease_until, current.lease.deadline)):
                    raise PortError("voice_storage_unavailable")
                return current.lease, self._resume(current.resume, now)
            return await self._run(reconcile)

    def _resume(self, state, now):
        if state.transport_expires_at is not None and now >= state.transport_expires_at:
            return state.changed(transport_token=None, transport_expires_at=None)
        return state

    async def _current(self, tx, lease):
        lease = StreamLease.model_validate(lease)
        value, parent = await self._authority(tx, lease.credential_hash)
        current = _decode(await tx.get(_path(lease.device_id)), "voice_stream", _Claim)
        now = self.now()
        access = self._validate_authority(value, parent, now)
        if (current.released or current.lease != lease or access.device_id != lease.device_id
                or access.binding != lease.binding or lease.resource_hash != self._resources()
                or now >= min(lease.deadline, lease.lease_until, access.expires_at)):
            raise AccessDenied("voice_stream_stale")
        return current, access, now

    async def check(self, lease: StreamLease, *, tool: str | None = None) -> VoiceAccess:
        async def check(tx):
            _, access, _ = await self._current(tx, lease)
            if tool is not None:
                access.require_tool(tool)
            return access
        return await self._run(check)

    async def renew(self, lease: StreamLease) -> StreamLease:
        async def renew(tx):
            current, access, now = await self._current(tx, lease)
            updated = StreamLease.model_validate(lease.model_dump() | {
                "lease_until": min(lease.deadline, access.expires_at, now + timedelta(seconds=LEASE_SECONDS))})
            tx.set(_path(lease.device_id), _encode("voice_stream", _Claim(
                lease=updated, resume=current.resume)))
            return updated
        return await self._run(renew)

    async def checkpoint(self, lease: StreamLease, resume: ResumeState):
        resume = ResumeState.model_validate(resume)
        async def save(tx):
            _, _, now = await self._current(tx, lease)
            safe = self._resume(resume, now)
            if safe.transport_expires_at is not None and safe.transport_expires_at > lease.deadline:
                safe = safe.changed(transport_expires_at=lease.deadline)
            tx.set(_path(lease.device_id), _encode("voice_stream", _Claim(lease=lease, resume=safe)))
        await self._run(save)

    async def release(self, lease: StreamLease):
        """Cleanup even after expiry/revoke. Exact fence, no authority granted."""
        async def release(tx):
            current = _decode(await tx.get(_path(lease.device_id)), "voice_stream", _Claim)
            if current.lease != lease:
                raise AccessDenied("voice_stream_stale")
            tx.set(_path(lease.device_id), _encode("voice_stream", _Claim(
                lease=lease, resume=current.resume, released=True)))
        await self._run(release)
