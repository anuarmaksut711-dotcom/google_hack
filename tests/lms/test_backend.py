"""Real production factory wiring, with synthetic identity and in-memory storage."""

import asyncio
from datetime import timedelta

import httpx
import pytest

from campuspulse_recovery.cloud.backend import create_backend
from campuspulse_recovery.domain.ports import PortError
from campuspulse_recovery.lms.sync import MoodleSync
from campuspulse_recovery.security.resources import ResourcePolicy
from campuspulse_recovery.web.auth import VerifiedSignIn
from tests.lms.test_settings import valid_payload, validate
from tests.lms.test_web import snapshot
from tests.storage.test_plans import FakeDocuments, NOW


@pytest.mark.parametrize('worker_available', [True, False])
def test_production_backend_uses_durable_moodle_without_worker_secret_sheets_or_gemini(
        monkeypatch, tmp_path, worker_available):
    async def scenario():
        settings = validate(valid_payload())
        policy = ResourcePolicy(settings.application, environment='cloud')
        documents, refreshed, closed = FakeDocuments(), [], []
        origin = settings.backend_origin
        monkeypatch.delenv('GEMINI_API_KEY', raising=False)
        # A backend must remain bootable even if this worker-only mounted file
        # is absent. All possible external reads below fail the test on use.
        monkeypatch.setenv('CAMPUSPULSE_MOODLE_TOKEN_FILE', str(tmp_path / 'not-mounted-on-backend'))

        def forbidden(*args, **kwargs):
            raise AssertionError('Moodle UI attempted unrelated credentials, model or Sheets IO')

        async def forbidden_async(*args, **kwargs):
            forbidden()

        monkeypatch.setattr('campuspulse_recovery.cloud.moodle_worker._mounted_token', forbidden)
        monkeypatch.setattr('campuspulse_recovery.lms.client.MoodleClient.fetch', forbidden_async)
        monkeypatch.setattr('campuspulse_recovery.cloud.backend.CloudConversation.__init__', forbidden)
        monkeypatch.setattr('campuspulse_recovery.cloud.backend.CloudTransport.request', forbidden_async)
        monkeypatch.setattr('campuspulse_recovery.cloud.backend.normalized_assignments', forbidden)
        monkeypatch.setattr('google.auth.default', forbidden)
        monkeypatch.setattr('campuspulse_recovery.cloud.backend.utcnow', lambda: NOW)

        async def identity(self, token, *, nonce, now):
            assert token == 'offline-signed-google-token' and nonce
            assert now == NOW
            return VerifiedSignIn(principal_id=settings.application.student.allowed_signin_principal,
                authenticated_at=NOW, expires_at=NOW + timedelta(hours=1))

        # Keep the production verifier object/type; only signature verification
        # itself is synthetic. Never request a real Google certificate/token.
        monkeypatch.setattr('campuspulse_recovery.web.auth.GoogleSignInVerifier.verify', identity)

        async def request_refresh(self):
            assert self.origin == settings.moodle.sync_origin
            refreshed.append(True)
            if not worker_available:
                raise PortError('moodle_sync_unavailable')

        monkeypatch.setattr('campuspulse_recovery.cloud.backend.MoodleRefreshTransport.refresh', request_refresh)

        async def fetch_cached():
            return snapshot()

        seeded = MoodleSync(documents, owner_id=settings.application.student.trusted_user_id,
            site_url=settings.moodle.site_url, expected_user_id=settings.moodle.expected_user_id,
            fetch_snapshot=fetch_cached, clock=lambda: NOW)
        await seeded.refresh()
        app = create_backend(settings, policy, documents, close=lambda: closed.append(True))
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as browser:
                assert (await browser.get('/healthz')).json() == {'status': 'alive'}
                assert (await browser.get('/')).status_code == 303
                assert (await browser.get('/api/moodle')).status_code == 403
                challenge = await browser.get('/auth/challenge')
                signin = await browser.post('/auth/signin', headers={
                    'Origin': origin, 'X-CSRF-Token': challenge.json()['csrf_token']},
                    json={'credential': 'offline-signed-google-token'})
                assert signin.status_code == 200, signin.text
                headers = {'Origin': origin, 'X-CSRF-Token': signin.json()['csrf_token']}
                assert 'id="moodle-tasks"' in (await browser.get('/')).text
                cached = await browser.get('/api/moodle')
                assert cached.status_code == 200
                assert cached.json()['snapshot']['tasks'][0]['grade_value'] == 0
                assert (await browser.get('/ui/state')).status_code == 200
                # Production create_backend disables the old Sheets plan path
                # when Moodle is configured; the real Moodle routes are wired.
                assert (await browser.post('/api/runs', headers=headers, json={})).status_code == 404
                result = await browser.post('/api/moodle/sync', headers=headers, json={})
                assert result.status_code == (200 if worker_available else 503), result.text
                assert refreshed == [True]
                after = await browser.get('/api/moodle')
                assert after.json() == cached.json()
                assert after.headers['cache-control'] == 'no-store'
                assert 'token_secret_reference' not in after.text
        assert closed == [True]

    asyncio.run(scenario())
