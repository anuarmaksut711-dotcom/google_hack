"""Explicit production settings and server-owned invocation bindings."""
import json
import os
import re
from datetime import UTC, datetime

from ..config import AppConfig
from ..security.context import AccessDenied, AuthenticatedCaller, ServerRunBinding


def settings():
    value = json.loads(os.environ['CAMPUSPULSE_LIVE_SETTINGS'])
    config = AppConfig.model_validate(value['app_config'])
    config.require_environment('cloud')
    return value, config


def sync_client(config):
    from google.cloud.firestore import Client
    return Client(project=config.project_id, database=config.firestore_database)


def invocation(client, key, config):
    if not isinstance(key, str) or not re.fullmatch(r'inv_[a-f0-9]{32}', key):
        raise AccessDenied('invalid_invocation')
    snapshot = client.document('cp_cloud_invocations/' + key).get(timeout=8)
    value = snapshot.to_dict() if snapshot.exists else None
    if not value or value.get('config_revision') != config.config_revision:
        raise AccessDenied('missing_invocation')
    binding = ServerRunBinding.model_validate_json(value['binding'])
    now = datetime.now(UTC)
    if (binding.expires_at <= now or binding.owner_id != config.student.trusted_user_id
            or binding.signin_principal_id != config.student.allowed_signin_principal
            or binding.config_revision != config.config_revision):
        raise AccessDenied('expired_invocation')
    return value, binding


class Bindings:
    def __init__(self, binding):
        self.binding = binding

    def resolve(self, *, session_id, run_id):
        b = self.binding
        return b if (session_id, run_id) == (b.session_id, b.run_id) else None


class AgentCaller:
    """Called only inside the IAM-protected Agent Engine query entrypoint.

    Invocation records are written by the authenticated application backend.
    Their unguessable keys are an additional binding, never a model argument.
    """
    def __init__(self, config, binding):
        self.config, self.binding = config, binding

    def authenticate(self, credential, *, boundary, now):
        if credential is not self or boundary != 'agent':
            raise AccessDenied('managed_entrypoint_required')
        b = self.binding
        return AuthenticatedCaller(principal_id=self.config.identities.backend_service_account,
            session_id=b.session_id, run_id=b.run_id, authenticated_at=now, expires_at=b.expires_at)


class NoUncertainTakeover:
    async def require_stopped(self, *args, **kwargs):
        from ..domain.ports import BindingConflict
        raise BindingConflict('uncertain_writer_requires_operator_reconciliation')
