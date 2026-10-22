"""Recovered production MCP composes with current sources; no cloud I/O.

Domain services, transports, lifespans and authorization adapters are real.
Only Firestore client construction/documents and signed-token verification are
replaced, and all mounted configuration is synthetic test data.
"""
from datetime import UTC, datetime, timedelta
import json
import logging
from types import SimpleNamespace

from cryptography.fernet import Fernet
import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from campuspulse_recovery.cloud import live_mcp, study_mcp
from campuspulse_recovery.cloud.calendar_app import CalendarSettings
from campuspulse_recovery.demo.adapters import demo_config
from campuspulse_recovery.integrations.local_calendar import CALLBACK
from campuspulse_recovery.security.context import ServerRunBinding
from tests.lms.test_multiuser import run_async

ORIGIN = 'https://study-mcp.example.run.app'


@pytest.fixture(autouse=True)
def restore_process_logging():
    previous = logging.root.manager.disable
    previous_factory = logging.getLogRecordFactory()
    loggers = [logging.root, *(value for value in logging.root.manager.loggerDict.values()
                              if isinstance(value, logging.Logger))]
    state = {logger: (list(logger.filters), logger.level, logger.disabled, logger.propagate)
             for logger in loggers}
    try:
        yield
    finally:
        # Production composition deliberately installs permanent process-wide
        # SDK privacy. Keep it permanent in the product, but isolate this test's
        # process from later tests which establish an unredacted SDK baseline.
        logging.setLogRecordFactory(previous_factory)
        current = [logging.root, *(value for value in logging.root.manager.loggerDict.values()
                                  if isinstance(value, logging.Logger))]
        for logger in current:
            filters, level, disabled, propagate = state.get(logger, ([], logging.NOTSET, False, True))
            logger.filters[:] = filters
            logger.setLevel(level)
            logger.disabled, logger.propagate = disabled, propagate
        logging.disable(previous)


def configuration():
    config = demo_config().model_dump(mode='json')
    config['mode'] = 'cloud'
    return CalendarSettings(application=config, origin='https://study-web.example.run.app',
        google_client_id='test.apps.googleusercontent.com',
        chat={'provider': 'vertex', 'project_id': config['project_id'],
              'location': 'global', 'model': 'gemini-test'},
        scheduler_service_account='demo-scheduler@campuspulse-demo.iam.gserviceaccount.com',
        study_agent={'target': {'project_id': config['project_id'], 'location': config['agent_region'],
            'resource_name': f"projects/123/locations/{config['agent_region']}/reasoningEngines/456"},
            'agent_audience': 'https://study-agent.example.run.app', 'mcp_origin': ORIGIN,
            'backend_service_account': config['identities']['backend_service_account'],
            'agent_service_account': 'study-isolated@campuspulse-demo.iam.gserviceaccount.com'})


