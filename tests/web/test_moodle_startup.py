"""Run actual browser startup scheduling without credentials or external IO."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_moodle_waits_for_scoped_session_without_blocking_chat_state():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is required for browser startup regressions')
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [node, str(Path(__file__).with_name('moodle_startup.cjs')), str(root)],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
