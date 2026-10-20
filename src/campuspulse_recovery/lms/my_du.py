"""Owner-bound AITU My DU connection and bounded, read-only class schedule.

Protocol reference: remoodle/heresy apps/calendar/server/lib/my-du (trunk).
This is an independent Python adapter; provider payloads never enter chat.
"""
import asyncio
from datetime import UTC, date, datetime, time, timedelta
import json
import re
import secrets
from typing import Annotated, Literal
from urllib.parse import parse_qs, urlencode, urlsplit
from zoneinfo import ZoneInfo

import httpx
from pydantic import Field, StrictInt, model_validator

from ..domain.models import Contract, Instant, Interval
from ..domain.ports import BindingConflict
from ..security.context import AccessDenied

ORIGIN = 'https://my-du.astanait.edu.kz'
ZONE = ZoneInfo('Asia/Almaty')
MAX_BYTES = 1_500_000


class TermSettings(Contract):
    study_year: Annotated[StrictInt, Field(ge=2020, le=2100)]
    term: Literal[-1, 1, 2, 3]
    first_week_start: date

    @model_validator(mode='after')
    def valid_term(self):
        if (self.first_week_start.weekday() != 0 or
                self.first_week_start.year not in (self.study_year, self.study_year + 1)):
            raise ValueError('invalid_first_teaching_monday')
        return self


class ConnectMyDu(TermSettings):
    callback_url: str = Field(min_length=1, max_length=12000)


class ClassMeeting(Contract):
    interval: Interval
    subject: str = Field(min_length=1, max_length=240)


class ScheduleSnapshot(Contract):
    fetched_at: Instant
    coverage: Interval
    classes: tuple[ClassMeeting, ...] = Field(max_length=500)


class MyDuLoginError(ValueError):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class MyDuUnavailable(ValueError):
    def __init__(self, reconnect=False, *, reason='unavailable', provider_status=None):
        super().__init__('my_du_reconnect' if reconnect else 'my_du_unavailable')
        self.reconnect = reconnect
        self.reason = reason
        self.provider_status = provider_status


def credentials(value):
    if not isinstance(value, dict):
        raise MyDuUnavailable()
    result = {}
    for key in ('access_token', 'refresh_token'):
        token = value.get(key)
        if not isinstance(token, str) or not re.fullmatch(r'[\x21-\x3a\x3c-\x7e]{1,16000}', token):
            raise MyDuUnavailable()
        result[key] = token
    return result


def parse_week(payload, settings, week):
    """Validate the academic week before assigning concrete local dates."""
    if not isinstance(payload, dict) or any(type(payload.get(key)) is not int or payload[key] != expected
            for key, expected in (('studyYear', settings.study_year), ('term', settings.term), ('weekNumber', week))):
        raise MyDuUnavailable()
    slots = payload.get('slots')
    if not isinstance(slots, list) or len(slots) > 100:
        raise MyDuUnavailable()
    meetings = []
    try:
        for slot in slots:
            day = slot['weekDay']['id']
            if type(day) is not int or not 1 <= day <= 7 or not isinstance(slot['items'], list):
                raise ValueError()
            if len(slot['items']) > 100:
                raise ValueError()
            lesson_date = settings.first_week_start + timedelta(weeks=week - 1, days=day - 1)
            for lesson in slot['items']:
                match = re.fullmatch(r'(\d{1,2}):(\d{2})\s*[–—-]\s*(\d{1,2}):(\d{2})', lesson['classTime']['title'])
                if match is None:
                    raise ValueError()
                h1, m1, h2, m2 = map(int, match.groups())
                interval = Interval(start=datetime.combine(lesson_date, time(h1, m1), ZONE),
                                    end=datetime.combine(lesson_date, time(h2, m2), ZONE))
                meetings.append(ClassMeeting(interval=interval, subject=lesson['subjectName']))
                if len(meetings) > 500:
                    raise ValueError()
    except (KeyError, TypeError, ValueError, OverflowError):
        raise MyDuUnavailable() from None
    return meetings


