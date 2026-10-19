"""Offline API -> durable sync -> authenticated UI integration, without cloud IO.

The actual client, sync coordinator, cloud backend read methods and UI routes
are composed here. MockTransport supplies Moodle responses and FakeDocuments
simulates persistent storage; these tests do not verify deployed IAM or AITU.
"""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx

from campuspulse_recovery.cloud.backend import CloudBackend
from campuspulse_recovery.lms.client import MoodleClient
from campuspulse_recovery.lms.sync import MoodleSync
from campuspulse_recovery.web.moodle import install_moodle_routes
from campuspulse_recovery.web.views.app import create_ui_app
from tests.lms.test_client import SITE, TOKEN, USER, fixtures
from tests.storage.test_plans import FakeDocuments
from tests.web.test_approval import ORIGIN, Setup, login


class Integration:
    def __init__(self, monkeypatch, documents=None, clock=None):
        self.h = Setup(documents=documents)
        if clock is not None:
            self.h.clock = clock
        self.payloads, self.requests = fixtures(), []

        clock = self.h.clock

        class ClientDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock().astimezone(tz) if tz is not None else clock().replace(tzinfo=None)

        monkeypatch.setattr('campuspulse_recovery.lms.client.datetime', ClientDateTime)

        def respond(request):
            body = parse_qs(request.content.decode())
            assert request.method == 'POST' and not request.url.query
            assert body['wstoken'] == [TOKEN]
            self.requests.append(body['wsfunction'][0])
            return httpx.Response(200, json=deepcopy(self.payloads[body['wsfunction'][0]]))

        self.client = MoodleClient(SITE, TOKEN, USER, transport=httpx.MockTransport(respond))
        self.sync = self.writer()
        settings = SimpleNamespace(agent_origin='https://agent-test.europe-west1.run.app', moodle=SimpleNamespace(
            site_url=SITE, expected_user_id=USER, interval_minutes=15,
            stale_after_minutes=30, sync_origin='https://sync-test.europe-west1.run.app'))
        self.backend = CloudBackend(settings, self.h.policy, self.h.documents)
        # Explicit deterministic clock injection; the production cache reader is
        # otherwise the real backend's instance, with no Moodle token or reader.
        self.backend.moodle.clock = self.h.clock
        self.backend.moodle_worker = SimpleNamespace(refresh=self.force_refresh)

    def writer(self):
        return MoodleSync(self.h.documents, owner_id=self.h.config.student.trusted_user_id,
            site_url=SITE, expected_user_id=USER, fetch_snapshot=self.client.fetch,
            interval_minutes=15, stale_after_minutes=30, clock=self.h.clock)

    async def force_refresh(self):
        return await self.sync.refresh(force=True)

    def app(self):
        app = create_ui_app(policy=self.h.policy, documents=self.h.documents,
            verifier=self.h.verifier, origin=ORIGIN, environment='local',
            clock=self.h.clock, projection=self.backend, moodle_enabled=True)
        install_moodle_routes(app, auth=app.state.web_auth,
            reader=self.backend.moodle_read, refresh=self.backend.moodle_refresh)
        return app


def publish_score(harness, value, display, *, total=82):
    grades = harness.payloads['gradereport_user_get_grade_items']['usergrades'][0]['gradeitems']
    grades[0].update(graderaw=total, gradeformatted=f'{total:.2f}')
    grades[1].update(graderaw=value, gradeformatted=display)


