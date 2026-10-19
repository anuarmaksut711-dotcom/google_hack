"""Verify the runner's audit fence without making an actual socket call."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


def test_parent_audit_rejects_external_destination_without_socket():
    assert os.environ.get("W12_NETWORK_MODE") in ("denied", "literal-loopback")
    with pytest.raises(AssertionError, match="W12 network fence"):
        sys.audit("socket.connect", None, ("198.51.100.2", 443))


def test_isolated_python_child_keeps_fence_when_legacy_replaces_pythonpath():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    code = """
import sys
try:
    sys.audit('socket.connect', None, ('198.51.100.2', 443))
except AssertionError as error:
    assert str(error) == 'W12 network fence rejected I/O'
    print('isolated-child-fenced')
else:
    raise SystemExit(4)
"""
    result = subprocess.run([sys.executable, "-I", "-B", "-c", code], env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0 and result.stdout.strip() == "isolated-child-fenced"
