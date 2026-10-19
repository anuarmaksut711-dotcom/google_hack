"""Run the actual offline CLI under an ADC and network fence."""
import json
from pathlib import Path
import subprocess
import sys

from tests.agent_engine.test_artifacts import ROOT, LOCK, wheel
from tests.agent_engine.test_runtime import settings

SCRIPT = ROOT / 'deploy/prepare-agent-engine.py'
FENCE = '''import runpy, sys, socket
def forbidden(*args, **kwargs):
    raise AssertionError("Offline preparation attempted ADC or network")
socket.socket.connect = forbidden
import google.auth
google.auth.default = forbidden
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
'''


def run(tmp_path, *extra):
    raw = tmp_path / 'private-settings.json'
    raw.write_text(json.dumps(settings()))
    return subprocess.run([sys.executable, '-c', FENCE, str(SCRIPT), '--settings', str(raw),
        '--wheel', str(wheel(tmp_path)), '--requirements', str(LOCK),
        '--output', str(tmp_path / 'prepared'), *extra], cwd=tmp_path,
        capture_output=True, text=True, timeout=30)


def test_cli_prepares_offline_from_another_working_directory(tmp_path):
    result = run(tmp_path)
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output['status'] == 'prepared_not_deployed' and output['cloud_calls'] == 0
    assert output['artifact_count'] == 5
    assert (tmp_path / 'prepared/manifest.json').is_file()
    assert 'campus-test-42' not in result.stdout and 'student-1' not in result.stdout


def test_cli_has_no_execute_mode_and_does_not_create_artifacts(tmp_path):
    result = run(tmp_path, '--execute')
    assert result.returncode == 2
    assert json.loads(result.stdout)['status'] == 'blocked'
    assert not (tmp_path / 'prepared').exists()


def test_cli_does_not_echo_bad_settings_paths_or_private_values(tmp_path):
    result = subprocess.run([sys.executable, '-c', FENCE, str(SCRIPT),
        '--settings', str(tmp_path / 'private-token-value.json'),
        '--wheel', 'missing.whl', '--output', str(tmp_path / 'prepared')],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 2
    assert json.loads(result.stdout) == {'status': 'blocked', 'reason': 'preparation_failed', 'cloud_calls': 0}
    assert 'private-token-value' not in result.stdout + result.stderr
