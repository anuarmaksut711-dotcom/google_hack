"""Typed weekly timetable extracted from chat; publication needs human review."""
from datetime import date
import re

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator


class DraftClass(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    subject: str = Field(min_length=1, max_length=120)
    weekday: StrictInt = Field(ge=0, le=6, description='Monday=0 through Sunday=6; only explicitly supplied weekdays')
    start: str = Field(pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    end: str = Field(pattern=r'^([01]\d|2[0-3]):[0-5]\d$')

    @model_validator(mode='after')
    def valid_lesson(self):
        if not self.subject.strip() or '\n' in self.subject or '\r' in self.subject or self.end <= self.start:
            raise ValueError('invalid_class_draft')
        return self


class ChatTimetable(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    classes: tuple[DraftClass, ...] = Field(min_length=1, max_length=30)
    valid_from: date | None = Field(default=None, description='Explicit YYYY-MM-DD supplied by student; null if missing')
    valid_until: date | None = Field(default=None, description='Explicit YYYY-MM-DD supplied by student; null if missing')

    @field_validator('valid_from', 'valid_until', mode='before')
    @classmethod
    def explicit_date(cls, value):
        if value is not None and type(value) is not date and (
                not isinstance(value, str) or re.fullmatch(r'\d{4}-\d{2}-\d{2}', value) is None):
            raise ValueError('explicit_date_required')
        return value

    @model_validator(mode='after')
    def valid_week(self):
        if self.valid_from and self.valid_until and not 0 <= (self.valid_until-self.valid_from).days <= 90:
            raise ValueError('schedule_period_max_91_days')
        ordered = sorted(self.classes, key=lambda item: (item.weekday, item.start))
        if any(a.weekday == b.weekday and b.start < a.end for a, b in zip(ordered, ordered[1:])):
            raise ValueError('classes_overlap')
        return self

    def preview(self, timezone):
        days = ('понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота', 'воскресенье')
        lines = ['Черновик пар из сообщения. Проверьте всю неделю перед сохранением.', 'Время: ' + timezone + '.']
        lines.extend(f'{days[item.weekday]}, {item.start}–{item.end} · {item.subject.strip()}'
                     for item in sorted(self.classes, key=lambda item: (item.weekday, item.start)))
        if self.valid_from and self.valid_until:
            lines.append(f'Повторять каждую неделю с {self.valid_from:%d.%m.%Y} по {self.valid_until:%d.%m.%Y}.')
        else:
            lines.append('Укажите даты действия в редакторе: начало и окончание периода.')
        lines.append('Пары пока не сохранены и не добавлены в Google Calendar.')
        return '\n\n'.join(lines)
