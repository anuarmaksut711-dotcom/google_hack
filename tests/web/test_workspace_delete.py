"""Run deletion UI behavior against the shipped workspace controller."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_workspace_delete_confirmation_recovery_and_session_isolation():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for browser state regressions")
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [node, str(Path(__file__).with_name("workspace_delete.cjs")), str(root)],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
