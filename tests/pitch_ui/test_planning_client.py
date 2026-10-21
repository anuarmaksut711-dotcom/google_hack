"""Execute the shipped planning client against controlled browser/API edges."""
import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize('case', [
    'csrf', 'changed_session', 'invalid_token', 'quarter_hour', 'invalid_effort',
    'empty_feed', 'double_save', 'uncertain_save', 'reload_failure',
])
def test_planning_client(case):
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ['node', str(Path(__file__).with_suffix('.cjs')), str(root), case],
        text=True, capture_output=True, timeout=15,
        env={**os.environ, 'TZ': 'Asia/Almaty'},
    )
    assert result.returncode == 0, result.stdout + result.stderr
