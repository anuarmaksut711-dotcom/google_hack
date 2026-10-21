"""Execute the shipped progress renderer and polling lifecycle without cloud IO."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_real_study_trace_rendering_and_read_only_polling():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is required for browser progress regressions')
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [node, str(Path(__file__).with_name('study_trace.cjs')), str(root)],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
