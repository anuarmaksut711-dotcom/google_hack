"""A failed release stays failed and exposes its bounded diagnostic log."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest


def test_failed_release_prints_the_test_failure_without_suppressing_it(tmp_path, monkeypatch, capsys):
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location('pitch_release_gate', root / 'deploy/run-rollout-checks.py')
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    output = tmp_path / 'verification'
    monkeypatch.setattr(sys, 'argv', ['gate', '--source-sha', 'a' * 40, '--output', str(output)])
    calls = []

    def run(args, *, stdout, stderr):
        calls.append(args)
        failed = len(calls) == 2
        stdout.write('FAILED synthetic-release-case: diagnostic detail\n' if failed else 'ok\n')
        return SimpleNamespace(returncode=2 if failed else 0)

    monkeypatch.setattr(gate.subprocess, 'run', run)
    with pytest.raises(RuntimeError, match='release_check_failed:pytest'):
        gate.main()
    assert len(calls) == 2
    assert 'FAILED synthetic-release-case: diagnostic detail' in capsys.readouterr().out
    assert 'FAILED synthetic-release-case' in (output / 'pytest.log').read_text()
