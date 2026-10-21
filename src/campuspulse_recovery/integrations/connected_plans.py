"""Create-only, approved Calendar proposals from structured chat blocks.

Immutable intent and per-operation state live in the encrypted document port.
External I/O is exclusively invoked by the private MCP process. No model output
is an approval. Existing events are observed, never modified or adopted.
"""
import asyncio
import time
from datetime import datetime, time as wall_time, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from ..domain.models import Instant, canonical_hash, calendar_event_id
from ..domain.ports import BindingConflict
from ..security.context import AccessDenied
from ..lms.manual_schedule import WeeklySchedule
from .local_calendar import CalendarConnectionError

WRITE_SCOPE = 'https://www.googleapis.com/auth/calendar.events.owned'


class NewBlock(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    title: str = Field(min_length=1, max_length=160)
    start: Instant
    end: Instant


def overlap(a, b):
    return a[0] < b[1] and b[0] < a[1]


def event_interval(event, timezone):
    zone = ZoneInfo(timezone)
    def point(value):
        if 'dateTime' in value:
            result = datetime.fromisoformat(value['dateTime'])
            if result.tzinfo is None:
                raise CalendarConnectionError('calendar_time_invalid')
            return result
        return datetime.fromisoformat(value['date']).replace(tzinfo=zone)
    return point(event['start']), point(event['end'])


def relevant_snapshot(events, blocks, timezone, ignore=()):
    intervals = [(datetime.fromisoformat(b['start']), datetime.fromisoformat(b['end'])) for b in blocks]
    # Whole local days are relevant, including an event added beside a block.
    days = {t.astimezone(ZoneInfo(timezone)).date() for interval in intervals for t in interval}
    rows = []
    for event in events:
        if event['id'] in ignore:
            continue
        start, end = event_interval(event, timezone)
        if any(start.astimezone(ZoneInfo(timezone)).date() <= day <= end.astimezone(ZoneInfo(timezone)).date() for day in days):
            rows.append({k: event.get(k) for k in ('id', 'etag', 'start', 'end', 'transparency')})
    return canonical_hash(sorted(rows, key=lambda v: v['id']))


def validate_blocks(raw, *, now):
    if not isinstance(raw, list) or not 1 <= len(raw) <= 12:
        raise BindingConflict('calendar_plan_size')
    blocks = [NewBlock.model_validate(b) for b in raw]
    for b in blocks:
        if (not b.title.strip() or '\n' in b.title or '\r' in b.title or
                not now <= b.start < b.end <= now + timedelta(hours=72)):
            raise BindingConflict('calendar_plan_time')
    blocks.sort(key=lambda b: b.start)
    if any(overlap((a.start, a.end), (b.start, b.end)) for a, b in zip(blocks, blocks[1:])):
        raise BindingConflict('calendar_plan_conflict')
    return [b.model_dump(mode='json') for b in blocks]


class ConnectedPlans:
    def __init__(self, calendar, *, io=None):
        self.calendar, self.documents = calendar, calendar.documents
        self.io = io or GoogleCreates()

    def path(self, kind, key):
        return 'cp_calendar_' + kind + '/' + key

    async def binding(self, owner, expected=None, reader=None):
        value = await (reader or self.documents).get('cp_local_google_calendars/' + owner)
        if not value or not value.get('connected') or value.get('owner') != owner:
            raise BindingConflict('calendar_not_connected')
        if expected and (value['calendar_id'], value.get('binding_generation', value['generation'])) != (expected['calendar'], expected['generation']):
            raise BindingConflict('calendar_connection_changed')
        return value

    async def class_basis(self, owner, blocks, timezone, *, expected=None, reader=None):
        """Use the same transaction as consent/intent, never a cached projection."""
        reader = reader or self.documents
        saved = await reader.get_class_schedule(owner)
        basis = canonical_hash(saved)
        if expected is not None and basis != expected:
            raise BindingConflict('calendar_class_schedule_changed')
        try:
            if not saved or saved['owner_id'] != owner:
                raise ValueError()
            weekly = WeeklySchedule.model_validate(saved['schedule'])
            zone = ZoneInfo(timezone)
            start = datetime.combine(weekly.valid_from, wall_time.min, zone)
            end = datetime.combine(weekly.valid_until + timedelta(days=1), wall_time.min, zone)
            if not start <= self.calendar.clock() < self.calendar.clock() + timedelta(hours=72) <= end:
                raise ValueError()
            for block in blocks:
                interval = (datetime.fromisoformat(block['start']), datetime.fromisoformat(block['end']))
                if not start <= interval[0] < interval[1] <= end:
                    raise ValueError()
                day = interval[0].astimezone(zone).date()
                while day <= interval[1].astimezone(zone).date():
                    for lesson in weekly.classes:
                        if lesson.weekday == day.weekday() and overlap(interval, (
                                datetime.combine(day, wall_time.fromisoformat(lesson.start), zone),
                                datetime.combine(day, wall_time.fromisoformat(lesson.end), zone))):
                            raise ValueError()
                    day += timedelta(days=1)
        except (ValueError, TypeError, KeyError):
            raise BindingConflict('calendar_class_schedule_unavailable') from None
        return basis

    async def current_basis(self, owner, plan, *, reader=None):
        if not plan.get('class_basis') or not plan.get('class_timezone'):
            raise BindingConflict('calendar_class_schedule_changed')
        return await self.class_basis(owner, plan['blocks'], plan['class_timezone'],
            expected=plan['class_basis'], reader=reader)

    async def preflight(self, owner, plan_id):
        async def check(tx):
            plan, state = await self.get(owner, plan_id, tx)
            await self.binding(owner, plan, tx)
            await self.current_basis(owner, plan, reader=tx)
            return plan, state
        return await self.documents.transact(check)

    async def propose(self, owner, request_key, raw_blocks, source, *, class_timezone):
        binding = await self.binding(owner)
        blocks = validate_blocks(raw_blocks, now=self.calendar.clock())
        class_basis = await self.class_basis(owner, blocks, class_timezone)
        horizon = source['horizon']
        if (datetime.fromisoformat(source['read_at']) > self.calendar.clock() or
                self.calendar.clock() - datetime.fromisoformat(source['read_at']) > timedelta(minutes=5) or
                any(not datetime.fromisoformat(horizon['start']) <= datetime.fromisoformat(block['start']) <
                    datetime.fromisoformat(block['end']) <= datetime.fromisoformat(horizon['end']) for block in blocks)):
            raise BindingConflict('calendar_source_unavailable')
        for block in blocks:
            interval = (datetime.fromisoformat(block['start']), datetime.fromisoformat(block['end']))
            if any(event.get('transparency') != 'transparent' and overlap(interval, event_interval(event, source['timezone'])) for event in source['events']):
                raise BindingConflict('calendar_plan_conflict')
        request = canonical_hash([owner, request_key])
        payload = {'owner': owner, 'calendar': binding['calendar_id'], 'generation': binding.get('binding_generation', binding['generation']),
                   'timezone': source['timezone'], 'blocks': blocks, 'request': request,
                   'snapshot': relevant_snapshot(source['events'], blocks, source['timezone']),
                   'class_basis': class_basis, 'class_timezone': class_timezone}
        plan_id = canonical_hash(payload)
        payload['plan_id'] = plan_id
        for i, block in enumerate(blocks):
            block_id = canonical_hash([owner, request, i])
            block.update(block_id=block_id, operation_id='op_' + canonical_hash([plan_id, i]),
                         event_id=calendar_event_id(owner, binding['calendar_id'], block_id))
        payload['hash'] = canonical_hash(payload)
        async def save(tx):
            request_path = self.path('plan_requests', request)
            old = await tx.get(request_path)
            index_path = self.path('plan_index', owner)
            index = await tx.get(index_path) or {'ids': []}
            plan_path, state_path = self.path('plans', plan_id), self.path('plan_state', plan_id)
            await tx.get(plan_path)
            await tx.get(state_path)
            await self.binding(owner, payload, tx)
            await self.current_basis(owner, payload, reader=tx)
            if old:
                if old != {'plan_id': plan_id}:
                    raise BindingConflict('calendar_request_changed')
                return
            tx.create(request_path, {'plan_id': plan_id})
            tx.create(plan_path, payload)
            tx.create(state_path, {'owner': owner, 'hash': payload['hash'], 'status': 'draft',
                'created_at': self.calendar.clock().isoformat(), 'operations': {b['operation_id']: 'pending' for b in blocks}})
            tx.set(index_path, {'ids': ([plan_id] + index['ids'])[:20]})
        await self.documents.transact(save)
        return payload

    async def get(self, owner, plan_id, reader=None):
        reader = reader or self.documents
        if len(plan_id) != 64 or any(c not in '0123456789abcdef' for c in plan_id):
            raise AccessDenied('calendar_plan_required')
        plan = await reader.get(self.path('plans', plan_id))
        state = await reader.get(self.path('plan_state', plan_id))
        if not plan or not state or plan['owner'] != owner or state['owner'] != owner:
            raise AccessDenied('calendar_plan_owner')
        if canonical_hash({k: v for k, v in plan.items() if k != 'hash'}) != plan['hash'] or state['hash'] != plan['hash']:
            raise AccessDenied('calendar_plan_integrity')
        return plan, state

    async def list(self, owner):
        index = await self.documents.get(self.path('plan_index', owner)) or {'ids': []}
        result = []
        for plan_id in index['ids']:
            plan, state = await self.get(owner, plan_id)
            try:
                await self.preflight(owner, plan_id)
            except BindingConflict:
                # A historical successful write is not evidence that the
                # proposal still fits the user's current class schedule.
                state = {**state, 'status': 'stale', 'verified_at': None, 'links': []}
            result.append({'plan': plan, 'state': state})
        return result

    async def approve(self, owner, plan_id, payload_hash, session, *, source):
        plan, _ = await self.get(owner, plan_id)
        await self.binding(owner, plan)
        if (source['timezone'] != plan['timezone'] or
                relevant_snapshot(source['events'], plan['blocks'], plan['timezone']) != plan['snapshot']):
            raise BindingConflict('calendar_source_changed')
        async def save(tx):
            plan, state = await self.get(owner, plan_id, tx)
            await self.binding(owner, plan, tx)
            await self.current_basis(owner, plan, reader=tx)
            if plan['hash'] != payload_hash or state['status'] != 'draft':
                raise BindingConflict('calendar_approval_changed')
            tx.set(self.path('plan_state', plan_id), {**state, 'status': 'approved',
                'approval': {'hash': payload_hash, 'session': session, 'at': self.calendar.clock().isoformat()}})
        await self.documents.transact(save)

    async def state(self, owner, plan_id, *, check_basis=False, **updates):
        async def save(tx):
            plan, state = await self.get(owner, plan_id, tx)
            if check_basis:
                await self.binding(owner, plan, tx)
                await self.current_basis(owner, plan, reader=tx)
            tx.set(self.path('plan_state', plan_id), {**state, **updates})
        await self.documents.transact(save)

    async def claim(self, owner, plan_id):
        plan, _ = await self.get(owner, plan_id)
        lock_path = self.path('plan_locks', canonical_hash(plan['calendar']))
        async def save(tx):
            saved, state = await self.get(owner, plan_id, tx)
            lock = await tx.get(lock_path)
            await self.binding(owner, saved, tx)
            await self.current_basis(owner, saved, reader=tx)
            if state['status'] == 'verified':
                return False
            approval = state.get('approval')
            if (not approval or approval['hash'] != saved['hash'] or
                    self.calendar.clock() - datetime.fromisoformat(approval['at']) > timedelta(minutes=30)):
                raise AccessDenied('calendar_exact_approval_required')
            if state['status'] == 'applying' or (lock and lock['plan_id'] != plan_id):
                raise BindingConflict('calendar_apply_busy')
            if state['status'] not in ('approved', 'partial', 'unknown', 'applied_unverified'):
                raise BindingConflict('calendar_new_approval_required')
            tx.set(lock_path, {'plan_id': plan_id})
            tx.set(self.path('plan_state', plan_id), {**state, 'status': 'applying', 'started_at': self.calendar.clock().isoformat()})
            return True
        return await self.documents.transact(save)

    async def release(self, owner, plan_id):
        plan, _ = await self.get(owner, plan_id)
        path = self.path('plan_locks', canonical_hash(plan['calendar']))
        async def release(tx):
            lock = await tx.get(path)
            if lock and lock.get('plan_id') == plan_id:
                tx.set(path, {})
        await self.documents.transact(release)

    async def owned(self, owner, plan, block):
        path = self.path('owned_events', block['event_id'])
        record = {'owner': owner, 'calendar': plan['calendar'], 'plan_id': plan['plan_id'],
                  'operation_id': block['operation_id'], 'block_id': block['block_id']}
        async def save(tx):
            old = await tx.get(path)
            if old and old != record:
                raise AccessDenied('calendar_foreign_event')
            if not old:
                tx.create(path, record)
        await self.documents.transact(save)
        return record

    def matches(self, plan, block, event):
        return bool(event and event.get('status') != 'cancelled' and event.get('etag') and
            event.get('summary') == block['title'] and event.get('id') == block['event_id'] and
            event_interval(event, plan['timezone']) == (datetime.fromisoformat(block['start']), datetime.fromisoformat(block['end'])) and
            event.get('extendedProperties', {}).get('private') == self.marker(plan, block))

    def marker(self, plan, block):
        return {'cp_owner': plan['owner'], 'cp_plan': plan['plan_id'], 'cp_operation': block['operation_id']}

    async def apply(self, owner, plan_id, *, session_check):
        started = time.monotonic()
        plan, _ = await self.get(owner, plan_id)
        await self.binding(owner, plan)
        if not await self.claim(owner, plan_id):
            return await self.verify(owner, plan_id, session_check=session_check)
        try:
            await session_check()
            binding = await self.binding(owner, plan)
            if WRITE_SCOPE not in binding['tokens'].get('scopes', []):
                raise BindingConflict('calendar_write_consent_required')
            source = await self.calendar.read(owner)
            ours = [b['event_id'] for b in plan['blocks']]
            if relevant_snapshot(source['events'], plan['blocks'], plan['timezone'], ours) != plan['snapshot']:
                await self.state(owner, plan_id, status='stale')
                await self.release(owner, plan_id)
                return {'status': 'stale', 'plan_id': plan_id}
            for block in plan['blocks']:
                if time.monotonic() - started > 25:
                    raise CalendarConnectionError('calendar_write_unknown')
                await session_check()
                binding = await self.binding(owner, plan)
                _, state = await self.get(owner, plan_id)
                previous = state['operations'][block['operation_id']]
                await self.owned(owner, plan, block)
                event = await self.io.get(binding, block['event_id'])
                if event is not None:
                    if previous == 'pending' or not self.matches(plan, block, event):
                        raise BindingConflict('calendar_event_changed')
                elif previous == 'confirmed':
                    raise BindingConflict('calendar_event_changed')
                else:
                    if datetime.fromisoformat(block['start']) < self.calendar.clock():
                        raise BindingConflict('calendar_plan_time')
                    source = await self.calendar.read(owner)
                    if relevant_snapshot(source['events'], plan['blocks'], plan['timezone'], ours) != plan['snapshot']:
                        raise BindingConflict('calendar_source_changed')
                    binding = await self.binding(owner, plan)
                    ops = {**state['operations'], block['operation_id']: 'unknown'}
                    await self.state(owner, plan_id, check_basis=True, operations=ops)
                    await session_check()
                    # The final guard transaction follows reconciliation GET
                    # and precedes the single bounded insert (never retried).
                    await self.preflight(owner, plan_id)
                    event = await self.io.create(binding, {
                        'id': block['event_id'], 'summary': block['title'],
                        'start': {'dateTime': block['start']}, 'end': {'dateTime': block['end']},
                        'extendedProperties': {'private': self.marker(plan, block)}})
                    # Insert response is not final verification; GET below decides.
                    event = await self.io.get(binding, block['event_id'])
                    if not self.matches(plan, block, event):
                        raise CalendarConnectionError('calendar_write_unknown')
                _, state = await self.get(owner, plan_id)
                await self.state(owner, plan_id, check_basis=True,
                    operations={**state['operations'], block['operation_id']: 'confirmed'})
            await self.state(owner, plan_id, status='applied_unverified')
            return await self.verify(owner, plan_id, session_check=session_check)
        except (Exception, asyncio.CancelledError) as error:
            _, state = await self.get(owner, plan_id)
            if isinstance(error, BindingConflict) and (str(error).startswith('calendar_class_schedule') or
                                                        str(error) == 'calendar_source_changed'):
                await self.state(owner, plan_id, status='stale')
                await self.release(owner, plan_id)
                return {'status': 'stale', 'plan_id': plan_id}
            if state['status'] == 'applying':
                confirmed = sum(v == 'confirmed' for v in state['operations'].values())
                await self.state(owner, plan_id, status='partial' if confirmed else 'unknown')
            if isinstance(error, asyncio.CancelledError):
                raise
            return {'status': 'partial' if any(v == 'confirmed' for v in state['operations'].values()) else 'unknown',
                    'plan_id': plan_id, 'code': str(error) if isinstance(error, (BindingConflict, AccessDenied)) else 'calendar_write_unknown'}

    async def verify(self, owner, plan_id, *, session_check):
        plan, state = await self.get(owner, plan_id)
        if not state.get('approval') or state['status'] in ('draft', 'approved', 'stale'):
            raise AccessDenied('calendar_verification_requires_dispatch')
        if state['status'] == 'applying' and self.calendar.clock() - datetime.fromisoformat(state['started_at']) < timedelta(seconds=90):
            raise BindingConflict('calendar_apply_busy')
        await session_check()
        binding = await self.binding(owner, plan)
        links, operations = [], dict(state['operations'])
        try:
            for block in plan['blocks']:
                owned = await self.documents.get(self.path('owned_events', block['event_id']))
                if not owned or owned['owner'] != owner or owned['plan_id'] != plan_id:
                    raise CalendarConnectionError('calendar_final_verification_failed')
                event = await self.io.get(binding, block['event_id'])
                if not self.matches(plan, block, event):
                    raise CalendarConnectionError('calendar_final_verification_failed')
                operations[block['operation_id']] = 'confirmed'
                link = event.get('htmlLink', '')
                if link.startswith(('https://www.google.com/calendar/', 'https://calendar.google.com/')):
                    links.append(link)
            # Fresh availability catches new conflicts elsewhere, not only changed etags.
            source = await self.calendar.read(owner)
            if relevant_snapshot(source['events'], plan['blocks'], plan['timezone'], [b['event_id'] for b in plan['blocks']]) != plan['snapshot']:
                raise CalendarConnectionError('calendar_final_verification_failed')
            await session_check()
            at = self.calendar.clock().isoformat()
            await self.state(owner, plan_id, check_basis=True, status='verified',
                             operations=operations, verified_at=at, links=links)
            await self.release(owner, plan_id)
            return {'status': 'verified', 'plan_id': plan_id, 'verified_at': at, 'links': links}
        except Exception:
            await self.state(owner, plan_id, status='applied_unverified', operations=operations)
            return {'status': 'applied_unverified', 'plan_id': plan_id}


class GoogleCreates:
    """Bounded create/get only; no update, delete, attendees or public selectors."""
    async def get(self, binding, event_id):
        return await asyncio.to_thread(self.request, binding, event_id, None)

    async def create(self, binding, body):
        return await asyncio.to_thread(self.request, binding, body['id'], body)

    @staticmethod
    def request(binding, event_id, body):
        import json
        import requests
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
        from .google_clients import _RequestsHttp
        from .google_credentials import _BoundedAuthRequest, _AuthRequest
        credentials = Credentials.from_authorized_user_info(binding['tokens'])
        http = None
        try:
            if not credentials.valid:
                with requests.Session() as transport:
                    transport.trust_env = False
                    credentials.refresh(_BoundedAuthRequest(_AuthRequest(transport)))
            http = _RequestsHttp(credentials, lambda: None)
            events = build('calendar', 'v3', http=http, cache_discovery=False).events()
            request = (events.get(calendarId=binding['calendar_id'], eventId=event_id) if body is None else
                events.insert(calendarId=binding['calendar_id'], body=body, sendUpdates='none'))
            return request.execute(num_retries=0)
        except HttpError as error:
            if body is None and int(error.resp.status) == 404:
                return None
            if body is not None and int(error.resp.status) == 409:
                return None
            raise CalendarConnectionError('calendar_write_unknown') from None
        except Exception:
            raise CalendarConnectionError('calendar_write_unknown') from None
        finally:
            if http is not None:
                http.close()
