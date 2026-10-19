"""Prepare a private local handoff directory. Never creates a cloud resource."""
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import shutil

from ..cloud.settings import CloudSettings
from .artifacts import inspect_artifacts
from .runtime import AgentEngineRuntime


def _json(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')


def _write_private(path, data):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as target:
        target.write(data)
        target.flush()
        os.fsync(target.fileno())


def prepare_bundle(settings, *, wheel, requirements, output):
    """Validate everything before creating a new directory; manifest is last.

    Existing outputs are never replaced. A crash before the manifest leaves an
    incomplete directory, never a prepared receipt. Normal write failures clean
    up only the directory created by this call. Source bytes are read once.
    """
    parsed = CloudSettings.model_validate(settings)
    runtime = AgentEngineRuntime(parsed)
    artifacts = inspect_artifacts(wheel, requirements)
    for package, tested in {'google-cloud-aiplatform': '1.165.1', 'google-adk': '2.6.3',
                            'cloudpickle': '3.1.2'}.items():
        if version(package) != tested:
            raise ValueError('local_sdk_version_not_verified')
    from vertexai.agent_engines import _agent_engines as sdk
    from google.protobuf.json_format import MessageToDict
    schemas = sdk._generate_class_methods_spec_or_raise(
        agent_engine=runtime, operations=runtime.register_operations())
    if len(schemas) != 1 or schemas[0]['name'] != 'async_query':
        raise ValueError('runtime_operation_schema_required')
    config = {
        'display_name': 'campuspulse-recovery',
        'service_account': parsed.application.identities.agent_service_account,
        'requirements': 'requirements.lock',
        'extra_packages': [artifacts.wheel_name],
        # The method arguments include backend credentials. Keep default
        # platform telemetry off; journal events remain typed and redacted.
        'env_vars': {'GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY': 'false'},
    }
    files = {
        artifacts.wheel_name: artifacts.wheel,
        'requirements.lock': artifacts.requirements,
        'settings.json': _json(parsed.model_dump(mode='json')),
        'create-config.json': _json(config),
        'operations.json': _json([MessageToDict(schema) for schema in schemas]),
    }
    manifest = {
        'format_version': 1, 'status': 'prepared_not_deployed', 'cloud_calls': 0,
        'entrypoint': 'campuspulse_recovery.agent_engine.runtime.AgentEngineRuntime',
        'python_version': '3.12', 'sdk_version': '1.165.1',
        'project_id': parsed.application.project_id, 'location': parsed.application.agent_region,
        'product_version': artifacts.version,
        'files': {name: {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
                  for name, data in files.items()},
        'unverified': ['project_access', 'model_availability', 'runtime_identity_and_iam',
                       'mcp_reachability', 'actual_engine_resource', 'backend_sdk_transport',
                       'live_end_to_end_demo'],
    }
    output = Path(output)
    output.mkdir(mode=0o700)
    try:
        for name, data in files.items():
            _write_private(output / name, data)
        _write_private(output / 'manifest.json', _json(manifest))
    except BaseException:
        shutil.rmtree(output)
        raise
    return manifest
