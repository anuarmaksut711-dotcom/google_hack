"""Execute actual chat rendering for optional observed study-agent tool calls."""
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize("case", ["legacy", "actual", "failed", "escaped", "run_id", "malformed"])
def test_chat_renders_only_observed_tool_results(case):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for chat rendering regressions")
    result = subprocess.run([node, str(Path(__file__).with_suffix(".cjs")),
                             str(Path(__file__).resolve().parents[2]), case],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
