"""Exercise the shipped welcome animation through its browser lifecycle."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_welcome_logo_survives_drafts_and_chat_navigation():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is required for welcome animation regressions')
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [node, str(Path(__file__).with_name('welcome_logo.cjs')), str(root)],
        capture_output=True, text=True, encoding='utf-8', timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
