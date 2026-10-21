"""Approved Calendar proposals from chat blocks or a saved weekly timetable.

Immutable intent and per-operation state live in the encrypted document port.
External I/O is exclusively invoked by the private MCP process. No model output
is an approval. Existing events are observed, never modified or adopted.
"""
import asyncio
import time
from datetime import UTC, datetime, time as wall_time, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from ..domain.models import Instant, canonical_hash, calendar_event_id
from ..domain.ports import BindingConflict
from ..domain.time_rules import local_time
from ..security.context import AccessDenied
from ..lms.manual_schedule import WeeklySchedule
from .local_calendar import CalendarConnectionError

WRITE_SCOPE = 'https://www.googleapis.com/auth/calendar.events.owned'
DISPATCH_SECONDS = 25


async def bounded_io(deadline, work):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CalendarConnectionError('calendar_write_unknown')
    async with asyncio.timeout(remaining):
        return await work()


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


def block_intervals(blocks):
    return [(datetime.fromisoformat(item['start']), datetime.fromisoformat(item['end']))
            for block in blocks for item in block.get('occurrences', [block])]


def relevant_snapshot(events, blocks, timezone, ignore=()):
    intervals = block_intervals(blocks)
    # Whole local days are relevant, including an event added beside a block.
    days = {t.astimezone(ZoneInfo(timezone)).date() for interval in intervals for t in interval}
    rows = []
    for event in events:
        if event['id'] in ignore or event.get('recurringEventId') in ignore:
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
        if plan.get('purpose') == 'classes':
            saved = await (reader or self.documents).get_class_schedule(owner)
            if canonical_hash(saved) != plan['class_basis']:
                raise BindingConflict('calendar_class_schedule_changed')
            if not saved or saved.get('owner_id') != owner:
                raise BindingConflict('calendar_class_schedule_unavailable')
            WeeklySchedule.model_validate(saved['schedule'])
            return plan['class_basis']
        return await self.class_basis(owner, plan['blocks'], plan['class_timezone'],
            expected=plan['class_basis'], reader=reader)

    async def class_blueprint(self, owner, *, class_timezone):
        """Derive weekly recurrence only from the saved, human-reviewed timetable."""
        saved = await self.documents.get_class_schedule(owner)
        try:
            if not saved or saved['owner_id'] != owner:
                raise ValueError()
            weekly = WeeklySchedule.model_validate(saved['schedule'])
            zone = ZoneInfo(class_timezone)
        except (ValueError, TypeError, KeyError):
            raise BindingConflict('calendar_class_schedule_unavailable') from None
        now, blocks = self.calendar.clock(), []
        for lesson in sorted(weekly.classes, key=lambda item: (item.weekday, item.start)):
            day = max(weekly.valid_from, now.astimezone(zone).date())
            day += timedelta(days=(lesson.weekday - day.weekday()) % 7)
            occurrences = []
            while day <= weekly.valid_until:
                try:
                    start = local_time(datetime.combine(day, wall_time.fromisoformat(lesson.start)), class_timezone).astimezone(zone)
                    end = local_time(datetime.combine(day, wall_time.fromisoformat(lesson.end)), class_timezone).astimezone(zone)
                except ValueError:
                    raise BindingConflict('calendar_class_schedule_ambiguous_time') from None
                if start >= now:
                    occurrences.append({'start': start.isoformat(), 'end': end.isoformat()})
                day += timedelta(days=7)
            if occurrences:
                final = datetime.fromisoformat(occurrences[-1]['start']).astimezone(UTC)
                blocks.append({'title': lesson.subject, **occurrences[0], 'timezone': class_timezone,
                    'weekday': lesson.weekday, 'valid_until': weekly.valid_until.isoformat(),
                    'recurrence': ['RRULE:FREQ=WEEKLY;UNTIL=' + final.strftime('%Y%m%dT%H%M%SZ')],
                    'occurrences': occurrences})
        if not blocks:
            raise BindingConflict('calendar_class_schedule_empty')
        intervals = block_intervals(blocks)
        first = min(start for start, _ in intervals).astimezone(zone)
        last = max(end for _, end in intervals).astimezone(zone)
        horizon = {'start': first.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),
                   'end': datetime.combine(last.date() + timedelta(days=1), wall_time.min, zone).isoformat()}
        return canonical_hash(saved), blocks, horizon

    async def class_horizon(self, owner, *, class_timezone):
        _, _, horizon = await self.class_blueprint(owner, class_timezone=class_timezone)
        return horizon

    async def propose_classes(self, owner, source, *, class_timezone):
        binding = await self.binding(owner)
        basis, blocks, horizon = await self.class_blueprint(owner, class_timezone=class_timezone)
        publication_key = 'classes:' + canonical_hash([basis, class_timezone,
            binding['calendar_id'], binding.get('binding_generation', binding['generation'])])
        publication = canonical_hash([owner, publication_key])
        existing = await self.documents.get(self.path('plan_requests', publication))
        previous = None
        if existing:
            plan, state = await self.preflight(owner, existing['plan_id'])
            # A possible write always keeps its original IDs for reconciliation.
            # Preparing again may only replace an intent proven never dispatched.
            if not await self.unstarted(plan, state):
                return plan
            self.validate_source(source, blocks)
            old_blocks = [{key: value for key, value in block.items()
                if key not in ('block_id', 'operation_id', 'event_id')} for block in plan['blocks']]
            expired = (state.get('approval') and self.calendar.clock() -
                datetime.fromisoformat(state['approval']['at']) > timedelta(minutes=30))
            if (old_blocks == blocks and plan.get('horizon') == horizon and
                    plan['timezone'] == source['timezone'] and state['status'] != 'stale' and not expired and
                    plan['snapshot'] == relevant_snapshot(source['events'], blocks, source['timezone'])):
                return plan
            previous = plan['plan_id']
        snapshot = relevant_snapshot(source['events'], blocks, source['timezone'])
        request_key = publication_key + ':' + canonical_hash([blocks, horizon, snapshot, previous])
        try:
            return await self.save_proposal(owner, request_key, blocks, source,
                class_basis=basis, class_timezone=class_timezone, purpose='classes', horizon=horizon,
                publication=(publication, previous))
        except BindingConflict as error:
            if str(error) != 'calendar_class_publication_changed':
                raise
            # A concurrent prepare/apply won the transaction. Return the active
            # immutable intent; never create a second possible write.
            current = await self.documents.get(self.path('plan_requests', publication))
            if not current:
                raise
            plan, _ = await self.preflight(owner, current['plan_id'])
            return plan

    async def unstarted(self, plan, state, *, reader=None, allow_applying=False):
        if (state['status'] == 'applying' and not allow_applying) or any(
                operation != 'pending' for operation in state['operations'].values()):
            return False
        reader = reader or self.documents
        for block in plan['blocks']:
            if await reader.get(self.path('owned_events', block['event_id'])) is not None:
                return False
        return True

    async def stale_unstarted_classes(self, owner, plan_id, *, allow_applying=False):
        async def save(tx):
            plan, state = await self.get(owner, plan_id, tx)
            if (plan.get('purpose') != 'classes' or
                    state['status'] not in (('approved', 'applying') if allow_applying else ('approved',)) or
                    not await self.unstarted(plan, state, reader=tx, allow_applying=allow_applying)):
                return False
            tx.set(self.path('plan_state', plan_id), {**state, 'status': 'stale', 'approval': None})
            return True
        return await self.documents.transact(save)

    def validate_source(self, source, blocks):
        try:
            now = self.calendar.clock()
            read_at = datetime.fromisoformat(source['read_at'])
            start, end = (datetime.fromisoformat(source['horizon'][key]) for key in ('start', 'end'))
            if (not timedelta(0) <= now - read_at <= timedelta(minutes=5) or
                    any(not start <= begin < finish <= end for begin, finish in block_intervals(blocks))):
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            raise BindingConflict('calendar_source_unavailable') from None

    async def read_source(self, owner, plan):
        source = (await self.calendar.read(owner, horizon=plan['horizon'])
                  if plan.get('purpose') == 'classes' else await self.calendar.read(owner))
        self.validate_source(source, plan['blocks'])
        return source

    async def preflight(self, owner, plan_id):
        async def check(tx):
            plan, state = await self.get(owner, plan_id, tx)
            await self.binding(owner, plan, tx)
            await self.current_basis(owner, plan, reader=tx)
            return plan, state
        return await self.documents.transact(check)

    async def propose(self, owner, request_key, raw_blocks, source, *, class_timezone):
        blocks = validate_blocks(raw_blocks, now=self.calendar.clock())
        class_basis = await self.class_basis(owner, blocks, class_timezone)
        return await self.save_proposal(owner, request_key, blocks, source,
            class_basis=class_basis, class_timezone=class_timezone)

    async def save_proposal(self, owner, request_key, blocks, source, *, class_basis,
                            class_timezone, purpose=None, horizon=None, publication=None):
        binding = await self.binding(owner)
        self.validate_source(source, blocks)
        for interval in block_intervals(blocks):
            if any(event.get('transparency') != 'transparent' and overlap(interval, event_interval(event, source['timezone'])) for event in source['events']):
                raise BindingConflict('calendar_plan_conflict')
        request = canonical_hash([owner, request_key])
        payload = {'owner': owner, 'calendar': binding['calendar_id'], 'generation': binding.get('binding_generation', binding['generation']),
                   'timezone': source['timezone'], 'blocks': blocks, 'request': request,
                   'snapshot': relevant_snapshot(source['events'], blocks, source['timezone']),
                   'class_basis': class_basis, 'class_timezone': class_timezone}
        if purpose == 'classes':
            payload.update(purpose=purpose, horizon=horizon)
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
            replaced = None
            if publication is not None:
                publication_path = self.path('plan_requests', publication[0])
                current = await tx.get(publication_path)
                if (current or {}).get('plan_id') != publication[1]:
                    raise BindingConflict('calendar_class_publication_changed')
                if publication[1] is not None:
                    prior, prior_state = await self.get(owner, publication[1], tx)
                    if not await self.unstarted(prior, prior_state, reader=tx):
                        raise BindingConflict('calendar_class_publication_changed')
                    replaced = prior_state
            if old:
                if old != {'plan_id': plan_id}:
                    raise BindingConflict('calendar_request_changed')
                return
            tx.create(request_path, {'plan_id': plan_id})
            tx.create(plan_path, payload)
            tx.create(state_path, {'owner': owner, 'hash': payload['hash'], 'status': 'draft',
                'created_at': self.calendar.clock().isoformat(), 'operations': {b['operation_id']: 'pending' for b in blocks}})
            tx.set(index_path, {'ids': ([plan_id] + index['ids'])[:20]})
            if publication is not None:
                if replaced is not None:
                    tx.set(self.path('plan_state', publication[1]), {**replaced, 'status': 'stale', 'approval': None})
                    tx.set(publication_path, {'plan_id': plan_id})
                else:
                    tx.create(publication_path, {'plan_id': plan_id})
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
        self.validate_source(source, plan['blocks'])
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
            event.get('extendedProperties', {}).get('private') == self.marker(plan, block) and
            event.get('recurrence', []) == block.get('recurrence', []) and
            (not block.get('recurrence') or (
                event['start'].get('timeZone') == block['timezone'] and
                event['end'].get('timeZone') == block['timezone'])))

    def recurrence_matches(self, plan, source):
        for block in plan['blocks']:
            if not block.get('recurrence'):
                continue
            instances = [event for event in source['events'] if event.get('recurringEventId') == block['event_id']]
            if (len(instances) != len(block['occurrences']) or
                    any(event.get('summary') != block['title'] or event.get('transparency') == 'transparent'
                        for event in instances) or
                    sorted(event_interval(event, plan['timezone']) for event in instances) !=
                    sorted(block_intervals([block]))):
                return False
        return True

    def marker(self, plan, block):
        return {'cp_owner': plan['owner'], 'cp_plan': plan['plan_id'], 'cp_operation': block['operation_id']}

    async def apply(self, owner, plan_id, *, session_check):
        deadline = time.monotonic() + DISPATCH_SECONDS
        plan, initial_state = await self.get(owner, plan_id)
        await self.binding(owner, plan)
        if (plan.get('purpose') == 'classes' and any(
                initial_state['operations'][block['operation_id']] == 'pending' and
                datetime.fromisoformat(block['start']) < self.calendar.clock() for block in plan['blocks']) and
                await self.stale_unstarted_classes(owner, plan_id)):
            return {'status': 'stale', 'plan_id': plan_id}
        if not await self.claim(owner, plan_id):
            return await self.verify(owner, plan_id, session_check=session_check, _deadline=deadline)
        try:
            await session_check()
            binding = await self.binding(owner, plan)
            if WRITE_SCOPE not in binding['tokens'].get('scopes', []):
                raise BindingConflict('calendar_write_consent_required')
            source = await bounded_io(deadline, lambda: self.read_source(owner, plan))
            ours = [b['event_id'] for b in plan['blocks']]
            if relevant_snapshot(source['events'], plan['blocks'], plan['timezone'], ours) != plan['snapshot']:
                await self.state(owner, plan_id, status='stale')
                await self.release(owner, plan_id)
                return {'status': 'stale', 'plan_id': plan_id}
            for block in plan['blocks']:
                if time.monotonic() >= deadline:
                    raise CalendarConnectionError('calendar_write_unknown')
                await session_check()
                binding = await self.binding(owner, plan)
                _, state = await self.get(owner, plan_id)
                previous = state['operations'][block['operation_id']]
                if (plan.get('purpose') == 'classes' and previous == 'pending' and
                        datetime.fromisoformat(block['start']) < self.calendar.clock() and
                        await self.stale_unstarted_classes(owner, plan_id, allow_applying=True)):
                    await self.release(owner, plan_id)
                    return {'status': 'stale', 'plan_id': plan_id}
                await self.owned(owner, plan, block)
                event = await bounded_io(deadline, lambda: self.io.get(binding, block['event_id']))
                if event is not None:
                    if previous == 'pending' or not self.matches(plan, block, event):
                        raise BindingConflict('calendar_event_changed')
                elif previous == 'confirmed':
                    raise BindingConflict('calendar_event_changed')
                else:
                    if datetime.fromisoformat(block['start']) < self.calendar.clock():
                        raise BindingConflict('calendar_plan_time')
                    source = await bounded_io(deadline, lambda: self.read_source(owner, plan))
                    if relevant_snapshot(source['events'], plan['blocks'], plan['timezone'], ours) != plan['snapshot']:
                        raise BindingConflict('calendar_source_changed')
                    binding = await self.binding(owner, plan)
                    ops = {**state['operations'], block['operation_id']: 'unknown'}
                    await self.state(owner, plan_id, check_basis=True, operations=ops)
                    await session_check()
                    # The final guard transaction follows reconciliation GET
                    # and precedes the single bounded insert (never retried).
                    await self.preflight(owner, plan_id)
                    body = {
                        'id': block['event_id'], 'summary': block['title'],
                        'start': {'dateTime': block['start']}, 'end': {'dateTime': block['end']},
                        'extendedProperties': {'private': self.marker(plan, block)}}
                    if block.get('recurrence'):
                        body.update(recurrence=block['recurrence'])
                        body['start']['timeZone'] = body['end']['timeZone'] = block['timezone']
                    event = await bounded_io(deadline, lambda: self.io.create(binding, body))
                    # Insert response is not final verification; GET below decides.
                    event = await bounded_io(deadline, lambda: self.io.get(binding, block['event_id']))
                    if not self.matches(plan, block, event):
                        raise CalendarConnectionError('calendar_write_unknown')
                _, state = await self.get(owner, plan_id)
                await self.state(owner, plan_id, check_basis=True,
                    operations={**state['operations'], block['operation_id']: 'confirmed'})
            await self.state(owner, plan_id, status='applied_unverified')
            return await self.verify(owner, plan_id, session_check=session_check, _deadline=deadline)
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

    async def verify(self, owner, plan_id, *, session_check, _deadline=None):
        deadline = time.monotonic() + DISPATCH_SECONDS if _deadline is None else _deadline
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
                event = await bounded_io(deadline, lambda: self.io.get(binding, block['event_id']))
                if not self.matches(plan, block, event):
                    raise CalendarConnectionError('calendar_final_verification_failed')
                operations[block['operation_id']] = 'confirmed'
                link = event.get('htmlLink', '')
                if link.startswith(('https://www.google.com/calendar/', 'https://calendar.google.com/')):
                    links.append(link)
            # Fresh availability catches new conflicts elsewhere, not only changed etags.
            source = await bounded_io(deadline, lambda: self.read_source(owner, plan))
            if not self.recurrence_matches(plan, source):
                raise CalendarConnectionError('calendar_final_verification_failed')
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
