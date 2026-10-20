"""Persistent calendar feed for Cloud Run; no instance-local user files."""
import asyncio
from datetime import UTC, datetime, timedelta
import hashlib
import zlib

import httpx

from ..lms.calendar_feed import (CalendarFeed, FeedSnapshot, LocalTaskStatuses,
                                MAX_BYTES, parse_feed, validate_feed_url)


class CloudCalendarFeed(CalendarFeed):
    def __init__(self, *, documents, url, owner_id, user_id, transport=None,
                 clock=lambda: datetime.now(UTC)):
        self._url = validate_feed_url(url, user_id)
        self._binding = hashlib.sha256(url.encode()).hexdigest()
        self.documents = documents
        self.owner_id, self.user_id = owner_id, user_id
        self.transport, self.clock, self.lock = transport, clock, asyncio.Lock()
        identity = hashlib.sha256((owner_id + ':' + self._binding).encode()).hexdigest()
        self.record = 'cp_calendar_feeds/' + identity
        self.status_record = 'cp_calendar_statuses/' + identity
        self.snapshot, self.last_attempt, self.error = None, None, None
        self.statuses = LocalTaskStatuses(user_id=user_id, binding=self._binding)

    def _local_statuses(self):
        return self.statuses

    async def load(self, *, reader=None):
        reader = reader or self.documents
        # Approval freshness can read these exact documents in its transaction.
        saved = await reader.get(self.record)
        statuses = await reader.get(self.status_record)
        if saved is not None:
            if saved['owner_id'] != self.owner_id or saved['binding'] != self._binding:
                raise ValueError('calendar_binding_mismatch')
            self.last_attempt = datetime.fromisoformat(saved['last_attempt']) if saved.get('last_attempt') else None
            self.error = saved.get('error')
            if saved.get('snapshot') is not None:
                inflater = zlib.decompressobj()
                raw = inflater.decompress(saved['snapshot'], 12_000_001)
                if len(raw) > 12_000_000 or not inflater.eof:
                    raise ValueError('calendar_cache_too_large')
                snapshot = FeedSnapshot.model_validate_json(raw)
                if snapshot.user_id != self.user_id:
                    raise ValueError('calendar_user_mismatch')
                self.snapshot = snapshot
        self.statuses = (LocalTaskStatuses.model_validate(statuses) if statuses else
                         LocalTaskStatuses(user_id=self.user_id, binding=self._binding))
        if (self.statuses.user_id, self.statuses.binding) != (self.user_id, self._binding):
            raise ValueError('calendar_status_binding_mismatch')

    async def set_local_status(self, task_id, done):
        async with self.lock:
            await self.load()
            if self.snapshot is None or not any(t.id == task_id for t in self.snapshot.tasks):
                raise ValueError('unknown_moodle_calendar_event')

            async def save(tx):
                previous = await tx.get(self.status_record)
                statuses = (LocalTaskStatuses.model_validate(previous) if previous else
                            LocalTaskStatuses(user_id=self.user_id, binding=self._binding))
                if (statuses.user_id, statuses.binding) != (self.user_id, self._binding):
                    raise ValueError('calendar_status_binding_mismatch')
                tasks = dict(statuses.tasks)
                if done:
                    tasks[task_id] = 'done'
                else:
                    tasks.pop(task_id, None)
                value = LocalTaskStatuses(user_id=self.user_id, binding=self._binding, tasks=tasks)
                if len(value.model_dump_json().encode('utf-8')) > 800_000:
                    raise ValueError('calendar_statuses_too_large')
                tx.set(self.status_record, value.model_dump(mode='json'))
                return value
            self.statuses = await self.documents.transact(save)

    async def refresh(self):
        async with self.lock:
            await self.load()
            now = self.clock()

            async def claim(tx):
                saved = await tx.get(self.record)
                if saved and saved.get('last_attempt') and now - datetime.fromisoformat(saved['last_attempt']) < timedelta(seconds=60):
                    return False
                value = dict(saved or {'owner_id': self.owner_id, 'binding': self._binding})
                value['last_attempt'] = now.isoformat()
                tx.set(self.record, value)
                return True

            if not await self.documents.transact(claim):
                return
            self.last_attempt = now
            try:
                async with httpx.AsyncClient(transport=self.transport, timeout=20,
                                             follow_redirects=False, trust_env=False) as client:
                    async with client.stream('GET', self._url, headers={'Accept': 'text/calendar'}) as response:
                        if response.status_code != 200:
                            raise ValueError('calendar_unavailable')
                        raw = bytearray()
                        async for chunk in response.aiter_bytes():
                            raw.extend(chunk)
                            if len(raw) > MAX_BYTES:
                                raise ValueError('calendar_too_large')
                snapshot = parse_feed(bytes(raw), user_id=self.user_id, fetched_at=self.clock())
                compressed = zlib.compress(snapshot.model_dump_json().encode('utf-8'))
                if len(compressed) > 900_000:
                    raise ValueError('calendar_cache_too_large')
            except Exception:
                self.error = 'unavailable'
                async def fail(tx):
                    saved = await tx.get(self.record)
                    if saved and saved.get('last_attempt') == now.isoformat():
                        tx.set(self.record, {**saved, 'error': 'unavailable'})
                await self.documents.transact(fail)
                return

            async def save(tx):
                saved = await tx.get(self.record)
                if saved and saved.get('last_attempt') == now.isoformat():
                    tx.set(self.record, {**saved, 'snapshot': compressed, 'error': None})
            await self.documents.transact(save)
            await self.load()
