"""Actual pytest failure phases must survive the safe runner summary."""
import json
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("phase,source,exit_code", [
    ("collection", "this is invalid python syntax ?", 2),
    ("setup", "import pytest\n@pytest.fixture\ndef broken():\n    raise RuntimeError('synthetic setup')\ndef test_body(broken):\n    pass\n", 1),
    ("teardown", "import pytest\n@pytest.fixture\ndef broken():\n    yield\n    raise RuntimeError('synthetic teardown')\ndef test_body(broken):\n    pass\n", 1),
])
def test_actual_non_call_failures_remain_failed(tmp_path, phase, source, exit_code):
    fixture = tmp_path / "test_synthetic.py"
    fixture.write_text(source, encoding="utf-8")
    report = tmp_path / "technical.json"
    runner = Path(__file__).with_name("run_checks.py")
    result = subprocess.run([sys.executable, "-B", str(runner), "--mode", "denied",
        "--report", str(report), "--", "-q", str(fixture), "-p", "no:cacheprovider",
        "--basetemp=" + str(tmp_path / "nested"), "--tb=short"],
        capture_output=True, text=True, timeout=20)
    assert result.returncode == exit_code
    evidence = json.loads(report.read_text(encoding="utf-8"))
    assert evidence["exit_code"] == exit_code and evidence["status"] == "failed"
    if phase == "collection":
        assert evidence["collection_failures"] and evidence["counts"]["passed"] == 0
    else:
        assert evidence["counts"] == {"passed": 0, "skipped": 0, "failed": 1}
        assert any(r["phase"] == phase and r["outcome"] == "failed" for r in evidence["tests"])
        if phase == "teardown":
            assert any(r["phase"] == "call" and r["outcome"] == "passed" for r in evidence["tests"])
