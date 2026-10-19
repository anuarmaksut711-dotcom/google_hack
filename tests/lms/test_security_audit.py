"""Adversarial boundary regressions; all credentials and IO are synthetic."""

import asyncio

from campuspulse_recovery.lms.models import MoodleCourse, MoodleSnapshot, MoodleTask
from campuspulse_recovery.storage.plans import MAX_BODY_BYTES
from campuspulse_recovery.web.moodle import install_moodle_routes
from campuspulse_recovery.web.views.app import create_ui_app
from tests.lms.test_sync import Clock, SITE, service, snapshot
from tests.lms.test_web import MoodleFixture
from tests.storage.test_plans import FakeDocuments
from tests.web.test_approval import ORIGIN, login


def test_revocation_during_refresh_prevents_disclosure_of_coursework():
    async def scenario():
        h = MoodleFixture()
        entered, release = asyncio.Event(), asyncio.Event()
        app = create_ui_app(policy=h.policy, documents=h.documents,
            verifier=h.verifier, origin=ORIGIN, environment="local", clock=h.clock,
            moodle_enabled=True)

        async def refresh(context):
            entered.set()
            await release.wait()

        async def read(context):
            return h.value

        install_moodle_routes(app, auth=app.state.web_auth, reader=read, refresh=refresh)
        async with h.client(app) as client:
            headers = await login(client)
            request = asyncio.create_task(client.post('/api/moodle/sync', headers=headers, json={}))
            await entered.wait()
            assert (await client.post('/auth/signout', headers=headers, json={})).status_code == 200
            release.set()
            response = await request
            assert response.status_code == 403
            assert 'snapshot' not in response.json()
            assert 'Математика' not in response.text

    asyncio.run(scenario())


def test_oversized_valid_snapshot_retains_cache_and_releases_lease():
    """An LMS response can fit per-response limits but exceed durable storage."""
    async def scenario():
        documents, clock = FakeDocuments(), Clock()

        async def good():
            return snapshot(clock())

        original = await service(documents, clock, good).refresh()
        clock.advance(minutes=15)
        course = MoodleCourse(id=1, title='Algorithms')
        tasks = tuple(MoodleTask(id=f'assign:{i}', course_id=1, course_title=course.title,
            title=f'Assignment {i}', description='x' * 12_000, submission_status='todo',
            url=f'{SITE}/mod/assign/view.php?id={i}', kind='assignment') for i in range(1, 101))
        large = MoodleSnapshot(site_url=SITE, user_id=42, fetched_at=clock(),
            courses=(course,), tasks=tasks)
        assert len(large.model_dump_json().encode('utf-8')) > MAX_BODY_BYTES

        async def too_large():
            return large

        state = await service(documents, clock, too_large).refresh()
        assert state.error == 'invalid_response'
        assert not state.syncing
        assert state.snapshot == original.snapshot
        assert state.last_success == original.last_success

    asyncio.run(scenario())
