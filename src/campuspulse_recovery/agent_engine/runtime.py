"""Custom managed agent that keeps the existing worker's authority checks."""
from contextlib import asynccontextmanager
import asyncio
import inspect
import os
import re

from ..cloud.settings import CloudSettings

_QUERY_SECONDS = 125
_MAX_RESPONSE_BYTES = 800_000


def _failure(status):
    code = 'service_or_invocation_rejected' if status == 403 else 'invocation_outcome_unconfirmed'
    return {'status_code': status, 'payload': {'error': code}}


@asynccontextmanager
async def _worker_app(settings):
    # Same credential policy as the cloud entrypoint; no credential files or
    # emulator override in the deployed runtime. Tests replace this I/O edge.
    if os.environ.get('FIRESTORE_EMULATOR_HOST') or os.environ.get('GOOGLE_APPLICATION_CREDENTIALS'):
        raise ValueError('cloud_platform_credentials_required')
    from google.cloud.firestore import AsyncClient
    from ..cloud.agent import create_agent
    from ..cloud.planning import assignments_resource
    from ..security.resources import ResourcePolicy
    from ..storage.plans import FirestoreDocuments
    policy = ResourcePolicy(settings.application, environment='cloud',
                            assignments_resource_id=assignments_resource(settings))
    client = AsyncClient(project=policy.config.project_id, database=policy.config.firestore_database)
    try:
        documents = FirestoreDocuments(client, project_id=policy.config.project_id,
                                      database_id=policy.config.firestore_database)
        yield create_agent(settings, policy, documents, close=lambda: None)
    finally:
        async with asyncio.timeout(5):
            closed = client.close()
            if inspect.isawaitable(closed):
                await closed


class AgentEngineRuntime:
    def __init__(self, settings):
        parsed = CloudSettings.model_validate(settings)
        if parsed.chat.provider != 'vertex':
            raise ValueError('agent_engine_requires_explicit_vertex_provider')
        self._settings_json = parsed.model_dump_json()

    def register_operations(self):
        return {'async': ['async_query']}

    def __reduce__(self):
        # Allowlist reconstruction. Even future transient attributes must not
        # carry clients, active request scopes or credential caches to storage.
        return (type(self), (CloudSettings.model_validate_json(self._settings_json).model_dump(),))

    async def async_query(self, invocation: str, service_token: str) -> dict:
        """Invoke one saved grant; the worker verifies the backend's signed token.

        Arguments are private backend credentials, never model/browser input.
        A platform OAuth token alone does not authorize this worker operation.
        """
        import httpx
        if (not isinstance(invocation, str) or re.fullmatch(r'[A-Za-z0-9_-]{43}', invocation) is None
                or not isinstance(service_token, str)
                or re.fullmatch(r'[A-Za-z0-9._-]{1,11993}', service_token) is None):
            return _failure(403)
        try:
            settings = CloudSettings.model_validate_json(self._settings_json)
            async with asyncio.timeout(_QUERY_SECONDS):
                async with _worker_app(settings) as app:
                    async with app.router.lifespan_context(app):
                        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                base_url=settings.agent_origin, trust_env=False) as client:
                            response = await client.post('/internal/invoke', json={}, headers={
                                'Authorization': 'Bearer ' + service_token,
                                'X-CampusPulse-Invocation': invocation})
                            if response.status_code == 403:
                                return _failure(403)
                            if (response.status_code != 200 or len(response.content) > _MAX_RESPONSE_BYTES
                                    or response.headers.get('content-type', '').split(';')[0] != 'application/json'):
                                return _failure(503)
                            value = response.json()
                            if not isinstance(value, dict):
                                return _failure(503)
                            return {'status_code': 200, 'payload': value}
        except asyncio.CancelledError:
            raise
        except Exception:
            # Do not echo exceptions: SDK/HTTP errors can contain private input.
            # An ambiguous write remains ambiguous; no automatic retry.
            return _failure(503)
