"""Cloud Run composition of the working Google/Moodle/Gemini application."""
import asyncio
import logging
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from ..agent.conversation import AdkConversationAgent, ConversationTarget, require_chat_credentials
from ..config import AppConfig, ServiceAccount
from ..domain.models import Contract
from ..lms.multiuser import create_multiuser_app
from ..security.context import AccessDenied
from .authority import ServiceIdentity
from .calendar_feed import CloudCalendarFeed


class CalendarSettings(Contract):
    application: AppConfig
    origin: str
    google_client_id: str
    chat: ConversationTarget
    scheduler_service_account: ServiceAccount


class CalendarConversation:
    local_preview_only = False

    def __init__(self, target):
        self.agent = AdkConversationAgent(target, profile='calendar_advice')

    async def reply(self, **kwargs):
        return await self.agent.reply(**kwargs)


def create_calendar_cloud_app(settings, documents):
    settings.application.require_environment('cloud')
    if settings.application.mode != 'cloud':
        raise ValueError('cloud_configuration_required')
    if not settings.scheduler_service_account.endswith('@' + settings.application.project_id + '.iam.gserviceaccount.com'):
        raise ValueError('scheduler_project_mismatch')
    factory = lambda **kwargs: CloudCalendarFeed(documents=documents, **kwargs)
    app = create_multiuser_app(origin=settings.origin, client_id=settings.google_client_id,
        directory=Path('/tmp/campuspulse'), application=settings.application, documents=documents,
        feed_factory=factory, agent_factory=lambda: CalendarConversation(settings.chat))

    # These routes must precede the multi-user catch-all mount.
    mount = app.router.routes.pop()

    @app.get('/health')
    async def health():
        await documents.get('cp_system/health')
        return {'status': 'ok', 'storage': 'firestore', 'chat': settings.chat.model}

    @app.get('/favicon.ico')
    async def favicon():
        return Response(status_code=204)

    @app.post('/internal/calendar/sync')
    async def sync(request: Request):
        try:
            await ServiceIdentity(settings.origin, settings.scheduler_service_account).verify(request)
        except AccessDenied:
            return JSONResponse({'error': 'service_identity_required'}, status_code=403)
        records = await documents.scan('cp_moodle_accounts')
        semaphore = asyncio.Semaphore(3)

        async def update(record):
            async with semaphore:
                try:
                    user = int(parse_qs(urlsplit(record['url']).query)['userid'][0])
                    feed = factory(url=record['url'], owner_id=record['owner_id'], user_id=user)
                    await feed.refresh()
                    return feed.state().snapshot is not None and feed.state().error is None
                except Exception:
                    return False

        results = await asyncio.gather(*(update(record) for record in records))
        failed = results.count(False)
        return JSONResponse({'accounts': len(results), 'updated': results.count(True), 'failed': failed},
                            status_code=503 if failed else 200)

    app.router.routes.append(mount)
    return app


def main():
    if not os.environ.get('K_SERVICE') or os.environ.get('FIRESTORE_EMULATOR_HOST') or os.environ.get('GOOGLE_APPLICATION_CREDENTIALS'):
        raise SystemExit('cloud_platform_required')
    settings = CalendarSettings.model_validate_json(Path(os.environ['CAMPUSPULSE_CALENDAR_SETTINGS']).read_text())
    require_chat_credentials(settings.chat)
    from google.cloud.firestore import AsyncClient
    from ..storage.plans import FirestoreDocuments
    # Construct the async Firestore client inside the server's event loop.
    import uvicorn

    async def serve():
        client = AsyncClient(project=settings.application.project_id,
                             database=settings.application.firestore_database)
        documents = FirestoreDocuments(client, project_id=settings.application.project_id,
                                        database_id=settings.application.firestore_database)
        app = create_calendar_cloud_app(settings, documents)
        logging.disable(logging.CRITICAL)
        try:
            await uvicorn.Server(uvicorn.Config(app, host='0.0.0.0', port=int(os.environ.get('PORT', '8080')),
                proxy_headers=True, forwarded_allow_ips='*', access_log=False, log_level='critical',
                timeout_graceful_shutdown=120)).serve()
        finally:
            client.close()

    asyncio.run(serve())


if __name__ == '__main__':
    main()
