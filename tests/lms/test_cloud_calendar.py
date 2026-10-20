import asyncio
from datetime import UTC, datetime, timedelta
from functools import wraps

import httpx
import pytest

from campuspulse_recovery.cloud.calendar_feed import CloudCalendarFeed
from campuspulse_recovery.storage.sqlite_documents import SqliteDocuments


def run_async(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return run


URL = ('https://lms.astanait.edu.kz/calendar/export_execute.php?userid=42&authtoken='
       + 'a' * 32 + '&preset_what=all&preset_time=recentupcoming')
ICS = b'BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:task\r\nSUMMARY:Homework\r\nDTSTART:20300101T120000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n'


@run_async
async def test_cloud_feed_restart_cooldown_status_isolation_and_failed_refresh(tmp_path):
    docs = SqliteDocuments(tmp_path, project_id='test-project', database_id='(default)')
    now = datetime.now(UTC)
    requests = []
    available = True

    def fetch(request):
        requests.append(request)
        return httpx.Response(200 if available else 503, content=ICS)

    def feed(owner='alice'):
        return CloudCalendarFeed(documents=docs, url=URL, owner_id=owner, user_id=42,
            transport=httpx.MockTransport(fetch), clock=lambda: now)

    first = feed()
    await first.refresh()
    assert first.state().snapshot.tasks[0].title == 'Homework'
    await first.set_local_status('task', True)
    restarted = feed()
    await restarted.refresh()
    assert len(requests) == 1  # cooldown survives process replacement
    assert restarted.state().snapshot.tasks[0].local_status == 'done'
    other = feed('bob')
    await other.refresh()
    assert other.state().snapshot.tasks[0].local_status == 'active'
    available = False
    now += timedelta(minutes=31)
    await restarted.refresh()
    state = restarted.state()
    assert state.stale and state.error == 'unavailable'
    assert state.snapshot.tasks[0].local_status == 'done'
    again = feed()
    await again.load()
    assert again.state().snapshot.fetched_at == first.state().snapshot.fetched_at
    assert again.state().error == 'unavailable'
    with pytest.raises(ValueError):
        await again.set_local_status('unknown', True)


@run_async
async def test_distributed_refresh_claim_allows_one_fetch(tmp_path):
    docs = SqliteDocuments(tmp_path, project_id='test-project', database_id='(default)')
    requests = []

    async def fetch(request):
        requests.append(request)
        await asyncio.sleep(0)
        return httpx.Response(200, content=ICS)

    feeds = [CloudCalendarFeed(documents=docs, url=URL, owner_id='alice', user_id=42,
             transport=httpx.MockTransport(fetch)) for _ in range(2)]
    await asyncio.gather(*(f.refresh() for f in feeds))
    assert len(requests) == 1
    await feeds[1].load()
    assert feeds[1].state().snapshot is not None


@run_async
async def test_cloud_composition_requires_google_auth_and_recovers_owner_state(tmp_path, monkeypatch):
    from campuspulse_recovery.cloud.calendar_app import CalendarSettings, create_calendar_cloud_app
    from campuspulse_recovery.demo.adapters import demo_config
    from campuspulse_recovery.web.auth import GoogleSignInVerifier
    from tests.lms.test_multiuser import Verifier, client, login, post

    # Patch only the official verifier method in the test process. The deployed
    # factory has no setting that permits a fake verifier or local auth.
    monkeypatch.setattr(GoogleSignInVerifier, 'verify', Verifier.verify)
    values = demo_config().model_dump()
    values['mode'] = 'cloud'
    settings = CalendarSettings(application=values, origin='https://127.0.0.1:8961',
        google_client_id='test.apps.googleusercontent.com', chat={'provider': 'gemini_api', 'model': 'gemini-flash-latest'},
        scheduler_service_account='demo-scheduler@campuspulse-demo.iam.gserviceaccount.com')
    docs = SqliteDocuments(tmp_path, project_id='campuspulse-demo', database_id='(default)')
    import campuspulse_recovery.cloud.calendar_app as module
    original = module.CloudCalendarFeed
    monkeypatch.setattr(module, 'CloudCalendarFeed', lambda **kwargs:
        original(**{**kwargs, 'transport': httpx.MockTransport(lambda request: httpx.Response(200,
            content=ICS.replace(b'Homework', ('Homework ' + request.url.params['userid']).encode())))}))
    app = create_calendar_cloud_app(settings, docs)
    async with client(app) as user:
        assert (await user.get('/health')).status_code == 200
        assert (await user.post('/internal/calendar/sync')).status_code == 403
        assert (await user.get('/api/moodle')).status_code == 403
        await login(user, 'alice')
        assert (await post(user, '/api/account/moodle', {'url': URL})).status_code == 200
        assert (await post(user, '/api/moodle/task-status', {'task_id': 'task', 'done': True})).status_code == 200
    restarted = create_calendar_cloud_app(settings, docs)
    async with client(restarted) as user:
        await login(user, 'alice')
        assert (await user.get('/api/moodle')).json()['snapshot']['tasks'][0]['local_status'] == 'done'
        # A second Cloud Run replica can replace the connection. The first
        # replica must reload its binding rather than displaying the old feed.
        assert (await post(user, '/api/account/moodle', {'url': URL.replace('userid=42', 'userid=43')})).status_code == 200
    async with client(app) as user:
        await login(user, 'alice')
        task = (await user.get('/api/moodle')).json()['snapshot']['tasks'][0]
        assert task['title'] == 'Homework 43'
        assert task['local_status'] == 'active'
