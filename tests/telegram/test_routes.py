import asyncio
from urllib.parse import parse_qs, urlsplit

import httpx

from campuspulse_recovery.cloud.telegram_reminders import TelegramReminders
from campuspulse_recovery.lms.multiuser import create_multiuser_app
from campuspulse_recovery.storage.sqlite_documents import SqliteDocuments
from tests.lms.test_multiuser import ORIGIN, Verifier, client, login, post, fetch
from .test_reminders import Bot


def test_settings_owner_is_server_derived_and_mutations_require_csrf(tmp_path):
    async def scenario():
        docs = SqliteDocuments(tmp_path, project_id='campuspulse-demo', database_id='(default)')
        service = TelegramReminders(docs, bot=Bot(), username='CampusPulseTestBot', webhook_secret='s' * 43)
        app = create_multiuser_app(origin=ORIGIN, client_id='test.apps.googleusercontent.com',
            directory=tmp_path, documents=docs, telegram=service, test_verifier=Verifier(),
            transport=httpx.MockTransport(fetch))
        async with client(app) as alice, client(app) as bob, client(app) as anonymous:
            assert (await anonymous.get('/api/account/telegram')).status_code == 403
            await login(alice, 'alice')
            await login(bob, 'bob')
            assert (await alice.post('/api/account/telegram', json={'action': 'connect'})).status_code == 403
            assert (await post(alice, '/api/account/telegram', {'action': 'connect', 'owner': 'bob'})).status_code == 400
            assert (await alice.get('/api/account/telegram?owner=bob')).status_code == 400
            link = await post(alice, '/api/account/telegram', {'action': 'connect'})
            assert link.status_code == 200, link.text
            nonce = parse_qs(urlsplit(link.json()['url']).query)['start'][0]
            await service.webhook({'message': {'chat': {'type': 'private', 'id': 123},
                'from': {'id': 123, 'username': 'student_123'}, 'text': '/start ' + nonce}})
            assert (await alice.get('/api/account/telegram')).json()['confirmation_required']
            assert not (await bob.get('/api/account/telegram')).json()['pending']
            assert (await post(bob, '/api/account/telegram', {'action': 'confirm'})).status_code == 409
            result = await post(alice, '/api/account/telegram', {'action': 'confirm'})
            assert result.status_code == 200 and result.json()['state']['enabled'] is True
            assert (await post(alice, '/api/account/telegram', {'action': 'enabled', 'enabled': 'false'})).status_code == 400
            result = await post(alice, '/api/account/telegram', {'action': 'enabled', 'enabled': False})
            assert result.status_code == 200 and result.json()['state']['enabled'] is False
            assert not (await bob.get('/api/account/telegram')).json()['connected']
            page = await alice.get('/settings')
            assert 'id="telegram-enabled"' in page.text and 'telegram-settings.js' in page.text
            script = await alice.get('/assets/telegram-settings.js')
            assert script.status_code == 200
            assert "'unsafe-inline'" not in page.headers['content-security-policy']
    asyncio.run(scenario())


def test_cloud_webhook_and_scheduled_endpoint_reject_unauthorized_requests(tmp_path, monkeypatch):
    async def scenario():
        from campuspulse_recovery.cloud.calendar_app import CalendarSettings, create_calendar_cloud_app
        from campuspulse_recovery.demo.adapters import demo_config
        from campuspulse_recovery.cloud.authority import ServiceIdentity
        from campuspulse_recovery.security.context import AccessDenied
        values = demo_config().model_dump()
        values['mode'] = 'cloud'
        settings = CalendarSettings(application=values, origin=ORIGIN,
            google_client_id='test.apps.googleusercontent.com', chat={'provider': 'gemini_api', 'model': 'gemini-flash-latest'},
            scheduler_service_account='demo-scheduler@campuspulse-demo.iam.gserviceaccount.com')
        docs = SqliteDocuments(tmp_path, project_id='campuspulse-demo', database_id='(default)')
        app = create_calendar_cloud_app(settings, docs)
        async with client(app) as browser:
            assert (await browser.post('/telegram/webhook', json={})).status_code == 403
            assert (await browser.post('/telegram/webhook', headers={'X-Telegram-Bot-Api-Secret-Token': 'wrong'}, json={})).status_code == 403
            assert (await browser.post('/internal/telegram/remind', json={})).status_code == 403
            async def verified(self, request):
                assert self.principal == settings.scheduler_service_account
            monkeypatch.setattr(ServiceIdentity, 'verify', verified)
            result = await browser.post('/internal/telegram/remind', json={})
            assert result.status_code == 200 and result.json()['configured'] is False
    asyncio.run(scenario())
