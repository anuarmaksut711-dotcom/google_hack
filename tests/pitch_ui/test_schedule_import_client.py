"""Execute the actual weekly editor script, including asynchronous race cases."""
import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize('case', ['explicit_import', 'review_dates_save', 'manual', 'failure',
    'duplicate_import', 'new_file', 'session_change', 'double_save', 'uncertain_save', 'local_size'])
def test_schedule_import_client(case):
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(['node', str(Path(__file__).with_suffix('.cjs')), str(root), case],
        text=True, capture_output=True, timeout=15, env={**os.environ, 'TZ':'Asia/Almaty'})
    assert result.returncode == 0, result.stdout + result.stderr
