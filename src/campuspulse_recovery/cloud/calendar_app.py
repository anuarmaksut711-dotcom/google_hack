"""Cloud Run composition of the working Google/Moodle/Gemini application."""
import asyncio
import logging
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from pydantic import model_validator

from ..agent.conversation import AdkConversationAgent, ConversationTarget, require_chat_credentials
from ..config import AppConfig, ServiceAccount
from ..domain.models import Contract
from ..lms.multiuser import create_multiuser_app
from ..security.context import AccessDenied
from .authority import ServiceIdentity, utcnow
from .calendar_feed import CloudCalendarFeed
from .recovery import CalendarRecoverySettings


class CalendarSettings(Contract):
    application: AppConfig
    origin: str
    google_client_id: str
    chat: ConversationTarget
    scheduler_service_account: ServiceAccount
    recovery: CalendarRecoverySettings | None = None

    @model_validator(mode='after')
    def matching_recovery(self):
        if self.recovery is not None:
            cfg = self.recovery.settings
            if ((cfg.application.project_id, cfg.application.firestore_database) !=
                    (self.application.project_id, self.application.firestore_database)
                    or cfg.backend_origin != self.origin
                    or cfg.google_signin_client_id != self.google_client_id):
                raise ValueError('matching_recovery_application_required')
        return self


class CalendarConversation:
    local_preview_only = False

    def __init__(self, target, *, recovery=False):
        self.agent = AdkConversationAgent(target, profile='recovery' if recovery else 'calendar_advice')

    async def reply(self, **kwargs):
        return await self.agent.reply(**kwargs)

    async def suggest_title(self, **kwargs):
        return await self.agent.suggest_title(**kwargs)

    async def transcribe(self, **kwargs):
        return await self.agent.transcribe(**kwargs)


def create_calendar_cloud_app(settings, documents, *, clock=utcnow):
    settings.application.require_environment('cloud')
    if settings.application.mode != 'cloud':
        raise ValueError('cloud_configuration_required')
    if not settings.scheduler_service_account.endswith('@' + settings.application.project_id + '.iam.gserviceaccount.com'):
        raise ValueError('scheduler_project_mismatch')
    factory = lambda **kwargs: CloudCalendarFeed(documents=documents, clock=clock, **kwargs)
    google_calendar = None
    if os.environ.get('CAMPUSPULSE_CALENDAR_OAUTH'):
        import json
        from .connected_calendar import CloudConnectedCalendar
        config = json.loads(Path(os.environ['CAMPUSPULSE_CALENDAR_OAUTH']).read_text())
        if (config['project_id'] != settings.application.project_id or config['origin'] != settings.origin
                or config['database'] != settings.application.firestore_database):
            raise ValueError('calendar_oauth_deployment_mismatch')
        google_calendar = CloudConnectedCalendar(documents, client=config['client'], key=config['key'],
            project_id=config['project_id'], origin=config['origin'], mcp_origin=config['mcp_origin'], clock=clock)
    app = create_multiuser_app(origin=settings.origin, client_id=settings.google_client_id,
        directory=Path('/tmp/campuspulse'), application=settings.application, documents=documents,
        feed_factory=factory, agent_factory=lambda: CalendarConversation(settings.chat), clock=clock,
        recovery_factory=(None if settings.recovery is None else
            lambda principal, clock: settings.recovery.backend(principal, documents, clock=clock)),
        recovery_chat_factory=lambda: CalendarConversation(settings.chat, recovery=True),
        cloud_google_calendar=google_calendar)

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
