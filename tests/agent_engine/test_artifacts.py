"""Inspect real wheel/lock formats without installing or executing their code."""
import hashlib
from pathlib import Path
from zipfile import ZipFile

import pytest

from tests.agent_engine.test_runtime import no_cloud

ROOT = Path(__file__).resolve().parents[2]
LOCK = ROOT / 'requirements/cloud-linux.lock'


def wheel(tmp_path, *, name='campuspulse-recovery', python='>=3.12,<3.13', missing=False):
    target = tmp_path / 'campuspulse_recovery-0.1.0-py3-none-any.whl'
    with ZipFile(target, 'w') as archive:
        archive.writestr('campuspulse_recovery-0.1.0.dist-info/METADATA',
            f'Metadata-Version: 2.4\nName: {name}\nVersion: 0.1.0\nRequires-Python: {python}\n')
        for module in ('agent_engine/runtime.py', 'cloud/agent.py', 'cloud/authority.py'):
            if missing and module.startswith('agent_engine'):
                continue
            archive.writestr('campuspulse_recovery/' + module, '# trusted build fixture\n')
    return target


def test_accepts_product_wheel_and_real_hashed_cloud_lock(tmp_path):
    from campuspulse_recovery.agent_engine.artifacts import inspect_artifacts
    product = wheel(tmp_path)
    result = inspect_artifacts(product, LOCK)
    assert result.version == '0.1.0'
    assert result.wheel_name == 'campuspulse_recovery-0.1.0-py3-none-any.whl'
    assert hashlib.sha256(result.wheel).digest() == hashlib.sha256(product.read_bytes()).digest()
    assert result.requirements == LOCK.read_bytes()


@pytest.mark.parametrize('cause', ['foreign_name', 'wrong_python', 'missing_runtime', 'corrupt_zip', 'wrong_filename'])
def test_rejects_unusable_wheel_before_bundle_creation(tmp_path, cause):
    from campuspulse_recovery.agent_engine.artifacts import inspect_artifacts
    product = wheel(tmp_path, name='another-project' if cause == 'foreign_name' else 'campuspulse-recovery',
                    python='>=3.13' if cause == 'wrong_python' else '>=3.12,<3.13',
                    missing=cause == 'missing_runtime')
    if cause == 'corrupt_zip':
        product.write_bytes(b'not-a-wheel')
    elif cause == 'wrong_filename':
        product = product.rename(tmp_path / 'arbitrary.whl')
    with pytest.raises(ValueError):
        inspect_artifacts(product, LOCK)


@pytest.mark.parametrize('cause', ['unpinned', 'no_hash', 'url', 'include', 'wrong_sdk', 'duplicate', 'truncated', 'empty'])
def test_rejects_unreproducible_or_unsupported_requirements(tmp_path, cause):
    from campuspulse_recovery.agent_engine.artifacts import inspect_artifacts
    data = LOCK.read_text()
    if cause == 'unpinned':
        data += '\nunsafe-package>=1\n'
    elif cause == 'no_hash':
        data += '\nunsafe-package==1.0\n'
    elif cause == 'url':
        data += '\nunsafe-package @ https://foreign.test/package.whl\n'
    elif cause == 'include':
        data += '\n-r /private/credentials\n'
    elif cause == 'wrong_sdk':
        data = data.replace('google-cloud-aiplatform==1.165.1', 'google-cloud-aiplatform==99.0.0')
    elif cause == 'duplicate':
        data += '\ncloudpickle==3.1.2 --hash=sha256:' + 'a' * 64 + '\n'
    elif cause == 'truncated':
        data += '\nunfinished==1.0 \\\n'
    else:
        data = '# no requirements\n'
    requirements = tmp_path / 'requirements.lock'
    requirements.write_text(data)
    with pytest.raises(ValueError):
        inspect_artifacts(wheel(tmp_path), requirements)
