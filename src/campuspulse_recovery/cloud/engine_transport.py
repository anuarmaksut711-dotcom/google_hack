"""Private custom-operation transport; no stock chat protocol or write retries."""
import asyncio
import json
import logging
import sys

from ..agent.mcp_auth import RefreshingIdTokenHeaders, cloud_run_audience
from ..domain.ports import PortError
from ..web.agent_client import AgentEngineTarget, _PRIVATE_QUERY, _PrivateQueryFilter
from .authority import grant_path


class AgentEngineTransport:
    def __init__(self, target, *, audience, token_factory=None, client_factory=None):
        self.target = AgentEngineTarget.model_validate(target)
        self.audience = cloud_run_audience(audience)
        self.tokens = token_factory or RefreshingIdTokenHeaders(self.audience)
        self.client_factory = client_factory

    async def request(self, reference, *, seconds=150):
        grant_path(reference)
        if not 0 < seconds <= 150:
            raise ValueError('invalid_query_budget')
        client = None
        logger = logging.getLogger('vertexai_genai.agentengines')
        privacy = _PrivateQueryFilter()
        logger.addFilter(privacy)
        private = _PRIVATE_QUERY.set(True)
        try:
            async with asyncio.timeout(seconds):
                values = await self.tokens(self)
                authorization = values.get('Authorization', '')
                if not authorization.startswith('Bearer ') or not authorization[7:]:
                    raise ValueError('service_token_required')
                if self.client_factory is None:
                    import vertexai
                    factory = vertexai.Client
                else:
                    factory = self.client_factory
                options = {'timeout': max(1, int(seconds * 1000)), 'retry_options': {'attempts': 1}}
                client = factory(project=self.target.project_id, location=self.target.location,
                                 http_options=options)
                remote = await asyncio.to_thread(client.agent_engines.get,
                    name=self.target.resource_name, config={'http_options': options})
                response = await remote.async_query(invocation=reference, service_token=authorization[7:])
                if (not isinstance(response, dict) or set(response) != {'status_code', 'payload'}
                        or type(response['status_code']) is not int or response['status_code'] != 200
                        or not isinstance(response['payload'], dict)
                        or len(json.dumps(response, ensure_ascii=True, allow_nan=False).encode()) > 800_000):
                    raise ValueError('invalid_custom_operation_response')
                return response['payload']
        except asyncio.CancelledError:
            raise
        except Exception:
            raise PortError('agent_engine_outcome_unconfirmed') from None
        finally:
            primary = sys.exception()
            cleanup_error = None
            try:
                if client is not None:
                    try:
                        async with asyncio.timeout(3):
                            await client.aio.aclose()
                    except BaseException as error:
                        cleanup_error = error
                    try:
                        client._api_client.close()
                    except BaseException as error:
                        if cleanup_error is None or isinstance(error, asyncio.CancelledError):
                            cleanup_error = error
            finally:
                _PRIVATE_QUERY.reset(private)
                logger.removeFilter(privacy)
            if isinstance(primary, asyncio.CancelledError):
                raise primary
            if isinstance(cleanup_error, asyncio.CancelledError):
                raise cleanup_error
            if cleanup_error is not None and primary is None:
                raise PortError('agent_engine_outcome_unconfirmed') from None