class MyDuClient:
    def __init__(self, *, transport=None):
        self.transport = transport

    async def request(self, path, *, tokens=None, data=None):
        headers = {'Accept': 'application/json'}
        if tokens is not None:
            tokens = credentials(tokens)
            headers['Cookie'] = '; '.join(key + '=' + tokens[key] for key in ('access_token', 'refresh_token'))
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=12, follow_redirects=False,
                                         trust_env=False) as client:
                async with client.stream('GET' if data is None else 'POST', ORIGIN + '/api' + path,
                        params={'lang': 'en'}, headers=headers, **({} if data is None else {'json': data})) as response:
                    if response.status_code in (401, 403):
                        raise MyDuUnavailable(reconnect=True, reason='provider_rejected', provider_status=response.status_code)
                    if not 200 <= response.status_code < 300:
                        raise MyDuUnavailable(reason='provider_rejected', provider_status=response.status_code)
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > MAX_BYTES:
                            raise MyDuUnavailable()
                    return json.loads(raw)
        except (httpx.HTTPError, json.JSONDecodeError, UnicodeError):
            raise MyDuUnavailable(reason='transport_or_response') from None

    async def exchange(self, code):
        return credentials(await self.request('/auth/external-login', data={
            'provider': 'microsoft', 'token': code, 'email': None,
            'device_info': {'platform': 'web', 'device_id': None},
            'avatar_url': None, 'first_name': None, 'last_name': None}))


