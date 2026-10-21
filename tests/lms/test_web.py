"""Offline auth and UI contract tests. No student credentials or LMS requests."""
from datetime import timedelta
import json
import shutil
import subprocess

import pytest

from campuspulse_recovery.lms.models import MoodleCourse, MoodleSnapshot, MoodleTask
from campuspulse_recovery.lms.sync import MoodleState
from campuspulse_recovery.web.assets.client import SCRIPT
from campuspulse_recovery.web.moodle import install_moodle_routes
from campuspulse_recovery.web.views.app import create_ui_app
from campuspulse_recovery.web.views.render import render_shell
from tests.storage.test_plans import NOW, run
from tests.web.test_approval import ORIGIN, Setup, login


SITE = "https://lms.astanait.edu.kz"


def snapshot():
    return MoodleSnapshot(site_url=SITE, user_id=42, fetched_at=NOW,
        courses=(MoodleCourse(id=3, title="Математика", grade_value=0, grade_max=100),),
        tasks=(MoodleTask(id="assign:9", course_id=3, course_title="Математика",
            title='<img src=x onerror="alert(1)">', description="<script>secret()</script>",
            due_at=None, submission_status="submitted", grade_value=0, grade_max=10,
            url=SITE + "/mod/assign/view.php?id=91", kind="assignment"),))


class MoodleFixture(Setup):
    def __init__(self):
        super().__init__()
        self.reads = self.refreshes = 0
        self.after_read = None
        self.value = MoodleState(owner_id=self.config.student.trusted_user_id, site_url=SITE,
            expected_user_id=42, connected=True, stale=False, syncing=False, status="ready",
            last_success=NOW, last_attempt=NOW, snapshot=snapshot())

    def app(self, **changes):
        app = create_ui_app(policy=self.policy, documents=self.documents,
            verifier=self.verifier, origin=ORIGIN, environment="local", clock=self.clock,
            moodle_enabled=True, **changes)

        async def read(context):
            self.policy.require_context(context)
            assert context.trusted.owner_id == self.config.student.trusted_user_id
            self.reads += 1
            if self.after_read:
                self.after_read()
            return self.value

        async def refresh(context):
            self.policy.require_context(context)
            assert context.trusted.owner_id == self.config.student.trusted_user_id
            self.refreshes += 1

        install_moodle_routes(app, auth=app.state.web_auth, reader=read, refresh=refresh)
        return app


def test_cached_get_and_csrf_refresh_are_owner_bound_and_no_store():
    async def scenario():
        h = MoodleFixture()
        async with h.client() as client:
            assert (await client.get('/api/moodle')).status_code == 403
            headers = await login(client)
            data = await client.get('/api/moodle')
            assert data.status_code == 200
            assert h.reads == 1 and h.refreshes == 0
            assert data.headers['cache-control'] == 'no-store'
            payload = data.json()
            assert payload['snapshot']['tasks'][0]['grade_value'] == 0
            assert payload['snapshot']['tasks'][0]['grade_max'] == 10
            assert not {'owner_id', 'expected_user_id'} & payload.keys()
            assert 'user_id' not in payload['snapshot']
            assert (await client.post('/api/moodle/sync', headers=headers, json={})).status_code == 200
            assert h.refreshes == 1 and h.reads == 2
            assert (await client.post('/auth/signout', headers=headers, json={})).status_code == 200
            assert (await client.get('/api/moodle')).status_code == 403
    run(scenario())


@pytest.mark.parametrize('case', ['csrf_missing', 'origin_foreign', 'cross_site', 'anonymous', 'forged_cookie'])
def test_refresh_rejects_unauthorized_requests_before_dispatch(case):
    async def scenario():
        h = MoodleFixture()
        async with h.client() as client:
            headers = await login(client)
            if case == 'csrf_missing': headers.pop('X-CSRF-Token')
            if case == 'origin_foreign': headers['Origin'] = 'https://evil.test'
            if case == 'cross_site': headers['Sec-Fetch-Site'] = 'cross-site'
            if case == 'anonymous': client.cookies.clear()
            if case == 'forged_cookie': headers['Cookie'] = '__Host-cp-session=foreign; __Host-cp-csrf=foreign'
            response = await client.post('/api/moodle/sync', headers=headers, json={})
            assert response.status_code == 403
            assert h.reads == h.refreshes == 0
    run(scenario())


@pytest.mark.parametrize('field', ['owner_id', 'user_id', 'site_url', 'token', 'expected_user_id'])
def test_browser_cannot_select_someone_elses_account_or_inject_credentials(field):
    async def scenario():
        h = MoodleFixture()
        async with h.client() as client:
            headers = await login(client)
            assert (await client.get('/api/moodle', params={field: 'foreign'})).status_code == 400
            assert (await client.post('/api/moodle/sync', headers=headers, json={field: 'foreign'})).status_code == 400
            assert h.reads == h.refreshes == 0
    run(scenario())


