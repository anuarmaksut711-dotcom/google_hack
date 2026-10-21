"""Saved university classes constrain recovery without rewriting raw availability."""
import asyncio
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from campuspulse_recovery.domain.models import Interval, canonical_hash
from campuspulse_recovery.lms.manual_schedule import ManualSchedule, WeeklySchedule
from campuspulse_recovery.lms.planning import input_version
from campuspulse_recovery.security.context import AccessDenied
from tests.lms.test_planning import setup, update
from tests.storage.test_plans import NOW


def local_day(h):
    return NOW.astimezone(ZoneInfo(h.config.student.timezone)).date() + timedelta(days=1)


def at(h, value, *, day=None):
    return datetime.combine(day or local_day(h), time.fromisoformat(value),
                            ZoneInfo(h.config.student.timezone))


def weekly(h, lessons=(), **changes):
    day = local_day(h)
    return WeeklySchedule.model_validate({
        'valid_from': day - timedelta(days=1), 'valid_until': day + timedelta(days=6),
        'classes': [{'subject': 'University class', 'weekday': day.weekday(),
                     'start': start, 'end': end} for start, end in lessons], **changes})


def schedule_store(h):
    return ManualSchedule(h.documents, h.policy, clock=lambda: h.clock)


@pytest.mark.parametrize('lessons,expected', [
    ([('09:00', '10:20')], [('10:30', '13:00')]),
    ([('09:20', '10:20')], [('09:00', '09:15'), ('10:30', '13:00')]),
    ([('08:00', '09:00'), ('13:00', '14:00')], [('09:00', '13:00')]),
    ([('08:00', '09:20'), ('12:40', '14:00')], [('09:30', '12:30')]),
    ([('08:00', '14:00')], []),
    ([('10:00', '11:00'), ('11:00', '12:00')], [('09:00', '10:00'), ('12:00', '13:00')]),
])
def test_classes_are_subtracted_clipped_and_conservatively_rounded(lessons, expected):
    async def scenario():
        h, inputs, _ = await setup()
        raw = Interval(start=at(h, '09:00'), end=at(h, '13:00'))
        with h.request() as context:
            await schedule_store(h).save(context, weekly(h, lessons))
            saved = await inputs.update(context, update(working_windows=[raw.model_dump()]))
            state = await inputs.get(context)
            constraints = inputs.constraints(state, now=NOW)
            assert constraints.working_windows == tuple(
                Interval(start=at(h, start), end=at(h, end)) for start, end in expected)
            assert state.working_windows == saved.working_windows == (raw,)
            assert state.model_dump(mode='json') == saved.model_dump(mode='json')
            assert set(state.model_dump()) == {'schema_version', 'working_windows', 'estimates',
                                               'revision', 'updated_at'}
            # Raw availability is still what a fresh process/public GET reads.
            assert (await inputs.get(context)).working_windows == (raw,)
    asyncio.run(scenario())


@pytest.mark.parametrize('kind', ['unknown', 'empty', 'expired', 'future', 'partial'])
def test_unknown_empty_and_insufficient_schedule_periods_remain_distinct(kind):
    async def scenario():
        h, inputs, _ = await setup()
        raw = Interval(start=at(h, '09:00'), end=at(h, '13:00'))
        with h.request() as context:
            if kind != 'unknown':
                changes = {}
                if kind == 'expired':
                    changes = {'valid_from': local_day(h) - timedelta(days=8),
                               'valid_until': local_day(h) - timedelta(days=2)}
                elif kind == 'future':
                    changes = {'valid_from': local_day(h), 'valid_until': local_day(h) + timedelta(days=4)}
                elif kind == 'partial':
                    changes = {'valid_until': local_day(h)}
                await schedule_store(h).save(context, weekly(h, **changes))
            await inputs.update(context, update(working_windows=[raw.model_dump()]))
            state = await inputs.get(context)
            assert inputs.constraints(state, now=NOW).working_windows == (
                (raw,) if kind in {'unknown', 'empty'} else None)
            assert state.working_windows == (raw,)
            assert (await schedule_store(h).read(context))['connected'] is (kind != 'unknown')
    asyncio.run(scenario())


def test_class_basis_is_versioned_without_public_fields_or_changing_planning_revision():
    async def scenario():
        h, inputs, _ = await setup()
        with h.request() as context:
            before = await inputs.update(context, update())
            # Existing explicit-availability states retain their source version.
            assert input_version(before) == 'moodle_' + canonical_hash(before) + '_'
            await schedule_store(h).save(context, weekly(h))
            empty = await inputs.get(context)
            await schedule_store(h).save(context, weekly(h, [('09:00', '10:20')]))
            changed = await inputs.get(context)
            assert before.model_dump() == empty.model_dump() == changed.model_dump()
            assert len({input_version(before), input_version(empty), input_version(changed)}) == 3
    asyncio.run(scenario())


def test_class_read_uses_guard_transaction_and_rejects_foreign_saved_owner(monkeypatch):
    async def scenario():
        h, inputs, _ = await setup()
        raw = Interval(start=at(h, '09:00'), end=at(h, '13:00'))
        with h.request() as context:
            classes = schedule_store(h)
            await classes.save(context, weekly(h, [('09:00', '10:20')]))
            await inputs.update(context, update(working_windows=[raw.model_dump()]))
            async def outside_transaction(*args, **kwargs):
                raise AssertionError('Class constraints must use caller transaction')
            with monkeypatch.context() as guards:
                guards.setattr(h.documents, 'get', outside_transaction)
                async def read(tx):
                    assert (await classes.read(context, reader=tx))['connected']
                    return await inputs.get(context, reader=tx)
                state = await h.documents.transact(read)
                assert inputs.constraints(state, now=NOW).working_windows[0].start == at(h, '10:30')
            h.documents.data[classes.record]['owner_id'] = 'another-student'
            with pytest.raises(AccessDenied, match='class_schedule_owner_mismatch'):
                await inputs.get(context)
    asyncio.run(scenario())


def test_another_accounts_classes_cannot_constrain_owner_availability():
    async def scenario():
        h, inputs, _ = await setup()
        raw = Interval(start=at(h, '09:00'), end=at(h, '13:00'))
        h.documents.data['cp_class_schedules/another-student'] = {
            'owner_id': 'another-student', 'schedule': weekly(h, [('08:00', '14:00')]).model_dump(mode='json')}
        with h.request() as context:
            state = await inputs.update(context, update(working_windows=[raw.model_dump()]))
            assert inputs.constraints(state, now=NOW).working_windows == (raw,)
            assert not (await schedule_store(h).read(context))['connected']
    asyncio.run(scenario())


def test_feed_planning_reads_same_saved_class_record_and_preserves_raw_windows():
    async def scenario():
        from tests.lms.test_feed_recovery import setup as feed_setup
        h, _, inputs, _ = await feed_setup()
        h.clock = h.now
        raw = Interval(start=at(h, '09:00'), end=at(h, '13:00'))
        with h.request() as context:
            await schedule_store(h).save(context, weekly(h, [('09:00', '10:20')]))
            state = await inputs.update(context, {'expected_revision': 0,
                'working_windows': [raw.model_dump()],
                'estimates': [{'task_id': 'actual-uid@lms', 'remaining_minutes': 90}]})
            assert inputs.constraints(state, now=NOW).working_windows == (
                Interval(start=at(h, '10:30'), end=at(h, '13:00')),)
            assert (await inputs.get(context)).working_windows == (raw,)
    asyncio.run(scenario())
