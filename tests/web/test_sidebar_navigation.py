"""Exercise the shipped navigation controller without cloud services."""
from pathlib import Path
import json
import shutil
import subprocess

import pytest

from campuspulse_recovery.web.assets.sidebar import HEAD_SCRIPT, SCRIPT


def test_sidebar_keeps_icon_navigation_and_restores_its_interaction_state():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is required for browser navigation tests')
    result = subprocess.run(
        [node, str(Path(__file__).with_name('sidebar_navigation.cjs'))],
        input=json.dumps({'head': HEAD_SCRIPT, 'script': SCRIPT}),
        capture_output=True, text=True, encoding='utf-8', timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
