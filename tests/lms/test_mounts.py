"""Check Cloud Run's secret-directory contract without accessing any secrets.

Prepare mode does not invoke ``gcloud run deploy``, so its tests cannot catch
the SDK rejecting two different secret volumes at the same mount directory.
"""
from pathlib import Path, PurePosixPath
import re


SCRIPT = Path(__file__).resolve().parents[2] / 'deploy' / 'deploy-moodle.ps1'


def _flag(name):
    source = SCRIPT.read_text(encoding='utf-8')
    matches = re.findall(r"['\"]--" + re.escape(name) + r"=([^'\"\r\n]+)['\"]", source)
    assert len(matches) == 1, f'Expected one explicit --{name} deployment flag'
    return dict(item.split('=', 1) for item in matches[0].split(','))


def test_distinct_worker_secrets_have_distinct_cloud_run_mount_directories():
    mounts = _flag('set-secrets')
    assert len(mounts) == 2
    assert len(set(mounts.values())) == 2
    directories = [PurePosixPath(path).parent for path in mounts]
    assert all(PurePosixPath(path).is_absolute() for path in mounts)
    assert len(set(directories)) == len(directories), (
        'Cloud Run permits only one secret per mounted directory; different '
        'filenames in the same parent directory still conflict'
    )


def test_worker_file_settings_point_to_the_declared_secret_mounts():
    mounts, environment = _flag('set-secrets'), _flag('set-env-vars')
    assert environment['CAMPUSPULSE_ROLE'] == 'moodle-sync'
    file_paths = {environment['CAMPUSPULSE_CLOUD_SETTINGS'],
                  environment['CAMPUSPULSE_MOODLE_TOKEN_FILE']}
    assert len(file_paths) == 2
    assert file_paths == set(mounts), 'Worker must read exactly the mounted secret files'
    assert 'settingsName' in mounts[environment['CAMPUSPULSE_CLOUD_SETTINGS']]
    assert 'tokenName' in mounts[environment['CAMPUSPULSE_MOODLE_TOKEN_FILE']]
