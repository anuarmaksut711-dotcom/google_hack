"""The server rejects conflicting or unverifiable model plans before display."""
from datetime import timedelta
import json

import pytest

from campuspulse_recovery.domain.chat_plan import ClassSchedule, render_advice
from campuspulse_recovery.domain.models import Interval
from campuspulse_recovery.domain.time_rules import instant

NOW = instant('2026-10-20T06:00:00Z')
COVERAGE = Interval(start='2026-10-20T00:00:00+05:00', end='2026-11-03T00:00:00+05:00')
LESSON = Interval(start='2026-10-20T12:00:00+05:00', end='2026-10-20T14:00:00+05:00')
READY = ClassSchedule(status='ready', coverage=COVERAGE, fetched_at=NOW, busy=(LESSON,))


def block(start='2026-10-20T14:00:00+05:00', end='2026-10-20T15:00:00+05:00', title='Algorithms'):
    return dict(title=title, start=start, end=end)


def render(*blocks, schedule=READY, message=''):
    return render_advice(json.dumps(dict(kind='plan', message=message, blocks=list(blocks))),
                         schedule=schedule, now=NOW, timezone='Asia/Almaty')


@pytest.mark.parametrize('start,end', [
    ('11:30', '12:01'), ('12:30', '13:00'), ('13:59', '14:30'), ('11:30', '14:30'),
])
def test_overlap_cannot_be_shown_as_a_plan(start, end):
    response = render(block(f'2026-10-20T{start}:00+05:00', f'2026-10-20T{end}:00+05:00'))
    assert 'пересечение с парой' in response
    assert 'Algorithms' not in response


def test_endpoints_are_allowed_and_output_is_server_rendered():
    response = render(block(), block('2026-10-20T06:00:00Z', '2026-10-20T07:00:00Z', 'Math'),
                      message='Untrusted alternative timetable must not appear')
    assert response.startswith('План проверен')
    assert '20.10.2026 14:00' in response and '20.10.2026 11:00' in response
    assert response.index('Math') < response.index('Algorithms')
    assert 'Untrusted' not in response


def test_utc_offsets_are_compared_as_instants():
    assert 'пересечение с парой' in render(block('2026-10-20T07:00:00Z', '2026-10-20T08:00:00Z'))


def test_overlapping_study_blocks_and_past_are_rejected():
    assert 'между собой' in render(block(), block(title='Math'))
    assert 'в прошлом' in render(block('2026-10-20T05:00:00Z', '2026-10-20T06:00:00Z'))


@pytest.mark.parametrize('start,end', [
    ('2026-11-02T23:30:00+05:00', '2026-11-03T00:01:00+05:00'),
    ('2026-11-03T14:00:00+05:00', '2026-11-03T15:00:00+05:00'),
])
def test_uncovered_dates_are_not_assumed_free(start, end):
    assert 'за период' in render(block(start, end))


@pytest.mark.parametrize('schedule', [
    ClassSchedule(status='unavailable'),
    ClassSchedule(status='ready', coverage=COVERAGE, fetched_at=NOW-timedelta(hours=25)),
    ClassSchedule(status='ready', coverage=COVERAGE, fetched_at=NOW+timedelta(minutes=1)),
])
def test_unavailable_stale_or_future_snapshot_blocks_plans(schedule):
    response = render(block(), schedule=schedule)
    assert 'Проверьте период расписания' in response and 'Algorithms' not in response


def test_disconnected_account_gets_explicit_unverified_draft():
    response = render(block(), schedule=ClassSchedule(status='not_connected'))
    assert 'не проверены' in response and 'Algorithms' in response
    assert 'План проверен' not in response


@pytest.mark.parametrize('item', [
    block('2026-10-20T14:00:00', '2026-10-20T15:00:00'),
    block(end='2026-10-20T13:00:00+05:00'),
    block(end='2026-10-20T14:00:00+05:00'),
    block(title='Algorithms\nAlternative schedule'),
])
def test_invalid_time_or_block_fails_closed(item):
    assert 'Не удалось проверить формат' in render(item)


def test_messages_are_not_claimed_as_checked_plans_and_mixed_schema_is_rejected():
    raw = json.dumps(dict(kind='message', message='Сколько часов вы можете выделить?', blocks=[]))
    assert render_advice(raw, schedule=READY, now=NOW, timezone='Asia/Almaty') == 'Сколько часов вы можете выделить?'
    mixed = json.dumps(dict(kind='message', message='Bad plan', blocks=[block()]))
    for value in (mixed, 'raw timetable', '{}'):
        assert 'Не удалось проверить формат' in render_advice(value, schedule=READY, now=NOW, timezone='Asia/Almaty')


def test_server_rejects_unexpected_fields_even_when_generation_schema_omits_constraint():
    raw = json.dumps(dict(kind='plan', message='', blocks=[dict(**block(), alternate='bad')]))
    assert 'Не удалось проверить формат' in render_advice(raw, schedule=READY, now=NOW, timezone='Asia/Almaty')
