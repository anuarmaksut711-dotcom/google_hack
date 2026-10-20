"""Private Cloud Run Job smoke check. No test routes or auth bypass in the web app."""
import asyncio
from datetime import UTC, datetime, timedelta
import json
import logging
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from google.cloud.firestore import AsyncClient

from ..storage.plans import FirestoreDocuments
from ..domain.chat_plan import ClassSchedule, render_advice
from ..domain.models import Interval
from .calendar_app import CalendarConversation, CalendarSettings
from .calendar_feed import CloudCalendarFeed


async def main():
    logging.disable(logging.CRITICAL)
    settings = CalendarSettings.model_validate_json(Path(os.environ['CAMPUSPULSE_CALENDAR_SETTINGS']).read_text())
    client = AsyncClient(project=settings.application.project_id, database=settings.application.firestore_database)
    documents = FirestoreDocuments(client, project_id=settings.application.project_id,
                                   database_id=settings.application.firestore_database)
    try:
        accounts = await documents.scan('cp_moodle_accounts')
        task_count = 0
        for account in accounts:
            user = int(parse_qs(urlsplit(account['url']).query)['userid'][0])
            feed = CloudCalendarFeed(documents=documents, url=account['url'], owner_id=account['owner_id'], user_id=user)
            await feed.refresh()
            state = feed.state()
            if state.error or state.snapshot is None:
                raise RuntimeError('moodle_smoke_failed')
            task_count += len(state.snapshot.tasks)
        now = datetime.now(UTC)
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        lesson = Interval(start=tomorrow + timedelta(hours=7), end=tomorrow + timedelta(hours=9))
        available = Interval(start=lesson.end, end=lesson.end + timedelta(hours=1))
        schedule = ClassSchedule(status='ready', fetched_at=now,
            coverage=Interval(start=tomorrow, end=tomorrow + timedelta(days=1)), busy=(lesson,))
        reply = await CalendarConversation(settings.chat).reply(history=(),
            message=('Составь план на завтра: одно задание TEST Algorithms, нужно ровно 60 минут. '
                     f'Я свободен с {available.start.isoformat()} до {available.end.isoformat()}. '
                     'Задание ещё не выполнено, других обязательств нет. Нужен конкретный блок с датой '
                     'и временем, уточнения не нужны. Используй kind=plan.'),
            user_id='deployment-check', session_id='deployment-check',
            sources={'assignments': [f'TEST Algorithms: 60 минут, срок {schedule.coverage.end.isoformat()}.'],
                     'calendar': [f'Сейчас {now.isoformat()}. Все данные в этой проверке синтетические.'],
                     'class_schedule': schedule.model_dump(mode='json')})
        checked = render_advice(reply, schedule=schedule, now=now, timezone='Asia/Almaty')
        if not checked.startswith('План проверен:'):
            raise RuntimeError('gemini_smoke_failed')
        print(json.dumps({'status': 'ok', 'accounts': len(accounts), 'tasks': task_count,
                          'gemini': 'structured_plan_checked'}))
    finally:
        client.close()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception:
        raise SystemExit('cloud_smoke_failed') from None
