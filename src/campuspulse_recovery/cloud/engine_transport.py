"""Private custom-operation transport; no stock chat protocol or write retries."""
import asyncio
import json
import logging
import re
import sys

from ..agent.mcp_auth import RefreshingIdTokenHeaders, cloud_run_audience
from ..domain.ports import PortError
from ..web.agent_client import AgentEngineTarget, _PRIVATE_QUERY, _PrivateQueryFilter
from .authority import grant_path

MAX_RESPONSE_BYTES = 800_000


async def _platform_access_token(seconds):
    """Discover and refresh platform ADC entirely in a bounded worker thread."""
    def acquire():
        import google.auth
        import requests
        from google.auth.transport.requests import Request

        class BoundedRequest(Request):
            def __call__(self, *args, **kwargs):
                kwargs['timeout'] = min(5, seconds)
                return super().__call__(*args, **kwargs)

        with requests.Session() as session:
            request = BoundedRequest(session=session)
            credentials, _ = google.auth.default(
                scopes=['https://www.googleapis.com/auth/cloud-platform'], request=request)
            if not credentials.valid or not credentials.token:
                credentials.refresh(request)
            return credentials.token
    async with asyncio.timeout(min(6, seconds)):
        return await asyncio.to_thread(acquire)


def _payload(response):
    if (not isinstance(response, dict) or set(response) != {'status_code', 'payload'}
            or type(response['status_code']) is not int or response['status_code'] != 200
            or not isinstance(response['payload'], dict)
            or len(json.dumps(response, ensure_ascii=True, allow_nan=False).encode()) > MAX_RESPONSE_BYTES):
        raise ValueError('invalid_custom_operation_response')
    return response['payload']


class AgentEngineTransport:
    def __init__(self, target, *, audience, token_factory=None, client_factory=None,
                 access_token_factory=None, http_client_factory=None):
        self.target = AgentEngineTarget.model_validate(target)
        self.audience = cloud_run_audience(audience)
        self.tokens = token_factory or RefreshingIdTokenHeaders(self.audience)
        self.client_factory = client_factory
        self.access_tokens = access_token_factory or _platform_access_token
        self.http_client_factory = http_client_factory

    async def _rest_query(self, reference, service_token, seconds):
        """Call the documented custom operation directly, without SDK discovery.

        The Vertex SDK's lazy Agent Engines import scans installed package
        metadata synchronously. Doing that in a request can stall every route.
        This fixed REST endpoint needs no model/schema discovery at runtime.
        """
        import httpx
        access_token = await self.access_tokens(seconds)
        if not isinstance(access_token, str) or re.fullmatch(r'[A-Za-z0-9._~+/-]{1,12000}', access_token) is None:
            raise ValueError('platform_access_token_required')
        url = (f'https://{self.target.location}-aiplatform.googleapis.com/v1/'
               f'{self.target.resource_name}:query')
        factory = self.http_client_factory or httpx.AsyncClient
        async with factory(headers={'Authorization': 'Bearer ' + access_token},
                timeout=httpx.Timeout(seconds, connect=min(10, seconds)),
                trust_env=False, follow_redirects=False) as client:
            async with client.stream('POST', url, json={'class_method': 'async_query',
                    'input': {'invocation': reference, 'service_token': service_token}}) as response:
                if not 200 <= response.status_code < 300:
                    raise ValueError('agent_engine_http_rejected')
                length = response.headers.get('content-length')
                if length is not None and (not length.isascii() or not length.isdigit()
                        or int(length) > MAX_RESPONSE_BYTES):
                    raise ValueError('agent_engine_reply_too_large')
                content = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise ValueError('agent_engine_reply_too_large')
                    content.extend(chunk)
        result = json.loads(content.decode('utf-8'))
        if not isinstance(result, dict) or set(result) != {'output'}:
            raise ValueError('agent_engine_envelope_invalid')
        return _payload(result['output'])

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
                if (not isinstance(authorization, str) or not authorization.startswith('Bearer ')
                        or re.fullmatch(r'[A-Za-z0-9._-]{1,11993}', authorization[7:]) is None):
                    raise ValueError('service_token_required')
                if self.client_factory is None:
                    return await self._rest_query(reference, authorization[7:], seconds)
                # Explicit SDK adapter is retained for injected integration
                # clients only. Production never imports or discovers Vertex SDK.
                options = {'timeout': max(1, int(seconds * 1000)), 'retry_options': {'attempts': 1}}
                client = await asyncio.to_thread(self.client_factory, project=self.target.project_id,
                    location=self.target.location, http_options=options)
                remote = await asyncio.to_thread(lambda: client.agent_engines.get(
                    name=self.target.resource_name, config={'http_options': options}))
                response = await remote.async_query(invocation=reference, service_token=authorization[7:])
                return _payload(response)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Transport cleanup may itself fail while a caller cancellation is
            # unwinding. That must not turn cancellation into a retryable error.
            if asyncio.current_task().cancelling():
                raise asyncio.CancelledError from None
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
