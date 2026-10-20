"""Owner-authorized minimal deadline context for conversation; no feed credentials."""
from zoneinfo import ZoneInfo

from ..web.views.app import SafeSources


class CalendarChatProjection:
    def __init__(self, feed, policy):
        self.feed, self.policy = feed, policy

    async def project(self, context, *, run, plan):
        self.policy.require_context(context)
        self.policy.require_owner(context, self.feed.owner_id)
        # Refresh before every model invocation; CalendarFeed enforces its shared
        # cooldown and preserves the last successful snapshot on transport errors.
        await self.feed.refresh()
        state = self.feed.state()
        timezone = self.policy.config.student.timezone
        zone = ZoneInfo(timezone)
        now = self.feed.clock().astimezone(zone)
        calendar = [f'Сейчас {now.isoformat()}. Часовой пояс: {timezone}.',
            'Личные свободные часы неизвестны. Уточни доступное время.',
            'План составляется в чате; запись в календарь не подключена.']
        if state.snapshot is None:
            return SafeSources(assignments=('Данные Moodle недоступны. Попроси обновить задания.',),
                calendar=tuple(calendar))
        snapshot = state.snapshot
        notice = f'Moodle: календарный экспорт; получено {snapshot.fetched_at.astimezone(zone).isoformat()}. '
        notice += 'Статусы сдачи, оценки и оставшееся время неизвестны. '
        if state.error or state.stale:
            notice += 'Внимание: данные могли устареть; обновление не подтверждено. '
        tasks = sorted((task for task in snapshot.tasks if task.local_status != 'done'),
            key=lambda task: (task.due_at, task.id))
        done_count = sum(task.local_status == 'done' for task in snapshot.tasks)
        if done_count:
            notice += f'Пользователь отметил выполненными {done_count} работ в CampusPulse; они исключены из планирования. '
        # The shared display contract allows 100 lines. Explicitly report the
        # bound instead of suggesting a partial source is the full workload.
        if len(tasks) > 99:
            notice += f'Показаны первые 99 из {len(tasks)} событий; список ограничен. '
        else:
            notice += f'Переданы все {len(tasks)} события текущего экспорта. '
        notice += 'Экспорт ограничен выбранным в Moodle периодом.'
        lines = [notice[:500]]
        for task in tasks[:99]:
            subject = task.course_title.split('|')[0].strip()
            due = task.due_at.astimezone(zone).isoformat()
            # Keep identifiers, links, descriptions (which may contain personal
            # data), attendance, tokens and teacher names out of model context.
            label = f'{subject}: {task.title}'
            lines.append(f'{label[:360]} · срок {due} · статус сдачи неизвестен')
        return SafeSources(assignments=tuple(lines), calendar=tuple(calendar))