class MyDuAccount:
    """Durable per-owner record; transactional leases fence other replicas.

    Documents is the existing protected storage port (encrypted SQLite locally,
    private Firestore in cloud). No credentials or callback URLs in projections.
    """
    def __init__(self, documents, policy, *, transport=None, clock=lambda: datetime.now(UTC)):
        self.documents, self.policy, self.clock = documents, policy, clock
        self.owner = policy.config.student.trusted_user_id
        self.path = 'cp_my_du_accounts/' + self.owner
        self.client = MyDuClient(transport=transport)

    def authorize(self, context):
        self.policy.require_context(context)
        self.policy.require_owner(context, self.owner)

    async def load(self, reader=None):
        saved = await (reader or self.documents).get(self.path)
        if saved is not None and saved.get('owner_id') != self.owner:
            raise AccessDenied('my_du_owner_mismatch')
        return saved

    def login_requests(self, record):
        requests = [dict(item) for item in record.get('login_requests', [])]
        # Accept requests issued by the previous server revision during rollout.
        legacy = record.get('pending')
        if legacy and not any(item['state'] == legacy['state'] for item in requests):
            requests.append({**legacy, 'status': 'pending'})
        return [item for item in requests
                if self.clock() < datetime.fromisoformat(item['expires_at'])][-5:]

    async def start(self, context):
        self.authorize(context)
        state = secrets.token_urlsafe(32)
        async def save(tx):
            record = dict(await self.load(tx) or {'owner_id': self.owner})
            requests = self.login_requests(record)
            requests.append({'state': state, 'expires_at': (self.clock() + timedelta(minutes=10)).isoformat(),
                             'status': 'pending'})
            record.update(pending=None, login_requests=requests[-5:])
            tx.set(self.path, record)
        await self.documents.transact(save)
        query = urlencode({'client_id': '9f15860b-4243-4610-845e-428dc4ae43a8',
            'response_type': 'code', 'redirect_uri': ORIGIN + '/login', 'response_mode': 'fragment',
            'scope': 'offline_access user.read mail.read', 'state': state})
        return {'url': 'https://login.microsoftonline.com/158f15f3-83e0-4906-824c-69bdc50d9d61/oauth2/v2.0/authorize?' + query}

    async def connect(self, context, value):
        self.authorize(context)
        value = ConnectMyDu.model_validate(value)
        try:
            callback = urlsplit(value.callback_url)
            fields = parse_qs(callback.fragment, strict_parsing=True)
            code, state = fields['code'][0], fields['state'][0]
            if (callback.scheme != 'https' or callback.netloc != 'my-du.astanait.edu.kz'
                    or callback.path != '/login' or callback.query or not code
                    or len(code) > 10000 or any(len(v) != 1 for v in fields.values())):
                raise ValueError()
        except (KeyError, IndexError, ValueError):
            raise MyDuLoginError('my_du_callback_invalid') from None
        attempt = secrets.token_hex(16)
        async def consume(tx):
            record = await self.load(tx)
            if not record:
                return 'my_du_login_expired'
            requests = self.login_requests(record)
            pending = next((item for item in requests if secrets.compare_digest(item['state'], state)), None)
            if pending is None:
                return 'my_du_login_mismatch' if requests else 'my_du_login_expired'
            if pending['status'] != 'pending':
                return pending.get('error', 'my_du_login_used')
            pending['status'] = 'exchanging'
            record.update(pending=None, login_requests=requests, connecting=attempt)
            tx.set(self.path, record)
            return None
        # Return a domain rejection outside the transaction: the Firestore port
        # intentionally treats unexpected callback exceptions as unknown writes.
        error = await self.documents.transact(consume)
        if error:
            raise MyDuLoginError(error)
        try:
            tokens = await self.client.exchange(code)
        except MyDuUnavailable as error:
            # A code may already have been consumed by the provider. Never retry
            # that exchange; retain its actual outcome instead of calling it expiry.
            reason = ('my_du_login_rejected' if error.reason == 'provider_rejected'
                      else 'my_du_unavailable')
            async def failed(tx):
                record = await self.load(tx)
                if not record or record.get('connecting') != attempt:
                    return
                requests = self.login_requests(record)
                for item in requests:
                    if secrets.compare_digest(item['state'], state):
                        item.update(status='failed', error=reason)
                record.update(login_requests=requests, connecting=None)
                tx.set(self.path, record)
            await self.documents.transact(failed)
            raise
        settings = TermSettings(study_year=value.study_year, term=value.term, first_week_start=value.first_week_start)
        async def save(tx):
            old = await self.load(tx)
            if not old or old.get('connecting') != attempt:
                raise BindingConflict('my_du_connection_changed')
            requests = self.login_requests(old)
            for item in requests:
                if secrets.compare_digest(item['state'], state):
                    item['status'] = 'completed'
            tx.set(self.path, {'owner_id': self.owner, 'generation': attempt,
                'tokens': tokens, 'settings': settings.model_dump(mode='json'), 'snapshot': None,
                'last_attempt': None, 'error': None, 'pending': None, 'login_requests': requests})
        await self.documents.transact(save)
        return await self.refresh(context)

    async def disconnect(self, context):
        self.authorize(context)
        async def save(tx):
            tx.set(self.path, {'owner_id': self.owner})
        await self.documents.transact(save)

    def view(self, record):
        connected = bool(record and record.get('generation'))
        snapshot = None if not connected or record.get('snapshot') is None else ScheduleSnapshot.model_validate(record['snapshot'])
        stale = bool(snapshot is None or not timedelta(0) <= self.clock() - snapshot.fetched_at <= timedelta(hours=24)
                     or not snapshot.coverage.start <= self.clock() < snapshot.coverage.end)
        return {'connected': connected, 'stale': stale,
            'error': None if record is None else record.get('error'),
            'snapshot': None if snapshot is None else snapshot.model_dump(mode='json')}

    async def read(self, context):
        self.authorize(context)
        return self.view(await self.load())

    async def refresh(self, context, *, cached=False, timeout=40):
        self.authorize(context)
        now, lease = self.clock(), secrets.token_hex(16)
        async def claim(tx):
            record = await self.load(tx)
            if not record or not record.get('tokens'):
                return None
            if cached and not record.get('error') and record.get('snapshot'):
                snapshot = ScheduleSnapshot.model_validate(record['snapshot'])
                if (timedelta(0) <= now - snapshot.fetched_at < timedelta(minutes=15)
                        and snapshot.coverage.start <= now < snapshot.coverage.end):
                    return None
            if record.get('lease_until') and datetime.fromisoformat(record['lease_until']) > now:
                return None
            if record.get('last_attempt') and now - datetime.fromisoformat(record['last_attempt']) < timedelta(seconds=60):
                return None
            record.update(lease=lease, lease_until=(now + timedelta(seconds=90)).isoformat(), last_attempt=now.isoformat())
            tx.set(self.path, record)
            return record
        record = await self.documents.transact(claim)
        if record is None:
            return await self.read(context)
        generation, tokens = record['generation'], record['tokens']

        async def update(values):
            async def save(tx):
                latest = await self.load(tx)
                if not latest or latest.get('generation') != generation or latest.get('lease') != lease:
                    raise BindingConflict('my_du_connection_changed')
                latest.update(values)
                tx.set(self.path, latest)
            await self.documents.transact(save)

        async def get(path, data=None):
            nonlocal tokens
            try:
                return await self.client.request(path, tokens=tokens, data=data)
            except MyDuUnavailable as error:
                if not error.reconnect:
                    raise
            tokens = credentials(await self.client.request('/auth/refresh', tokens=tokens, data={}))
            # Persist rotated credentials before further requests, including if
            # the subsequent schedule read fails.
            await update({'tokens': tokens})
            return await self.client.request(path, tokens=tokens, data=data)

        try:
            async with asyncio.timeout(timeout):
                settings = TermSettings.model_validate(record['settings'])
                count = await get('/admin/client/settings/' + ('add_term_week_count' if settings.term == -1 else 'term_week_count'))
                weeks = int(count['value'])
                if str(weeks) != str(count['value']) or not 1 <= weeks <= 30:
                    raise MyDuUnavailable()
                first = datetime.combine(now.astimezone(ZONE).date(), time(), ZONE)
                last = first + timedelta(days=14)
                meetings = []
                for week in range(1, weeks + 1):
                    monday = datetime.combine(settings.first_week_start + timedelta(weeks=week - 1), time(), ZONE)
                    if monday >= last or monday + timedelta(days=7) <= first:
                        continue
                    payload = await get('/edu-process/classSchedule/student/me/search', {
                        'filters': [{'id': 'studyYear', 'value': str(settings.study_year)},
                                    {'id': 'term', 'value': str(settings.term)},
                                    {'id': 'weekNumber', 'value': str(week)}], 'start': 0, 'size': 100})
                    meetings.extend(parse_week(payload, settings, week))
                # Do not silently accept a wrong/outdated term as an empty timetable.
                term_start = datetime.combine(settings.first_week_start, time(), ZONE)
                term_end = term_start + timedelta(weeks=weeks)
                if term_start >= last or term_end <= first:
                    raise MyDuUnavailable()
                snapshot = ScheduleSnapshot(fetched_at=now,
                    coverage=Interval(start=max(first, term_start), end=min(last, term_end)),
                    classes=tuple(sorted((m for m in meetings if m.interval.start < last and m.interval.end > first),
                                         key=lambda m: (m.interval.start, m.interval.end, m.subject))))
                await update({'snapshot': snapshot.model_dump(mode='json'), 'error': None, 'lease_until': None})
        except (MyDuUnavailable, ValueError, KeyError, TypeError, TimeoutError) as error:
            reconnect = isinstance(error, MyDuUnavailable) and error.reconnect
            values = {'error': 'reconnect' if reconnect else 'unavailable', 'lease_until': None}
            if reconnect:
                values['tokens'] = None
            await update(values)
        return await self.read(context)

    async def calendar_lines(self, context):
        # Leave time for Gemini inside the chat endpoint's 60-second budget.
        state = await self.refresh(context, cached=True, timeout=8)
        if not state['connected']:
            return ('My DU не подключён. Расписание пар неизвестно; предложи подключить его на странице аккаунта.',)
        if state['stale'] or state['error'] or state['snapshot'] is None:
            return ('Расписание My DU недоступно или устарело. Попроси обновить подключение; не считай часы свободными.',)
        snapshot = ScheduleSnapshot.model_validate(state['snapshot'])
        lines = [f'My DU: пары — обязательные занятые интервалы. Не назначай работу на пересекающееся время. '
                 f'Период расписания: {snapshot.coverage.start.astimezone(ZONE).isoformat()} — '
                 f'{snapshot.coverage.end.astimezone(ZONE).isoformat()}. '
                 'За пределами периода пары неизвестны. Отсутствие пар не означает свободное время: уточни доступные часы.']
        # Preserve every occupied interval in a bounded source. Refuse a partial
        # timetable instead of hiding omitted busy time from the planner.
        meetings = [m for m in snapshot.classes if m.interval.end > self.clock()]
        if len(meetings) > 95:
            return ('Расписание My DU слишком велико для контекста. Попроси уточнить занятые часы; не считай их свободными.',)
        for meeting in meetings:
            lines.append(f'Пара {meeting.subject[:240]}: {meeting.interval.start.astimezone(ZONE).isoformat()} — '
                         f'{meeting.interval.end.astimezone(ZONE).isoformat()}; время занято.')
        return tuple(lines)
