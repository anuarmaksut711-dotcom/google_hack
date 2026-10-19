"""AITU's personal iCalendar feed: deadlines only, no invented grade/status data."""
from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, time, timedelta
import hashlib
import json
from pathlib import Path
import re
from typing import Literal
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import httpx
from icalendar import Calendar
from pydantic import Field

from ..domain.models import Contract, Instant, PositiveInt
from .models import Title

SITE = 'https://lms.astanait.edu.kz'
MAX_BYTES = 1_500_000


def validate_feed_url(value: str, expected_user_id: int) -> str:
    try:
        url = urlsplit(value)
        query = parse_qs(url.query, strict_parsing=True)
        if (len(value) > 4096 or value != value.strip() or url.scheme != 'https'
                or url.netloc != 'lms.astanait.edu.kz' or url.fragment
                or url.path != '/calendar/export_execute.php'
                or set(query) != {'userid', 'authtoken', 'preset_what', 'preset_time'}
                or any(len(v) != 1 for v in query.values())
                or query['userid'] != [str(expected_user_id)]
                or not re.fullmatch(r'[A-Za-z0-9]{16,128}', query['authtoken'][0])
                or query['preset_what'] != ['all']
                or query['preset_time'][0] not in {'recentupcoming', 'custom', 'monthnow', 'weeknow'}):
            raise ValueError()
    except Exception:
        raise ValueError('invalid_moodle_calendar_url') from None
    return value


class FeedTask(Contract):
    id: str = Field(min_length=1, max_length=512)
    title: Title
    course_title: str = Field(max_length=512)
    description: str = Field(default='', max_length=12000)
    due_at: Instant
    # Calendar UIDs are event IDs, never Moodle assignment IDs.
    submission_status: Literal['unknown'] = 'unknown'
    url: str | None = None
    local_status: Literal['active', 'done'] = 'active'
    view_group: Literal['upcoming', 'past', 'completed'] = 'upcoming'


class FeedSnapshot(Contract):
    site_url: Literal['https://lms.astanait.edu.kz'] = SITE
    user_id: PositiveInt
    fetched_at: Instant
    source_kind: Literal['calendar_feed'] = 'calendar_feed'
    courses: tuple = ()
    tasks: tuple[FeedTask, ...] = Field(max_length=5000)
    event_count: int = Field(ge=0, le=5000)
    attendance_count: int = Field(ge=0, le=5000)


class FeedState(Contract):
    owner_id: str
    site_url: Literal['https://lms.astanait.edu.kz'] = SITE
    expected_user_id: PositiveInt
    connected: bool = True
    stale: bool
    syncing: bool = False
    last_attempt: Instant | None = None
    last_success: Instant | None = None
    error: Literal['unavailable'] | None = None
    snapshot: FeedSnapshot | None = None


class LocalTaskStatuses(Contract):
    user_id: PositiveInt
    binding: str
    tasks: dict[str, Literal['done']] = Field(default_factory=dict, max_length=5000)


def parse_feed(raw: bytes, *, user_id: int, fetched_at: datetime) -> FeedSnapshot:
    if (len(raw) > MAX_BYTES or not raw.strip().startswith(b'BEGIN:VCALENDAR')
            or not raw.strip().endswith(b'END:VCALENDAR')):
        raise ValueError('invalid_moodle_calendar')
    try:
        calendar = Calendar.from_ical(raw)
        events = calendar.walk('VEVENT')
        if len(events) > 5000:
            raise ValueError()
        tasks, seen, attendance = [], set(), 0
        for event in events:
            if str(event.get('STATUS', '')).upper() == 'CANCELLED':
                continue
            if any(key in event for key in ('RRULE', 'RECURRENCE-ID', 'RDATE', 'EXDATE')):
                raise ValueError()
            uid, title = str(event.get('UID', '')).strip(), str(event.get('SUMMARY', '')).strip()
            if not uid or not title or uid in seen or 'DTSTART' not in event:
                raise ValueError()
            seen.add(uid)
            if re.match(r'^Attendance\b', title, re.I):
                attendance += 1
                continue
            due = event.decoded('DTSTART')
            if isinstance(due, datetime):
                if due.tzinfo is None:
                    due = due.replace(tzinfo=ZoneInfo('Asia/Qyzylorda'))
            elif isinstance(due, date):
                due = datetime.combine(due, time.min, tzinfo=ZoneInfo('Asia/Qyzylorda'))
            else:
                raise ValueError()
            categories = event.get('CATEGORIES')
            category = str(categories.cats[0]) if categories is not None and categories.cats else ''
            description = str(event.get('DESCRIPTION', ''))
            direct = str(event.get('URL', ''))
            # Only an explicit event URL may become an activity link. References
            # inside descriptions can point to a different assignment or policy.
            link = direct if re.fullmatch(re.escape(SITE) + r'/mod/(?:assign|quiz)/view\.php\?id=[1-9][0-9]*', direct) else None
            tasks.append(FeedTask(id=uid, title=title, course_title=category,
                description=description, due_at=due.astimezone(UTC), url=link))
        return FeedSnapshot(user_id=user_id, fetched_at=fetched_at,
            tasks=tuple(sorted(tasks, key=lambda task: (task.due_at, task.id))),
            event_count=len(events), attendance_count=attendance)
    except Exception:
        raise ValueError('invalid_moodle_calendar') from None


