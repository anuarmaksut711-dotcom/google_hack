"""Owner-bound weekly classes, persisted with the application's document store."""
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator, model_validator

from ..domain.chat_plan import ClassSchedule
from ..domain.models import Contract, Interval
from ..security.context import AccessDenied


class WeeklyClass(Contract):
    subject: str = Field(min_length=1, max_length=120)
    weekday: int = Field(ge=0, le=6)
    start: str = Field(pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    end: str = Field(pattern=r'^([01]\d|2[0-3]):[0-5]\d$')

    @field_validator('subject')
    @classmethod
    def valid_subject(cls, value):
        if not value.strip():
            raise ValueError('subject_required')
        return value.strip()

    @model_validator(mode='after')
    def valid_class(self):
        if self.end <= self.start:
            raise ValueError('invalid_class_time_or_subject')
        return self


class WeeklySchedule(Contract):
    valid_from: date
    valid_until: date
    classes: tuple[WeeklyClass, ...] = Field(max_length=30)

    @model_validator(mode='after')
    def valid_schedule(self):
        if not 0 <= (self.valid_until - self.valid_from).days <= 90:
            raise ValueError('schedule_period_max_91_days')
        ordered = sorted(self.classes, key=lambda c: (c.weekday, c.start))
        if any(a.weekday == b.weekday and b.start < a.end for a, b in zip(ordered, ordered[1:])):
            raise ValueError('classes_overlap')
        return self


class ManualSchedule:
    def __init__(self, documents, policy, *, clock=lambda: datetime.now(UTC)):
        self.documents, self.policy, self.clock = documents, policy, clock
        self.owner = policy.config.student.trusted_user_id
        self.record = 'cp_class_schedules/' + self.owner
        self.zone = ZoneInfo(policy.config.student.timezone)

    def authorize(self, context):
        self.policy.require_context(context)
        self.policy.require_owner(context, self.owner)

    async def read(self, context):
        self.authorize(context)
        saved = await self.documents.get(self.record)
        if saved is None:
            return {'connected': False, 'schedule': None}
        if saved['owner_id'] != self.owner:
            raise AccessDenied('class_schedule_owner_mismatch')
        schedule = WeeklySchedule.model_validate(saved['schedule'])
        return {'connected': True, 'schedule': schedule.model_dump(mode='json')}

    async def save(self, context, schedule):
        self.authorize(context)
        async def write(tx):
            tx.set(self.record, {'owner_id': self.owner, 'schedule': schedule.model_dump(mode='json')})
        await self.documents.transact(write)
        return await self.read(context)

    def planning_schedule(self, state):
        if not state['connected']:
            return ClassSchedule(status='not_connected')
        weekly = WeeklySchedule.model_validate(state['schedule'])
        start = datetime.combine(weekly.valid_from, time.min, self.zone)
        end = datetime.combine(weekly.valid_until + timedelta(days=1), time.min, self.zone)
        if not start <= self.clock() < end:
            return ClassSchedule(status='unavailable')
        busy = []
        day = weekly.valid_from
        while day <= weekly.valid_until:
            for lesson in weekly.classes:
                if day.weekday() == lesson.weekday:
                    busy.append(Interval(start=datetime.combine(day, time.fromisoformat(lesson.start), self.zone),
                                         end=datetime.combine(day, time.fromisoformat(lesson.end), self.zone)))
            day += timedelta(days=1)
        return ClassSchedule(status='ready', coverage=Interval(start=start, end=end),
                             fetched_at=self.clock(), busy=tuple(busy))

    async def calendar_context(self, context):
        state = await self.read(context)
        schedule = self.planning_schedule(state)
        if schedule.status == 'not_connected':
            return ('Расписание занятий не заполнено. Предложи ввести его на странице /account.',), schedule
        if schedule.status == 'unavailable':
            return ('Период ручного расписания не включает сегодня. Попроси изменить даты на странице /account; часы не считай свободными.',), schedule
        weekly = WeeklySchedule.model_validate(state['schedule'])
        days = ('понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота', 'воскресенье')
        lines = [f'Расписание занятий введено пользователем, действует с {weekly.valid_from} по {weekly.valid_until}, часовой пояс {self.zone.key}. Каждая пара — занятый интервал.']
        lines.extend(f'{lesson.subject}: {days[lesson.weekday]}, {lesson.start}–{lesson.end}, каждую неделю.' for lesson in weekly.classes)
        if not weekly.classes:
            lines.append('Пользователь явно сохранил расписание без занятий на этот период.')
        return tuple(lines), schedule
