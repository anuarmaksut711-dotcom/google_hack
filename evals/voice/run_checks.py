"""Run pytest/CLI with an audit network fence and retain technical-only summary.

Example: python -B evals/voice/run_checks.py --mode denied --report PATH -- tests/voice --ignore=tests/voice/test_windows_client.py
"""
import argparse
from dataclasses import asdict
from datetime import UTC, datetime
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import subprocess
import time


def test_counts(rows):
    """A failed setup/teardown outweighs a passed call for the same node."""
    priority = {"passed": 0, "skipped": 1, "failed": 2}
    outcomes = {}
    for row in rows:
        before = outcomes.get(row["nodeid"], "passed")
        if priority[row["outcome"]] >= priority[before]:
            outcomes[row["nodeid"]] = row["outcome"]
    return {s: sum(v == s for v in outcomes.values()) for s in priority}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("denied", "literal-loopback"), required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--cli", action="store_true")
    parser.add_argument("--adapt-synthetic-mcp-listen", action="store_true")
    opts, args = parser.parse_known_args()
    if args[:1] == ["--"]: args = args[1:]
    os.environ["W12_NETWORK_MODE"] = opts.mode
    os.environ["W12_JOINED_EVIDENCE_DIR"] = str(opts.report.parent / (opts.report.stem + "-joined"))

    os.environ["PYTHONPATH"] = str(Path(__file__).resolve().parent) + os.pathsep + os.environ.get("PYTHONPATH", "")
    import network_guard
    child_counts = {"python_fenced": 0, "other_processes": 0}
    child_listeners = []
    from child_listen import select_child, SPEC_ENV, REPORT_ENV
    original_popen = subprocess.Popen
    guard_dir = str(Path(__file__).resolve().parent)
    def prepare_child(command, positional, kwargs):


        if isinstance(command, (list, tuple)) and command and Path(command[0]).resolve() == Path(sys.executable).resolve():
            command = list(command)
            env = dict(kwargs.get("env") or os.environ)
            env.pop(SPEC_ENV, None)
            env.pop(REPORT_ENV, None)
            env["W12_NETWORK_MODE"] = opts.mode
            env["PYTHONPATH"] = guard_dir + os.pathsep + env.get("PYTHONPATH", "")
            kwargs["env"] = env
            selected = select_child(command, env, mode=opts.mode, enabled=opts.adapt_synthetic_mcp_listen)
            if selected is not None:
                record = opts.report.parent / (opts.report.stem + "-children") / (str(len(child_listeners)) + ".json")
                if record.exists(): raise AssertionError("W12 fresh child evidence path required")
                env[SPEC_ENV] = json.dumps(asdict(selected))
                env[REPORT_ENV] = str(record.resolve())
                child_listeners.append(dict(selection=asdict(selected), evidence_path=str(record)))
            if "-I" in command:
                if "-c" not in command: raise AssertionError("W12 unsupported isolated Python child")
                index = command.index("-c") + 1
                original_code = command[index]
                command[index] = ("import sys; sys.path.insert(0, " + repr(guard_dir)
                    + "); import network_guard; sys.path.pop(0); exec(compile("
                    + repr(original_code) + ", '<isolated-test>', 'exec'))")
            child_counts["python_fenced"] += 1
        else:
            child_counts["other_processes"] += 1
        return command, positional, kwargs
    class GuardedPopen(original_popen):
        def __init__(self, command, *positional, **kwargs):
            command, positional, kwargs = prepare_child(command, positional, kwargs)
            super().__init__(command, *positional, **kwargs)
    subprocess.Popen = GuardedPopen
    rows = []
    collection_failures = []
    class Evidence:
        def pytest_collectreport(self, report):
            if report.failed:
                collection_failures.append(dict(nodeid=report.nodeid, outcome="failed", phase="collection"))
        def pytest_runtest_logreport(self, report):
            if report.when == "call" or report.failed or (report.when == "setup" and report.skipped):
                rows.append(dict(nodeid=report.nodeid, outcome=report.outcome,
                                 phase=report.when,
                                 duration_seconds=round(report.duration, 6),
                                 expected_failure=hasattr(report, "wasxfail")))
    start = time.monotonic()
    code = 1
    versions = {}
    for name in ("google-adk", "google-genai", "google-cloud-aiplatform", "mcp", "pytest"):
        try: versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError: versions[name] = "not-installed"
    try:
        if opts.cli:
            import runpy
            sys.argv = ["tools.voice_windows", *args]
            try: runpy.run_module("tools.voice_windows", run_name="__main__")
            except SystemExit as end: code = end.code or 0
            else: code = 0
        else:
            import pytest
            code = int(pytest.main(list(args), plugins=[Evidence()]))
        return code
    finally:
        for selected in child_listeners:
            path = Path(selected["evidence_path"])
            selected["evidence"] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"status": "no_bind_record"}
        summary = dict(evidence_class="windows_local", network_mode=opts.mode,
            completed_at=datetime.now(UTC).isoformat(), elapsed_seconds=round(time.monotonic()-start, 3),
            exit_code=int(code), network_audit=network_guard.COUNTS,
            blocked_network_events=network_guard.BLOCKED_EVENTS, child_processes=child_counts,
            synthetic_listen_adapter=opts.adapt_synthetic_mcp_listen,
            child_listener_evidence=child_listeners,
            argv=args, python=sys.version.split()[0],
            versions=versions,
            status="passed" if int(code) == 0 and not collection_failures and not test_counts(rows)["failed"] else "failed",
            collection_failures=collection_failures, counts=test_counts(rows),
            tests=rows)
        opts.report.parent.mkdir(parents=True, exist_ok=True)
        opts.report.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
