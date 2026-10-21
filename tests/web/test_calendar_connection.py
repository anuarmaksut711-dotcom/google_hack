"""Calendar mutations use the authenticated CSRF repair boundary, not cookie access."""
from pathlib import Path
import shutil
import subprocess

import pytest

from campuspulse_recovery.web.google_calendar import SCRIPT


def test_calendar_connection_bootstrap_and_session_guards():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is required for browser authentication tests')
    result = subprocess.run(
        [node, str(Path(__file__).with_name('calendar_connection.cjs'))],
        input=SCRIPT, encoding='utf-8', capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
