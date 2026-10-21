"""Run the shipped editor against successive daily screenshot responses."""
import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize('case', ['append_days', 'preserve_dates', 'repeat_file',
    'saved_rows', 'manual_rows', 'failed_next_day', 'malformed_next_day',
    'session_changes', 'whole_week_confirmation', 'row_limit', 'legacy_markup'])
def test_partial_schedule_client(case):
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(['node', str(Path(__file__).with_suffix('.cjs')), str(root), case],
        text=True, capture_output=True, timeout=15, env={**os.environ, 'TZ': 'Asia/Almaty'})
    assert result.returncode == 0, result.stdout + result.stderr
