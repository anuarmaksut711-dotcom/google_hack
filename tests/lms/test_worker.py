"""Offline HTTP/identity-claims tests; no live IAM, Moodle or browser session."""
from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from campuspulse_recovery.cloud.moodle_worker import create_moodle_worker
from campuspulse_recovery.lms.models import MoodleSnapshot
from tests.storage.test_plans import FakeDocuments


ORIGIN = 'https://campuspulse-moodle-123.europe-west1.run.app'
SITE = 'https://lms.astanait.edu.kz'
BACKEND = 'backend@campus-test-42.iam.gserviceaccount.com'
SCHEDULER = 'scheduler@campus-test-42.iam.gserviceaccount.com'


@pytest.fixture
def worker(monkeypatch, tmp_path):
    secret = tmp_path / 'moodle-token'
    secret.write_text('synthetic-secret-token', encoding='utf-8')
    monkeypatch.setenv('CAMPUSPULSE_MOODLE_TOKEN_FILE', str(secret))
    calls, closed = [], []

    class Client:
        def __init__(self, site_url, token, expected_user_id):
            assert (site_url, token, expected_user_id) == (SITE, 'synthetic-secret-token', 42)

        async def fetch(self):
            calls.append(True)
            return MoodleSnapshot(site_url=SITE, user_id=42,
                fetched_at=datetime.now(UTC), courses=(), tasks=())

    monkeypatch.setattr('campuspulse_recovery.cloud.moodle_worker.MoodleClient', Client)
    config = SimpleNamespace(student=SimpleNamespace(trusted_user_id='student-1'),
        identities=SimpleNamespace(backend_service_account=BACKEND))
    settings = SimpleNamespace(application=config, moodle=SimpleNamespace(site_url=SITE,
        expected_user_id=42, sync_origin=ORIGIN, scheduler_service_account=SCHEDULER,
        interval_minutes=15, stale_after_minutes=30))
    policy = SimpleNamespace(config=config)
    documents = FakeDocuments()
    now = int(datetime.now(UTC).timestamp())
    claims = dict(iss='https://accounts.google.com', aud=ORIGIN, email=SCHEDULER,
        email_verified=True, sub='scheduler-subject', iat=now - 60, exp=now + 600)
    verified_audiences = []

    def verify(token, request, audience):
        assert token == 'fake-google-id-token'
        verified_audiences.append(audience)
        return claims.copy()

    monkeypatch.setattr('google.oauth2.id_token.verify_oauth2_token', verify)
    app = create_moodle_worker(settings, policy, documents, close=lambda: closed.append(True))
    return SimpleNamespace(app=app, calls=calls, closed=closed, documents=documents,
        claims=claims, verified_audiences=verified_audiences, settings=settings, policy=policy)


AUTH = {'Authorization': 'Bearer fake-google-id-token'}


@pytest.mark.parametrize('caller', [SCHEDULER, BACKEND])
def test_poll_succeeds_without_browser_session_and_returns_only_summary(worker, caller):
    worker.claims['email'] = caller
    with TestClient(worker.app) as client:
        assert not client.cookies
        response = client.post('/internal/moodle/sync', headers=AUTH)
        assert response.status_code == 200
        assert response.json()['status'] == 'ready' and response.json()['task_count'] == 0
        assert set(response.json()) == {'status', 'stale', 'syncing', 'last_attempt',
                                       'last_success', 'task_count', 'error'}
        assert 'snapshot' not in response.json() and 'synthetic-secret-token' not in response.text
        # Duplicate scheduler dispatch / immediate on-open refresh are deduplicated.
        assert client.post('/internal/moodle/sync', headers=AUTH, json={'force': True}).status_code == 200
    assert worker.calls == [True] and worker.closed == [True]
    assert set(worker.verified_audiences) == {ORIGIN}
    assert all(path.startswith('cp_moodle_connections/') for path in worker.documents.data)
    assert 'synthetic-secret-token' not in repr(worker.documents.data)


@pytest.mark.parametrize('override', [
    {'aud': 'https://attacker.example'},
    {'email': 'other@campus-test-42.iam.gserviceaccount.com'},
    {'email_verified': False},
    {'iss': 'https://attacker.example'},
    {'sub': ''},
    {'exp': 0},
    {'iat': 9999999999},
])
def test_wrong_audience_principal_and_invalid_claims_cannot_trigger_io(worker, override):
    worker.claims.update(override)
    with TestClient(worker.app) as client:
        response = client.post('/internal/moodle/sync', headers=AUTH)
        assert response.status_code == 403 and response.json() == {'error': 'service_identity_rejected'}
    assert not worker.calls and not worker.documents.data


def test_missing_or_duplicated_identity_is_rejected_but_health_is_liveness(worker):
    with TestClient(worker.app) as client:
        assert client.get('/healthz').json() == {'status': 'alive'}
        assert client.post('/internal/moodle/sync').status_code == 403
        duplicate = [('Authorization', AUTH['Authorization']), ('Authorization', AUTH['Authorization'])]
        assert client.post('/internal/moodle/sync', headers=duplicate).status_code == 403
    assert not worker.calls and not worker.documents.data


@pytest.mark.parametrize('body', [
    b'{"force":"true"}', b'{"owner_id":"other"}', b'[]', b'invalid', b'x' * 1025,
])
def test_request_cannot_select_student_or_expand_sync_authority(worker, body):
    with TestClient(worker.app) as client:
        response = client.post('/internal/moodle/sync', headers=AUTH, content=body)
        assert response.status_code == 400 and response.json() == {'error': 'invalid_sync_request'}
    assert not worker.calls and not worker.documents.data


def test_invalid_token_mount_fails_closed_without_starting_service(worker, monkeypatch, tmp_path):
    monkeypatch.delenv('CAMPUSPULSE_MOODLE_TOKEN_FILE')
    with pytest.raises(ValueError, match='mount_required'):
        create_moodle_worker(worker.settings, worker.policy, worker.documents, close=lambda: None)
    token = tmp_path / 'invalid-token'
    token.write_text('contains internal whitespace', encoding='utf-8')
    monkeypatch.setenv('CAMPUSPULSE_MOODLE_TOKEN_FILE', str(token))
    with pytest.raises(ValueError, match='mount_invalid'):
        create_moodle_worker(worker.settings, worker.policy, worker.documents, close=lambda: None)
