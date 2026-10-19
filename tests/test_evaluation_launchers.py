"""Evaluation CLIs must work from an installed environment without shell setup."""
import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def test_voice_launcher_resolves_project_fixtures_without_pythonpath(tmp_path):
    fixture = tmp_path / "test_fixture_import.py"
    fixture.write_text(
        "from tests.domain.test_solver import request\n"
        "from campuspulse_recovery.domain.solver import solve_repair\n"
        "def test_solver():\n"
        "    assert solve_repair(request()).status == 'feasible'\n",
        encoding="utf-8",
    )
    report = tmp_path / "voice.json"
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    result = subprocess.run([
        sys.executable, "-B", str(ROOT / "evals/voice/run_checks.py"),
        "--mode", "denied", "--report", str(report), "--", "-q", str(fixture),
        "-p", "no:cacheprovider",
    ], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(report.read_text(encoding="utf-8"))
    assert evidence["status"] == "passed"
    assert evidence["counts"] == {"passed": 1, "skipped": 0, "failed": 0}


def test_isolated_recovery_launcher_finds_active_environment(tmp_path):
    output = tmp_path / "recovery"
    result = subprocess.run([
        sys.executable, "-I", "-S", "-B",
        str(ROOT / "evals/recovery/run_evaluation.py"),
        "--mode", "offline", "--output", str(output),
    ], cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads((output / "local-summary.json").read_text(encoding="utf-8"))
    assert evidence["pytest_exit"] == 0
    assert evidence["real_Gemini"] == "NOT_RUN"
    guards = json.loads((output / "guards.json").read_text(encoding="utf-8"))
    assert guards["unexpected_hits"] == []
