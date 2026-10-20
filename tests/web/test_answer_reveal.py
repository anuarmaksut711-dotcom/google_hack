"""Browser presentation lifecycle, independent of LLMs and Google services."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_completed_answers_reveal_gradually_without_losing_content():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is required for browser presentation tests')
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [node, str(Path(__file__).with_name('answer_reveal.cjs')), str(root)],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
