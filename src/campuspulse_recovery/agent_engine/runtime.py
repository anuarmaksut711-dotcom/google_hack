"""Custom managed agent that keeps the existing worker's authority checks."""
from contextlib import asynccontextmanager
import inspect
import os

from ..cloud.settings import CloudSettings


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

    async def async_query(self, invocation: str, service_token: str) -> dict:
        """Invoke one saved grant; the worker verifies the backend's signed token.

        Arguments are private backend credentials, never model/browser input.
        A platform OAuth token alone does not authorize this worker operation.
        """
        import httpx
        settings = CloudSettings.model_validate_json(self._settings_json)
        async with _worker_app(settings) as app:
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                        base_url=settings.agent_origin, trust_env=False) as client:
                    response = await client.post('/internal/invoke', json={}, headers={
                        'Authorization': 'Bearer ' + service_token,
                        'X-CampusPulse-Invocation': invocation})
                    return {'status_code': response.status_code, 'payload': response.json()}
