"""OAuth entry must work in the deployed cloud-only distribution."""
from pathlib import Path
import re
import tomllib

ROOT = Path(__file__).resolve().parents[2]


def test_cloud_release_installs_the_oauth_packages_used_by_calendar_signin():
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']
    oauth = project['optional-dependencies']['oauth-calendar']
    cloud = project['optional-dependencies']['cloud']
    assert set(oauth) <= set(cloud), 'Calendar OAuth is a cloud runtime capability'
    for name in ('cloud-linux.lock', 'test.lock'):
        lock = (ROOT / 'requirements' / name).read_text()
        for dependency in oauth:
            assert re.search(r'^' + re.escape(dependency) + r' \\\n    --hash=sha256:[0-9a-f]{64}', lock, re.M), (name, dependency)
