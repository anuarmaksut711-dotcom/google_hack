"""Structured chat plans, validated and rendered by the server before publication."""
from datetime import timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import Contract, Instant, Interval
from .time_rules import instant


class ClassSchedule(Contract):
    status: Literal['ready', 'not_connected', 'unavailable']
    coverage: Interval | None = None
    fetched_at: Instant | None = None
    busy: tuple[Interval, ...] = Field(default=(), max_length=500)

    @model_validator(mode='after')
    def complete_source(self):
        if self.status == 'ready' and (self.coverage is None or self.fetched_at is None):
            raise ValueError('class_schedule_incomplete')
        return self


class AdviceBlock(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str = Field(min_length=1, max_length=160)
    start: str = Field(description='ISO 8601 datetime with explicit UTC offset')
    end: str = Field(description='ISO 8601 datetime with explicit UTC offset')


class AdviceReply(BaseModel):
    """Model wire format; times appear only in blocks, never prose."""
    model_config = ConfigDict(extra='forbid')
    kind: Literal['message', 'plan']
    message: str = Field(max_length=4000, description='Questions or explanation for message; empty for plan')
    blocks: list[AdviceBlock] = Field(max_length=40)


def advice_output_schema():
    """Gemini responseSchema rejects additionalProperties, even when false.

    Keep strict extra-field rejection in the server models, but omit that keyword
    from the generation schema sent by ADK/GenAI.
    """
    def compatible(value):
        if isinstance(value, dict):
            return {key: compatible(item) for key, item in value.items() if key != 'additionalProperties'}
        if isinstance(value, list):
            return [compatible(item) for item in value]
        return value
    return compatible(AdviceReply.model_json_schema())


def intersects(first, second):
    # Half-open intervals: a study block may start exactly when a class ends.
    return first.start < second.end and second.start < first.end


def render_advice(raw, *, schedule, now, timezone, validated_blocks=None):
    """No raw model timetable is shown or saved, including on validation failure."""
    try:
        advice = AdviceReply.model_validate_json(raw)
        if advice.kind == 'message':
            if advice.blocks or not advice.message.strip():
                raise ValueError()
            return advice.message.strip()
        if not advice.blocks:
            raise ValueError()
        intervals = [Interval(start=instant(b.start), end=instant(b.end)) for b in advice.blocks]
        if any(not b.title.strip() or '\n' in b.title or '\r' in b.title for b in advice.blocks):
            raise ValueError()
    except (ValueError, TypeError):
        return 'Не удалось проверить формат плана. Попросите составить план ещё раз.'

    now = instant(now)
    schedule = ClassSchedule.model_validate(schedule) if schedule is not None else ClassSchedule(status='not_connected')
    if schedule.status == 'unavailable' or (schedule.status == 'ready' and
            not timedelta(0) <= now - schedule.fetched_at <= timedelta(hours=24)):
        return 'Не могу проверить план: расписание пар недоступно или устарело. Проверьте период расписания занятий на странице аккаунта и запросите план снова.'
    if any(block.start < now for block in intervals):
        return 'В предложенном плане есть время в прошлом. Запросите новый план — этот вариант не показан.'
    ordered = sorted(intervals, key=lambda block: block.start)
    if any(intersects(first, second) for first, second in zip(ordered, ordered[1:])):
        return 'В предложенном плане учебные блоки пересекаются между собой. Запросите новый план — этот вариант не показан.'
    if schedule.status == 'ready':
        if any(block.start < schedule.coverage.start or block.end > schedule.coverage.end for block in intervals):
            return 'План выходит за период загруженного расписания пар. Укажите более близкие даты или измените период расписания на странице аккаунта.'
        if any(intersects(block, lesson) for block in intervals for lesson in schedule.busy):
            return 'В предложенном плане есть пересечение с парой. Запросите новый план — конфликтующий вариант не показан.'
        header = 'План проверен: пересечений с загруженными парами и между учебными блоками нет.'
    else:
        header = 'Черновик плана. Расписание занятий не заполнено: пересечения с парами не проверены.'
    if validated_blocks is not None:
        validated_blocks.extend([b.model_dump() for b in advice.blocks])
    zone = ZoneInfo(timezone)
    lines = [header, 'Время: ' + timezone + '.']
    for block, interval in sorted(zip(advice.blocks, intervals), key=lambda item: item[1].start):
        start, end = interval.start.astimezone(zone), interval.end.astimezone(zone)
        lines.append(f'{start:%d.%m.%Y %H:%M} — {end:%d.%m.%Y %H:%M} · {block.title.strip()}')
    lines.append('Проверьте, что эти часы подходят для самостоятельной работы. План остаётся рекомендацией в чате.')
    return '\n\n'.join(lines)
