"""Packaging checks for the installed wheel, without source-path injection."""

from importlib import metadata
import os
import subprocess
import sys

import pytest


def _private_artifact_path(path):
    return (".env" in path or ".validation" in path or
            ("credentials" in path and
             path != "campuspulse_recovery/integrations/google_credentials.py"))


@pytest.mark.parametrize("path", [
    "campuspulse_recovery/integrations/google_credentials.py.bak",
    "campuspulse_recovery/integrations/google_credentials.json",
    "campuspulse_recovery/integrations/backup/google_credentials.py",
    "nested/campuspulse_recovery/integrations/google_credentials.py",
    "campuspulse_recovery/integrations/client_credentials.py",
    "campuspulse_recovery/credentials/service-account.json",
    "campuspulse_recovery/integrations/.env/google_credentials.py",
    "campuspulse_recovery/integrations/.validation/google_credentials.py",
    "campuspulse_recovery/.env",
    "campuspulse_recovery/.validation/private.json",
])
def test_private_artifact_counterexamples_still_rejected(path):
    assert _private_artifact_path(path)


def test_only_exact_credentials_source_path_is_exempted():
    assert not _private_artifact_path("campuspulse_recovery/integrations/google_credentials.py")


def test_namespace_import_without_credentials(tmp_path):
    env = {key: value for key, value in os.environ.items() if not key.startswith("GOOGLE_")}
    code = (
        "import sys, campuspulse_recovery; "
        "assert not any(x in sys.modules for x in ('google.adk', 'vertexai', 'mcp')); "
        "print(campuspulse_recovery.__version__)"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", code], cwd=tmp_path, env=env,
        capture_output=True, text=True, check=True, timeout=30,
    )
    assert result.stdout.strip() == metadata.version("campuspulse-recovery")


def test_wheel_contains_mit_notice():
    dist = metadata.distribution("campuspulse-recovery")
    notices = [p for p in dist.files if str(p).endswith("licenses/requirements/LICENSE")]
    assert len(notices) == 1
    notice = dist.locate_file(notices[0]).read_text(encoding="utf-8")
    assert "Copyright (c) 2025 Taylor Wilsdon" in notice
    assert "Permission is hereby granted" in notice


def test_wheel_has_no_upstream_or_private_files():
    dist = metadata.distribution("campuspulse-recovery")
    files = [str(p).replace("\\", "/") for p in dist.files]
    assert all(p.startswith(("campuspulse_recovery/", "campuspulse_recovery-")) for p in files)
    assert not any(_private_artifact_path(p) for p in files)
