"""Offline deployment account binding and pinned credential configuration tests."""
import json

import pytest
from pydantic import ValidationError

from campuspulse_recovery.cloud.settings import CloudSettings


PROJECT = 'campus-test-42'


def valid_payload():
    return {
        'application': {
            'mode': 'cloud', 'project_id': PROJECT, 'agent_region': 'europe-west1',
            'model_location': 'europe-west1', 'web_cloud_run_region': 'europe-west1',
            'mcp_cloud_run_region': 'europe-west1', 'firestore_location': 'eur3',
            'firestore_database': '(default)', 'workspace_access_mode': 'explicit_resource_sharing',
            'student': {'trusted_user_id': 'student-1',
                'allowed_signin_principal': 'https://accounts.google.com#student-subject',
                'spreadsheet_id': 'sheet-1', 'assignments_a1_range': 'Tasks!A1:H100',
                'calendar_id': 'calendar@example.com', 'timezone': 'Asia/Qyzylorda'},
            'identities': {
                'backend_service_account': 'backend@' + PROJECT + '.iam.gserviceaccount.com',
                'agent_service_account': 'agentxx@' + PROJECT + '.iam.gserviceaccount.com',
                'mcp_service_account': 'mcpserv@' + PROJECT + '.iam.gserviceaccount.com',
            },
        },
        'backend_origin': 'https://campuspulse-web-123.europe-west1.run.app',
        'agent_origin': 'https://campuspulse-agent-123.europe-west1.run.app',
        'mcp_origin': 'https://campuspulse-mcp-123.europe-west1.run.app',
        'google_signin_client_id': 'synthetic.apps.googleusercontent.com',
        'chat': {'provider': 'gemini_api', 'model': 'gemini-2.5-flash'},
        'working_hours': [{'weekdays': [0, 1, 2, 3, 4], 'start': '09:00', 'end': '18:00'}],
        'moodle': {'site_url': 'https://lms.astanait.edu.kz', 'expected_user_id': 42,
            'token_secret_reference': 'projects/' + PROJECT + '/secrets/moodle-token/versions/1',
            'sync_origin': 'https://campuspulse-moodle-123.europe-west1.run.app',
            'sync_service_account': 'moodle-sync@' + PROJECT + '.iam.gserviceaccount.com',
            'scheduler_service_account': 'moodle-scheduler@' + PROJECT + '.iam.gserviceaccount.com',
            'interval_minutes': 15, 'stale_after_minutes': 30},
    }


def validate(value):
    return CloudSettings.model_validate_json(json.dumps(value))


def test_moodle_is_optional_for_existing_cloud_deployments():
    value = valid_payload()
    del value['moodle']
    assert validate(value).moodle is None


def test_valid_moodle_binding_loads_without_credentials(monkeypatch):
    monkeypatch.delenv('GEMINI_API_KEY', raising=False)
    monkeypatch.delenv('GOOGLE_APPLICATION_CREDENTIALS', raising=False)
    settings = validate(valid_payload())
    assert settings.moodle.expected_user_id == 42
    assert settings.moodle.interval_minutes == 15
    assert 'token' not in type(settings.moodle).model_fields


@pytest.mark.parametrize('field,value', [
    ('site_url', 'https://attacker.example'),
    ('expected_user_id', True), ('expected_user_id', '42'), ('expected_user_id', 0),
    ('token_secret_reference', 'projects/other-project/secrets/moodle-token/versions/1'),
    ('token_secret_reference', 'projects/' + PROJECT + '/secrets/moodle-token/versions/latest'),
    ('sync_origin', 'http://localhost:8000'),
    ('sync_origin', 'https://campuspulse-web-123.europe-west1.run.app'),
    ('sync_service_account', 'backend@' + PROJECT + '.iam.gserviceaccount.com'),
    ('sync_service_account', 'moodle-sync@other-project.iam.gserviceaccount.com'),
    ('scheduler_service_account', 'moodle-sync@' + PROJECT + '.iam.gserviceaccount.com'),
    ('interval_minutes', 7), ('interval_minutes', 0),
    ('stale_after_minutes', 10),
])
def test_unsafe_or_undeployable_binding_rejected(field, value):
    settings = valid_payload()
    settings['moodle'][field] = value
    with pytest.raises(ValidationError):
        validate(settings)


def test_token_value_cannot_be_accidentally_saved_in_configuration():
    settings = valid_payload()
    settings['moodle']['token'] = 'synthetic-token-value'
    with pytest.raises(ValidationError):
        validate(settings)
