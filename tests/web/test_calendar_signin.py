"""Calendar redirect uses the current login challenge rather than session CSRF."""
from pathlib import Path
import shutil
import subprocess

import pytest

from campuspulse_recovery.web.google_calendar import SCRIPT


def test_calendar_signin_preserves_challenge_csrf_across_redirect():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is required for browser authentication tests')
    result = subprocess.run(
        [node, str(Path(__file__).with_name('calendar_signin.cjs'))],
        input=SCRIPT, encoding='utf-8', capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
