"""Private Cloud Run Job smoke check. No test routes or auth bypass in the web app."""
import asyncio
import json
import logging
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from google.cloud.firestore import AsyncClient

from ..storage.plans import FirestoreDocuments
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
        reply = await CalendarConversation(settings.chat).reply(history=(), message='Ответь одним словом: работает',
            user_id='deployment-check', session_id='deployment-check',
            sources={'assignments': ['Проверка подключения без личных данных.'], 'calendar': []})
        if not isinstance(reply, str) or not reply.strip():
            raise RuntimeError('gemini_smoke_failed')
        print(json.dumps({'status': 'ok', 'accounts': len(accounts), 'tasks': task_count, 'gemini': 'answered'}))
    finally:
        client.close()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception:
        raise SystemExit('cloud_smoke_failed') from None