@run_async
async def test_recovered_legacy_calendar_and_study_transports_compose_without_external_io(tmp_path, monkeypatch):
    import google.cloud.firestore
    import google.oauth2.id_token
    settings = configuration()
    config = settings.application
    now = datetime.now(UTC)
    binding = ServerRunBinding(owner_id=config.student.trusted_user_id,
        signin_principal_id=config.student.allowed_signin_principal,
        session_id='session-legacy', run_id='run-legacy', config_revision=config.config_revision,
        expires_at=now + timedelta(minutes=10))
    invocation = {'config_revision': config.config_revision, 'binding': binding.model_dump_json(),
                  'state': 'running'}
    closed = []

    class AsyncClient:
        def __init__(self, *, project, database):
            self.project, self._database = project, database

        def close(self):
            closed.append(self)

        def document(self, path):
            raise AssertionError('Composition/listing must not request private Firestore sources')

    class SyncClient:
        def document(self, path):
            assert path == 'cp_cloud_invocations/inv_' + 'a' * 32
            return SimpleNamespace(get=lambda **kwargs:
                SimpleNamespace(exists=True, to_dict=lambda: invocation))

    def verify(token, request, *, audience):
        assert audience == ORIGIN
        principal = {'synthetic-legacy-identity': config.identities.agent_service_account,
            'synthetic-website-identity': config.identities.backend_service_account,
            'synthetic-study-identity': settings.study_agent.agent_service_account}[token]
        return {'email': principal, 'email_verified': True, 'aud': ORIGIN,
                'iss': 'https://accounts.google.com', 'sub': 'synthetic-service-subject',
                'iat': int((now - timedelta(seconds=2)).timestamp()),
                'exp': int((now + timedelta(minutes=10)).timestamp())}

    monkeypatch.setattr(google.cloud.firestore, 'AsyncClient', AsyncClient)
    monkeypatch.setattr(live_mcp, 'sync_client', lambda _: SyncClient())
    monkeypatch.setattr(google.oauth2.id_token, 'verify_oauth2_token', verify)
    monkeypatch.setenv('CAMPUSPULSE_LIVE_SETTINGS', json.dumps(
        {'app_config': config.model_dump(mode='json'), 'mcp_url': ORIGIN}))
    settings_file = tmp_path / 'study-settings.json'
    settings_file.write_text(settings.model_dump_json(), encoding='utf-8')
    monkeypatch.setenv('CAMPUSPULSE_CALENDAR_SETTINGS', str(settings_file))
    oauth = {'project_id': config.project_id, 'database': config.firestore_database,
        'origin': settings.origin, 'mcp_origin': ORIGIN, 'key': Fernet.generate_key().decode(),
        'backend_service_account': config.identities.backend_service_account,
        'client': {'web': {'client_id': settings.google_client_id, 'client_secret': 'synthetic-only',
            'project_id': config.project_id, 'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
            'token_uri': 'https://oauth2.googleapis.com/token',
            'redirect_uris': [settings.origin + CALLBACK]}}}
    oauth_file = tmp_path / 'synthetic-oauth.json'
    oauth_file.write_text(json.dumps(oauth), encoding='utf-8')
    monkeypatch.setenv('CAMPUSPULSE_CALENDAR_OAUTH', str(oauth_file))
    app = study_mcp.create_app()
    assert app.complete is True
    async with app.app.router.lifespan_context(app.app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as http:
            assert (await http.get('/healthz')).json() == {'status': 'alive'}
            assert (await http.post('/mcp', json={})).status_code == 401
            assert (await http.post('/private/evidence', json={})).status_code == 401
            assert (await http.post('/calendar/mcp', json={})).status_code == 403
            assert (await http.post('/study/mcp', json={})).status_code == 403
        for path, identity, name, expected in (
            ('/mcp', 'legacy', 'campuspulse_recovery',
             {'read_assignments', 'read_calendar', 'apply_plan', 'verify_plan'}),
            ('/calendar/mcp', 'website', 'campuspulse_connected_calendar',
             {'read_calendar', 'apply_plan', 'verify_plan'}),
            ('/study/mcp', 'study', 'campuspulse_study_readonly',
             {'read_assignments', 'read_schedule', 'read_github_assignment'}),
        ):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN,
                    headers={'Authorization': f'Bearer synthetic-{identity}-identity',
                             'X-CP-Invocation': 'inv_' + 'a' * 32}) as http:
                async with streamable_http_client(ORIGIN + path, http_client=http) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        result = await session.initialize()
                        assert result.serverInfo.name == name
                        tools = (await session.list_tools()).tools
                        assert {tool.name for tool in tools} == expected
        # The new deadline-agent identity cannot acquire the calendar action
        # surface; the website identity cannot invoke its model-selected tools.
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as http:
            assert (await http.post('/calendar/mcp', json={}, headers={
                'Authorization': 'Bearer synthetic-study-identity'})).status_code == 403
            assert (await http.post('/mcp', json={}, headers={
                'Authorization': 'Bearer synthetic-study-identity',
                'X-CP-Invocation': 'inv_' + 'a' * 32})).status_code == 401
            assert (await http.post('/study/mcp', json={}, headers={
                'Authorization': 'Bearer synthetic-website-identity'})).status_code == 403
    assert len(closed) == 3
