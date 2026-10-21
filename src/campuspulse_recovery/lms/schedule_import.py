"""A full timetable or one visible day produces a draft, never a saved week."""
from datetime import date
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .manual_schedule import WeeklyClass, WeeklySchedule


MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
MAX_DRAFT_CHARACTERS = 24_000
DOCUMENT_SIGNATURES = {
    'application/pdf': b'%PDF-',
    'image/png': b'\x89PNG\r\n\x1a\n',
    'image/jpeg': b'\xff\xd8\xff',
}
IMPORT_REVIEW_WARNING = ('Проверьте распознанные пары, добавьте остальные дни недели и укажите даты действия. '
    'Перед сохранением подтвердите, что все пары недели добавлены. '
    'Скриншот одного дня не показывает расписание остальных дней.')


class ScheduleImportDraft(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, revalidate_instances='always')

    classes: tuple[WeeklyClass, ...] = Field(min_length=1, max_length=30)
    valid_from: date | None = None
    valid_until: date | None = None

    @field_validator('valid_from', 'valid_until', mode='before')
    @classmethod
    def explicit_date(cls, value):
        if value is not None and type(value) is not date and (
                not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value)):
            raise ValueError('explicit_date_required')
        return value

    @field_validator('classes', mode='before')
    @classmethod
    def explicit_weekdays(cls, values):
        if not isinstance(values, (list, tuple)):
            raise ValueError('weekly_classes_required')
        for value in values:
            weekday = value.weekday if isinstance(value, WeeklyClass) else (
                value.get('weekday') if isinstance(value, dict) else None)
            if type(weekday) is not int:
                raise ValueError('explicit_weekday_required')
        return values

    @model_validator(mode='after')
    def valid_weekly_draft(self):
        # Reuse the existing saved-schedule overlap/period rules. This internal
        # sentinel validates times only; it is never returned as an extracted date.
        WeeklySchedule(valid_from=self.valid_from if self.valid_from and self.valid_until else date(2000, 1, 1),
            valid_until=self.valid_until if self.valid_from and self.valid_until else date(2000, 1, 1),
            classes=self.classes)
        return self

    def wire_value(self):
        return {'classes': [lesson.model_dump(exclude={'schema_version'}) for lesson in self.classes],
            'valid_from': self.valid_from.isoformat() if self.valid_from else None,
            'valid_until': self.valid_until.isoformat() if self.valid_until else None}


def validate_document(document: bytes, mime_type: str):
    if mime_type not in DOCUMENT_SIGNATURES:
        raise ValueError('unsupported_schedule_document')
    if not document or len(document) > MAX_DOCUMENT_BYTES or not document.startswith(DOCUMENT_SIGNATURES[mime_type]):
        raise ValueError('invalid_schedule_document')
