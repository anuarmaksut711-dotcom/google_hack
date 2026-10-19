"""Execute the real PowerShell Prepare path offline with synthetic settings."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from tests.lms.test_settings import PROJECT, valid_payload


SCRIPT = Path(__file__).resolve().parents[2] / 'deploy' / 'deploy-moodle.ps1'
PWSH = shutil.which('pwsh')
pytestmark = pytest.mark.skipif(PWSH is None, reason='PowerShell 7 not installed')


def prepare(tmp_path, settings, interval=15):
    settings['moodle']['interval_minutes'] = interval
    inputs = {'project_id': PROJECT, 'project_number': '123', 'deployer_account': 'deployer@example.com',
        'web_cloud_run_region': 'europe-west1', 'firestore_database': '(default)',
        'agent_region': 'europe-west1', 'mcp_cloud_run_region': 'europe-west1',
        **settings['application']['identities'],
        'build_service_account': 'builder@' + PROJECT + '.iam.gserviceaccount.com',
        'artifact_registry_location': 'europe-west1', 'artifact_registry_repository': 'campuspulse'}
    inputs_path, settings_path = tmp_path / 'inputs.json', tmp_path / 'settings.json'
    inputs_path.write_text(json.dumps(inputs), encoding='utf-8')
    settings_path.write_text(json.dumps(settings), encoding='utf-8')
    return subprocess.run([PWSH, '-NoProfile', '-NonInteractive', '-File', str(SCRIPT),
        '-InputsPath', str(inputs_path), '-SettingsPath', str(settings_path),
        '-SettingsSecret', 'campuspulse-cloud-settings:3',
        '-ImageDigest', 'europe-west1-docker.pkg.dev/' + PROJECT + '/campuspulse/app@sha256:' + 'a' * 64],
        text=True, capture_output=True, timeout=20)


@pytest.mark.parametrize('interval,cron', [(5, '*/5 * * * *'), (10, '*/10 * * * *'),
    (15, '*/15 * * * *'), (20, '*/20 * * * *'), (30, '*/30 * * * *'), (60, '0 * * * *')])
def test_prepare_accepts_json_integer_user_and_exact_cadence_without_gcloud(tmp_path, interval, cron):
    settings = valid_payload()
    settings['moodle']['stale_after_minutes'] = max(30, interval)
    result = prepare(tmp_path, settings, interval)
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan['status'] == 'prepared_not_deployed'
    assert plan['schedule'] == cron and plan['timezone'] == 'Etc/UTC'
    assert plan['origin'] == settings['moodle']['sync_origin']


@pytest.mark.parametrize('field,value', [
    ('expected_user_id', '42'), ('expected_user_id', True), ('expected_user_id', 0),
    ('site_url', 'https://attacker.example'),
    ('sync_service_account', 'backend@' + PROJECT + '.iam.gserviceaccount.com'),
    ('scheduler_service_account', 'moodle-sync@' + PROJECT + '.iam.gserviceaccount.com'),
    ('token_secret_reference', 'projects/' + PROJECT + '/secrets/moodle-token/versions/latest'),
])
def test_prepare_rejects_account_or_secret_mismatch_before_cloud_io(tmp_path, field, value):
    settings = valid_payload()
    settings['moodle'][field] = value
    result = prepare(tmp_path, settings)
    assert result.returncode != 0
    assert 'prepared_not_deployed' not in result.stdout


def test_moodle_only_web_prepare_needs_no_gemini_secret_or_working_hours(tmp_path):
    settings = valid_payload()
    settings['working_hours'] = []
    assert prepare(tmp_path, settings).returncode == 0
    result = subprocess.run([PWSH, '-NoProfile', '-NonInteractive', '-File',
        str(SCRIPT.with_name('deploy-cloud.ps1')), '-InputsPath', str(tmp_path / 'inputs.json'),
        '-SettingsPath', str(tmp_path / 'settings.json'), '-MoodleOnly'],
        text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan['status'] == 'prepared_not_deployed'
    assert [service['role'] for service in plan['services']] == ['backend']
    assert not plan['key_secret_reference']