def test_background_refresh_without_browser_survives_restart_and_next_signin_sees_scores(monkeypatch, tmp_path):
    async def scenario():
        background = Integration(monkeypatch)
        first = await background.sync.refresh()
        assert first.snapshot.tasks[0].grade_value == 0
        assert not any(path.startswith('cp_web_') for path in background.h.documents.data)

        # Simulate a later scheduler dispatch and a fresh worker instance. There
        # is still no browser, UI server, cookie, Google Sheet or Gemini call.
        background.h.clock.value += timedelta(minutes=15)
        publish_score(background, 87.5, '87.50')
        current = await background.writer().refresh()
        assert current.snapshot.tasks[0].grade_value == 87.5
        assert current.snapshot.courses[0].grade_value == 82
        assert current.snapshot.fetched_at == background.h.clock()
        assert not any(path.startswith('cp_web_') for path in background.h.documents.data)
        assert TOKEN not in repr(background.h.documents.data)

        checkpoint = tmp_path / 'durable-offline-documents.bin'
        background.h.documents.checkpoint(checkpoint)
        reopened = Integration(monkeypatch, FakeDocuments.restore(checkpoint), background.h.clock)
        assert reopened.backend.moodle.fetch_snapshot is None
        async with reopened.h.client(reopened.app()) as browser:
            assert (await browser.get('/api/moodle')).status_code == 403
            await login(browser)
            response = await browser.get('/api/moodle')
            assert response.status_code == 200, response.text
            assert response.headers['cache-control'] == 'no-store'
            data = response.json()
            assert data['snapshot']['tasks'][0]['grade_value'] == 87.5
            assert data['snapshot']['tasks'][0]['grade_max'] == 100
            assert data['snapshot']['tasks'][0]['submission_status'] == 'graded'
            assert data['snapshot']['courses'][0]['grade_value'] == 82
            assert data['last_success'] == current.last_success.isoformat()
            assert 'user_id' not in data['snapshot']
            assert not {'owner_id', 'expected_user_id'} & data.keys()
            assert TOKEN not in response.text
            assert (await browser.get('/ui/state')).status_code == 200
            assert 'id="moodle-tasks"' in (await browser.get('/')).text
        # Rendering a saved snapshot never recontacts Moodle or needs its token.
        assert reopened.requests == []

    asyncio.run(scenario())


def test_failed_scheduled_fetch_retains_previous_scores_and_exposes_staleness(monkeypatch, tmp_path):
    async def scenario():
        background = Integration(monkeypatch)
        original = await background.sync.refresh()
        background.h.clock.value += timedelta(minutes=31)
        publish_score(background, 99, '99.00')
        background.payloads['mod_assign_get_submission_status'] = {
            'exception': 'moodle_exception', 'errorcode': 'invalidtoken',
            'message': 'upstream echoed ' + TOKEN}
        failed = await background.writer().refresh()
        assert failed.error == 'auth_failed' and failed.stale
        assert failed.snapshot == original.snapshot

        checkpoint = tmp_path / 'failed-sync-documents.bin'
        background.h.documents.checkpoint(checkpoint)
        reopened = Integration(monkeypatch, FakeDocuments.restore(checkpoint), background.h.clock)
        async with reopened.h.client(reopened.app()) as browser:
            await login(browser)
            response = await browser.get('/api/moodle')
            assert response.status_code == 200
            data = response.json()
            assert data['error'] == 'auth_failed' and data['stale']
            assert data['last_success'] == original.last_success.isoformat()
            assert data['last_attempt'] == background.h.clock().isoformat()
            assert data['snapshot']['tasks'][0]['grade_value'] == 0
            assert data['snapshot']['tasks'][0]['grade_display'] == '0.00'
            assert TOKEN not in response.text
            assert TOKEN not in repr(reopened.h.documents.data)
        assert reopened.requests == []

    asyncio.run(scenario())


def test_authenticated_on_open_refresh_updates_the_same_durable_cache(monkeypatch):
    async def scenario():
        harness = Integration(monkeypatch)
        await harness.sync.refresh()
        harness.h.clock.value += timedelta(minutes=2)
        publish_score(harness, 72, '72.00')
        before = len(harness.requests)
        async with harness.h.client(harness.app()) as browser:
            assert (await browser.post('/api/moodle/sync', json={})).status_code == 403
            assert len(harness.requests) == before
            headers = await login(browser)
            response = await browser.post('/api/moodle/sync', json={}, headers=headers)
            assert response.status_code == 200, response.text
            assert response.json()['snapshot']['tasks'][0]['grade_value'] == 72
            assert len(harness.requests) > before
            assert (await harness.writer().read()).snapshot.tasks[0].grade_value == 72
            # Existing plan machinery is absent in this composition.
            assert (await browser.post('/api/runs', json={}, headers=headers)).status_code == 404

    asyncio.run(scenario())
