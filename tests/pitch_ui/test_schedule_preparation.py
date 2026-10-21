"""The preparation page reuses the authenticated timetable in either deployment."""
import asyncio

from campuspulse_recovery.lms.multiuser import create_multiuser_app
from tests.lms.test_multiuser import ORIGIN, Verifier, client, login
from tests.lms.test_recovery_website import application as recovery_application, client as recovery_client


def test_classes_preparation_available_without_recovery_but_no_broken_planning_requests(tmp_path):
    async def scenario():
        app = create_multiuser_app(origin=ORIGIN, client_id='test.apps.googleusercontent.com',
            directory=tmp_path, test_verifier=Verifier())
        async with client(app) as browser, client(app) as anonymous:
            assert (await anonymous.get('/planning')).status_code in (307, 403)
            await login(browser, 'alice')
            page = await browser.get('/planning')
            assert page.status_code == 200
            assert 'id="class-schedule"' in page.text
            assert 'id="effort-form"' not in page.text
            assert 'сейчас недоступн' in page.text
            assert '/assets/recovery-planning.js' not in page.text
            assert '/assets/planning-classes.js' in page.text
            assert "script-src 'self'" in page.headers['content-security-policy']
            assert (await browser.get('/planning?owner=bob')).status_code == 400
            assert (await browser.get('/assets/planning-classes.js')).status_code == 200
            assert (await anonymous.get('/assets/planning-classes.js')).status_code == 403
    asyncio.run(scenario())


def test_recovery_preparation_includes_existing_class_editor_and_planning_forms(tmp_path, monkeypatch):
    async def scenario():
        app, _ = await recovery_application(tmp_path, monkeypatch)
        async with recovery_client(app) as browser:
            await login(browser, 'alice')
            page = await browser.get('/planning')
            assert page.status_code == 200
            assert 'id="class-schedule"' in page.text
            assert 'id="effort-form"' in page.text
            assert 'id="windows-form"' in page.text
            assert 'Предложенное расписание' in page.text
            assert page.text.count('id="class-schedule"') == 1
            assert '/assets/planning-classes.js' in page.text
    asyncio.run(scenario())


def test_upload_is_wired_to_selected_account_agent_and_never_saves_draft(tmp_path):
    class Importer:
        def __init__(self):
            self.calls = []

        async def extract_classes(self, **arguments):
            self.calls.append(arguments)
            return {'classes': [{'subject': 'Алгебра', 'weekday': 0, 'start': '09:00', 'end': '10:20'}],
                'valid_from': None, 'valid_until': None}

    async def scenario():
        agents = []
        def factory():
            agent = Importer()
            agents.append(agent)
            return agent
        app = create_multiuser_app(origin=ORIGIN, client_id='test.apps.googleusercontent.com',
            directory=tmp_path, test_verifier=Verifier(), agent_factory=factory)
        async with client(app) as alice, client(app) as bob:
            token = await login(alice, 'alice')
            await login(bob, 'bob')
            result = await alice.post('/api/account/classes/import', content=b'%PDF-1.7\nsynthetic',
                headers={'X-CSRF-Token': token, 'Content-Type': 'application/pdf'})
            assert result.status_code == 200, result.text
            assert result.json()['draft']['classes'][0]['subject'] == 'Алгебра'
            assert len(agents) == 2
            assert len(agents[0].calls) == 1
            assert agents[1].calls == []
            assert (await alice.get('/api/account/classes')).json() == {'connected': False, 'schedule': None}
            assert (await bob.get('/api/account/classes')).json() == {'connected': False, 'schedule': None}
    asyncio.run(scenario())


def test_published_composition_delegates_upload_to_its_calendar_conversation(tmp_path, monkeypatch):
    calls = []
    async def extract(agent, **arguments):
        calls.append(arguments)
        return {'classes': [{'subject': 'Алгебра', 'weekday': 0, 'start': '09:00', 'end': '10:20'}],
                'valid_from': None, 'valid_until': None}
    monkeypatch.setattr('campuspulse_recovery.agent.conversation.AdkConversationAgent.extract_classes', extract)
    async def scenario():
        app, _ = await recovery_application(tmp_path, monkeypatch)
        async with recovery_client(app) as browser:
            token = await login(browser, 'alice')
            response = await browser.post('/api/account/classes/import', content=b'%PDF-1.7\nsynthetic',
                headers={'X-CSRF-Token': token, 'Content-Type': 'application/pdf'})
            assert response.status_code == 200, response.text
            assert len(calls) == 1
            assert (await browser.get('/api/account/classes')).json()['connected'] is False
    asyncio.run(scenario())
