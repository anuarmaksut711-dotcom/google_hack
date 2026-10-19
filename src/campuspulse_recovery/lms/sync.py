"""Durable Moodle cache with bounded polling, leases and fenced commits.

The configured LMS connection is independent of a browser session. No Moodle
credential is persisted here; only an injected reader can perform upstream IO.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
import secrets
from typing import Literal

from pydantic import Field

from ..domain.models import Contract, Instant
from ..domain.ports import BindingConflict
from ..storage.plans import DocumentStore, _decode, _encode, _key
from .client import MoodleError
from .models import MoodleSnapshot


ErrorCode = Literal['auth_failed', 'identity_mismatch', 'unavailable',
                    'invalid_response', 'unsupported', 'sync_timeout', 'sync_failed']


def utcnow():
    return datetime.now(UTC)


class MoodleState(Contract):
    owner_id: str
    site_url: str
    expected_user_id: int
    connected: bool
    stale: bool
    syncing: bool
    status: Literal['not_connected', 'never_synced', 'ready', 'stale', 'syncing', 'error']
    last_attempt: Instant | None = None
    last_success: Instant | None = None
    error: ErrorCode | None = None
    snapshot: MoodleSnapshot | None = None


class _Record(Contract):
    owner_id: str
    site_url: str
    expected_user_id: int
    enabled: bool = True
    generation: int = Field(default=0, ge=0)
    lease_id: str | None = None
    lease_until: Instant | None = None
    last_attempt: Instant | None = None
    last_success: Instant | None = None
    error: ErrorCode | None = None
    snapshot: MoodleSnapshot | None = None


class MoodleSync:
    """One trusted owner/site/account connection, safe across worker restarts."""

    def __init__(self, documents: DocumentStore, *, owner_id: str, site_url: str,
                 expected_user_id: int,
                 fetch_snapshot: Callable[[], Awaitable[MoodleSnapshot]] | None = None,
                 interval_minutes: int = 15, stale_after_minutes: int = 30,
                 clock=utcnow):
        if (not owner_id or owner_id != owner_id.strip()
                or not site_url.startswith('https://') or site_url.endswith('/')
                or type(expected_user_id) is not int or expected_user_id < 1
                or type(interval_minutes) is not int or not 1 <= interval_minutes <= 1440
                or type(stale_after_minutes) is not int or stale_after_minutes < interval_minutes):
            raise ValueError('invalid_moodle_sync_configuration')
        self.documents, self.fetch_snapshot, self.clock = documents, fetch_snapshot, clock
        self.owner_id, self.site_url, self.expected_user_id = owner_id, site_url, expected_user_id
        self.interval = timedelta(minutes=interval_minutes)
        self.stale_after = timedelta(minutes=stale_after_minutes)
        self.path = 'cp_moodle_connections/' + _key(owner_id, site_url, expected_user_id)

    def _new(self):
        return _Record(owner_id=self.owner_id, site_url=self.site_url,
                       expected_user_id=self.expected_user_id)

    def _load(self, raw):
        result = self._new() if raw is None else _decode(raw, 'moodle_connection', _Record)
        if (result.owner_id, result.site_url, result.expected_user_id) != (
                self.owner_id, self.site_url, self.expected_user_id):
            raise BindingConflict('moodle_connection_binding_mismatch')
        if result.snapshot is not None and (result.snapshot.site_url, result.snapshot.user_id) != (
                self.site_url, self.expected_user_id):
            raise BindingConflict('moodle_snapshot_binding_mismatch')
        return result

    def _state(self, record):
        now = self.clock()
        syncing = bool(record.enabled and record.lease_id and record.lease_until and now < record.lease_until)
        stale = record.snapshot is None or now - record.snapshot.fetched_at >= self.stale_after
        status = ('not_connected' if not record.enabled else 'syncing' if syncing else
                  'error' if record.error else 'never_synced' if record.snapshot is None else
                  'stale' if stale else 'ready')
        return MoodleState(owner_id=self.owner_id, site_url=self.site_url,
            expected_user_id=self.expected_user_id, connected=record.enabled,
            stale=stale, syncing=syncing, status=status, last_attempt=record.last_attempt,
            last_success=record.last_success, error=record.error, snapshot=record.snapshot)

    async def read(self) -> MoodleState:
        return self._state(self._load(await self.documents.get(self.path)))

    async def disconnect(self) -> MoodleState:
        """Explicit server action revokes future work and fences in-flight results."""
        async def revoke(tx):
            old = self._load(await tx.get(self.path))
            result = old.model_copy(update=dict(enabled=False, generation=old.generation + 1,
                lease_id=None, lease_until=None, snapshot=None, last_success=None, error=None))
            tx.set(self.path, _encode('moodle_connection', result))
            return result
        return self._state(await self.documents.transact(revoke))

    async def refresh(self, *, force: bool = False) -> MoodleState:
        if self.fetch_snapshot is None:
            raise RuntimeError('moodle_reader_has_no_refresh_authority')
        if type(force) is not bool:
            raise ValueError('invalid_refresh_request')
        lease_id = secrets.token_urlsafe(24)
        # A manual/on-open refresh cannot hammer LMS, including after failures.
        interval = timedelta(seconds=60) if force else self.interval

        async def claim(tx):
            old = self._load(await tx.get(self.path))
            now = self.clock()
            if (not old.enabled
                    or old.lease_id and old.lease_until and now < old.lease_until
                    or old.last_attempt and now - old.last_attempt < interval):
                return None
            value = old.model_copy(update=dict(generation=old.generation + 1,
                lease_id=lease_id, lease_until=now + timedelta(seconds=120), last_attempt=now))
            tx.set(self.path, _encode('moodle_connection', value))
            return value

        claimed = await self.documents.transact(claim)
        if claimed is None:
            return await self.read()
        snapshot, error = None, None
        try:
            async with asyncio.timeout(60):
                snapshot = MoodleSnapshot.model_validate(await self.fetch_snapshot())
            if (snapshot.site_url, snapshot.user_id) != (self.site_url, self.expected_user_id):
                error, snapshot = 'identity_mismatch', None
            elif (snapshot.fetched_at < claimed.last_attempt - timedelta(seconds=5)
                  or snapshot.fetched_at > self.clock() + timedelta(seconds=5)):
                error, snapshot = 'invalid_response', None
            if snapshot is not None and error is None:
                # Check the real document budget before the commit transaction.
                # Oversized complete reads settle as an error, retaining the last
                # good cache rather than stranding an active lease on every poll.
                try:
                    _encode('moodle_connection', claimed.model_copy(update=dict(
                        snapshot=snapshot, last_success=self.clock(), error=None)))
                except BindingConflict:
                    raise MoodleError('invalid_response') from None
        except asyncio.CancelledError:
            # An interrupted process leaves a durable lease, recovered on expiry.
            raise
        except TimeoutError:
            error = 'sync_timeout'
        except MoodleError as exc:
            error = exc.code if exc.code in ('auth_failed', 'identity_mismatch', 'unavailable',
                'invalid_response', 'unsupported') else 'sync_failed'
        except Exception:
            error = 'sync_failed'

        async def commit(tx):
            old = self._load(await tx.get(self.path))
            now = self.clock()
            if (not old.enabled or old.generation != claimed.generation
                    or old.lease_id != lease_id or old.lease_until is None or now >= old.lease_until):
                return old  # An expired worker can never overwrite a newer result.
            update = dict(lease_id=None, lease_until=None, error=error)
            if snapshot is not None and error is None:
                update.update(snapshot=snapshot, last_success=now)
            value = old.model_copy(update=update)
            tx.set(self.path, _encode('moodle_connection', value))
            return value
        return self._state(await self.documents.transact(commit))