class CalendarFeed:
    """Server-only URL, bounded fetch, private cache and one-minute refresh limit."""
    def __init__(self, *, url_file: Path | None = None, url: str | None = None,
                 cache_file: Path, owner_id: str,
                 user_id: int, transport=None, clock=lambda: datetime.now(UTC)):
        if (url_file is None) == (url is None):
            raise ValueError('exactly_one_calendar_url_source_required')
        self._url = validate_feed_url(url_file.read_text(encoding='utf-8').strip() if url_file else url, user_id)
        self._binding = hashlib.sha256(self._url.encode()).hexdigest()
        self.cache_file, self.owner_id, self.user_id = cache_file, owner_id, user_id
        self.status_file = cache_file.with_name(cache_file.stem + '-task-statuses.json')
        self.transport, self.clock, self.lock = transport, clock, asyncio.Lock()
        self.snapshot = None
        self.last_attempt = None
        self.error = None
        if cache_file.exists():
            try:
                cached = json.loads(cache_file.read_text(encoding='utf-8'))
                if cached['binding'] == self._binding:
                    snapshot = FeedSnapshot.model_validate(cached['snapshot'])
                    if snapshot.user_id == user_id and snapshot.fetched_at <= clock():
                        self.snapshot = snapshot
            except Exception:
                pass

    def state(self):
        last = None if self.snapshot is None else self.snapshot.fetched_at
        snapshot = self.snapshot
        if snapshot is not None:
            statuses = self._local_statuses()
            tasks = tuple(task.model_copy(update={
                'local_status': 'done' if task.id in statuses.tasks else 'active',
                'view_group': 'completed' if task.id in statuses.tasks else
                    ('past' if task.due_at <= self.clock() else 'upcoming'),
            }) for task in snapshot.tasks)
            snapshot = snapshot.model_copy(update={'tasks': tasks})
        return FeedState(owner_id=self.owner_id, expected_user_id=self.user_id,
            stale=last is None or self.clock() - last > timedelta(minutes=30),
            syncing=self.lock.locked(), last_attempt=self.last_attempt,
            last_success=last, error=self.error, snapshot=snapshot)

    def _local_statuses(self):
        if not self.status_file.exists():
            return LocalTaskStatuses(user_id=self.user_id, binding=self._binding)
        try:
            value = LocalTaskStatuses.model_validate_json(self.status_file.read_text(encoding='utf-8'))
        except Exception:
            raise ValueError('local_task_statuses_invalid') from None
        if (value.user_id, value.binding) != (self.user_id, self._binding):
            return LocalTaskStatuses(user_id=self.user_id, binding=self._binding)
        return value

    async def set_local_status(self, task_id: str, done: bool):
        async with self.lock:
            if self.snapshot is None or not any(task.id == task_id for task in self.snapshot.tasks):
                raise ValueError('unknown_moodle_calendar_event')
            statuses = self._local_statuses()
            tasks = dict(statuses.tasks)
            if done:
                tasks[task_id] = 'done'
            else:
                tasks.pop(task_id, None)
            value = LocalTaskStatuses(user_id=self.user_id, binding=self._binding, tasks=tasks)
            self.status_file.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.status_file.with_suffix('.tmp')
            temporary.write_text(value.model_dump_json(), encoding='utf-8')
            temporary.replace(self.status_file)

    async def refresh(self):
        async with self.lock:
            now = self.clock()
            if self.last_attempt is not None and now - self.last_attempt < timedelta(seconds=60):
                return
            self.last_attempt = now
            try:
                async with httpx.AsyncClient(transport=self.transport, timeout=20,
                        follow_redirects=False, trust_env=False) as client:
                    async with client.stream('GET', self._url, headers={'Accept': 'text/calendar'}) as response:
                        if response.status_code != 200:
                            raise ValueError()
                        raw = bytearray()
                        async for chunk in response.aiter_bytes():
                            raw.extend(chunk)
                            if len(raw) > MAX_BYTES:
                                raise ValueError()
                snapshot = parse_feed(bytes(raw), user_id=self.user_id, fetched_at=self.clock())
                self.cache_file.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.cache_file.with_suffix('.tmp')
                temporary.write_text(json.dumps({'binding': self._binding,
                    'snapshot': snapshot.model_dump(mode='json')}, ensure_ascii=False), encoding='utf-8')
                temporary.replace(self.cache_file)
                self.snapshot, self.error = snapshot, None
            except Exception:
                # Never log HTTP exceptions: their text can include the secret URL.
                self.error = 'unavailable'
