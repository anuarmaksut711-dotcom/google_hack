"""Run real recording handlers against controlled browser media boundaries."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_browser_recording_lifecycle_and_draft_safety():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is required for browser recording regressions')
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [node, str(Path(__file__).with_name('voice_state.cjs')), str(root)],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