def test_cross_owner_reader_result_and_expired_session_never_release_grades():
    async def scenario():
        h = MoodleFixture()
        async with h.client() as client:
            await login(client)
            h.value = h.value.model_copy(update={'owner_id': 'another-student'})
            assert (await client.get('/api/moodle')).status_code == 403
            h.value = h.value.model_copy(update={'owner_id': h.config.student.trusted_user_id})
            h.after_read = lambda: setattr(h.clock, 'value', NOW + timedelta(hours=2))
            result = await client.get('/api/moodle')
            assert result.status_code == 403 and 'Математика' not in result.text
    run(scenario())


def test_mismatched_lms_identity_rejected_and_disconnected_data_hidden():
    async def scenario():
        h = MoodleFixture()
        async with h.client() as client:
            await login(client)
            h.value = h.value.model_copy(update={'expected_user_id': 43})
            assert (await client.get('/api/moodle')).status_code == 403
            h.value = h.value.model_copy(update={'expected_user_id': 42, 'connected': False})
            response = await client.get('/api/moodle')
            assert response.status_code == 200 and response.json()['snapshot'] is None
    run(scenario())


def test_moodle_remains_available_without_planner_projection():
    class UnavailablePlanner:
        async def project(self, *args, **kwargs):
            raise ValueError('calendar_unavailable')

    async def scenario():
        h = MoodleFixture()
        async with h.client(h.app(projection=UnavailablePlanner())) as client:
            await login(client)
            assert (await client.get('/ui/state')).status_code == 400
            result = await client.get('/api/moodle')
            assert result.status_code == 200 and result.json()['snapshot']['tasks']
    run(scenario())


def test_dashboard_is_opt_in_and_planning_does_not_use_old_sheet_data():
    assert 'id="moodle-tasks"' not in render_shell('Asia/Qyzylorda')
    assert 'id="moodle-tasks"' in render_shell('Asia/Qyzylorda', moodle_enabled=True)

    async def scenario():
        h = MoodleFixture()
        async with h.client() as client:
            await login(client)
            panel = (await client.get('/ui/state')).text
            assert 'сколько времени нужно на задания' in panel
            assert 'Составление расписания и применение в Google Calendar пока недоступны' in panel
            assert 'href="#workspace"' in panel
            assert 'data-runtime="false"' in panel
            assert 'Безопасная сводка источника' not in panel
            assert (await client.post('/api/runs', json={})).status_code == 404
    run(scenario())


def test_client_rendering_zero_missing_scores_and_untrusted_text():
    """Execute the production renderer with a tiny DOM double, not a browser claim."""
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for the production JavaScript renderer test')
    renderer = SCRIPT[SCRIPT.index('  function moodleDate'):SCRIPT.index('  async function loadMoodle')]
    state = dict(connected=True, stale=False, syncing=False, error=None,
                 last_success=NOW.isoformat(), snapshot=snapshot().model_dump(mode='json'))
    program = r'''
const assert = require('node:assert/strict');
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.text = ''; }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return this.text + this.children.map(child => child.textContent).join(' '); }
  set innerHTML(value) { throw new Error('HTML injection is forbidden'); }
  replaceChildren(...nodes) { this.text = ''; this.children = nodes; }
  append(...nodes) { this.children.push(...nodes); }
}
const nodes = Object.fromEntries(['moodle-tasks', 'moodle-courses', 'moodle-status'].map(id => [id, new Element('div')]));
const $ = id => nodes[id];
const document = {body: {dataset: {timezone: 'Asia/Qyzylorda'}}, createElement: tag => new Element(tag)};
''' + renderer + '\nconst state = ' + json.dumps(state, ensure_ascii=False) + r''';
showMoodle(state);
assert.match(nodes['moodle-tasks'].textContent, /0 \/ 10/);
assert.match(nodes['moodle-courses'].textContent, /0 \/ 100/);
assert.match(nodes['moodle-tasks'].textContent, /<script>secret\(\)<\/script>/);
const tags = node => [node.tag, ...node.children.flatMap(tags)];
assert(!tags(nodes['moodle-tasks']).includes('script'));
assert(!tags(nodes['moodle-tasks']).includes('img'));
assert.equal(moodleLink('javascript:alert(1)'), null);
assert.equal(moodleLink('https://lms.astanait.edu.kz.evil.test/mod/assign/view.php?id=9'), null);
assert.equal(moodleLink('https://lms.astanait.edu.kz/mod/assign/view.php?id=9&token=secret'), null);
state.snapshot.tasks[0].grade_value = null;
showMoodle(state);
assert.match(nodes['moodle-tasks'].textContent, /Ещё не оценено/);
assert(!nodes['moodle-tasks'].textContent.includes('0 / 10'));
state.error = 'unavailable'; state.stale = true;
showMoodle(state);
assert.match(nodes['moodle-status'].textContent, /ранее полученные данные/);
assert.match(nodes['moodle-tasks'].textContent, /<script>/);
state.snapshot = null; showMoodle(state);
assert.equal(nodes['moodle-tasks'].textContent, '');
assert.match(nodes['moodle-status'].textContent, /пока не получены/);
'''
    result = subprocess.run([node], input=program, text=True, encoding='utf-8',
                            capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
