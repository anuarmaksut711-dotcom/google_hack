"""Execute the shipped browser state functions, without a model or browser."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_drafts_survive_transient_errors_and_reconcile_completed_requests():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is required for browser state regressions')
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run([node, str(Path(__file__).with_name('client_state.cjs')), str(root)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
