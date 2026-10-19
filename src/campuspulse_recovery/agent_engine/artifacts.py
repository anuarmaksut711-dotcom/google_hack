"""Bounded, offline inspection of the trusted product build and hashed lock."""
from dataclasses import dataclass
from email.parser import BytesParser
from io import BytesIO
from pathlib import Path
import re
from zipfile import BadZipFile, ZipFile

from packaging.specifiers import SpecifierSet
from packaging.version import Version


@dataclass(frozen=True)
class ArtifactInputs:
    wheel_name: str
    wheel: bytes
    requirements: bytes
    version: str


def _read(path, limit):
    with Path(path).open('rb') as source:
        data = source.read(limit + 1)
    if not data or len(data) > limit:
        raise ValueError('artifact_empty_or_too_large')
    return data


def _lock(data):
    pins, pending = {}, ''
    for raw in data.decode('utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        continued = line.endswith('\\')
        pending += ' ' + (line[:-1].strip() if continued else line)
        if continued:
            continue
        match = re.fullmatch(r'\s*([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9.!+_-]+)'
                             r'(?:\s+--hash=sha256:[0-9a-f]{64})+', pending)
        if match is None:
            raise ValueError('fully_pinned_hashed_cloud_lock_required')
        name = re.sub(r'[-_.]+', '-', match[1]).lower()
        if name in pins:
            raise ValueError('duplicate_requirement')
        pins[name] = str(Version(match[2]))
        pending = ''
    required = {'google-cloud-aiplatform', 'google-adk', 'cloudpickle', 'google-cloud-firestore',
                'pydantic', 'tzdata', 'fastapi', 'httpx', 'mcp', 'google-auth', 'google-genai',
                'google-api-python-client'}
    if pending or not required <= pins.keys():
        raise ValueError('complete_cloud_lock_required')
    for name, version in {'google-cloud-aiplatform': '1.165.1', 'google-adk': '2.6.3',
                          'cloudpickle': '3.1.2'}.items():
        if pins[name] != version:
            raise ValueError('runtime_sdk_version_not_verified')


def inspect_artifacts(wheel, requirements):
    """Read once, validate structure, then hand immutable bytes to preparation.

    These are trusted local build inputs, not an untrusted package sandbox.
    No wheel code or pickle is loaded or executed by this inspector.
    """
    wheel = Path(wheel)
    filename = re.fullmatch(r'campuspulse_recovery-([0-9]+(?:\.[0-9]+)*)-py3-none-any\.whl', wheel.name)
    if filename is None:
        raise ValueError('portable_product_wheel_required')
    product, lock = _read(wheel, 32_000_000), _read(requirements, 2_000_000)
    _lock(lock)
    try:
        with ZipFile(BytesIO(product)) as archive:
            entries = archive.infolist()
            names = {entry.filename for entry in entries}
            if (len(names) != len(entries) or sum(entry.file_size for entry in entries) > 100_000_000
                    or any(entry.file_size > 30_000_000 or '..' in entry.filename.split('/')
                           or entry.filename.startswith('/') for entry in entries)):
                raise ValueError('invalid_wheel_archive')
            required = {'campuspulse_recovery/' + path for path in
                        ('agent_engine/runtime.py', 'cloud/agent.py', 'cloud/authority.py')}
            if not required <= names or any(archive.getinfo(path).file_size == 0 for path in required):
                raise ValueError('runtime_missing_from_wheel')
            metadata_path = f'campuspulse_recovery-{filename[1]}.dist-info/METADATA'
            if metadata_path not in names or archive.getinfo(metadata_path).file_size > 64_000:
                raise ValueError('product_wheel_metadata_required')
            meta = BytesParser().parsebytes(archive.read(metadata_path), headersonly=True)
            if (re.sub(r'[-_.]+', '-', meta.get('Name', '')).lower() != 'campuspulse-recovery'
                    or meta.get('Version') != filename[1]
                    or SpecifierSet(meta.get('Requires-Python', '')) != SpecifierSet('>=3.12,<3.13')):
                raise ValueError('product_wheel_metadata_mismatch')
            if archive.testzip() is not None:
                raise ValueError('corrupt_wheel_archive')
    except BadZipFile:
        raise ValueError('invalid_product_wheel') from None
    return ArtifactInputs(wheel.name, product, lock, filename[1])
