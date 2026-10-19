"""Bounded, no-retry Cloud Run and MCP calls with separate IAM and run grants."""
import asyncio
import json

import httpx

from ..agent.mcp_auth import RefreshingIdTokenHeaders
from ..domain.ports import PortError
from .authority import grant_path


class CloudTransport:
    def __init__(self, origin):
        self.origin = origin
        self.tokens = RefreshingIdTokenHeaders(origin)

    async def headers(self, reference):
        grant_path(reference)  # Validate before putting a reference in a header.
        values = await self.tokens(self)
        # Cloud Run consumes X-Serverless-Authorization; application verification
        # receives the original signed Authorization token.
        return {**values, 'X-Serverless-Authorization': values['Authorization'],
                'X-CampusPulse-Invocation': reference}

    async def request(self, reference, *, path='/internal/invoke', seconds=150):
        try:
            headers = await self.headers(reference)
            async with asyncio.timeout(seconds):
                async with httpx.AsyncClient(timeout=httpx.Timeout(seconds, connect=5),
                                             trust_env=False, follow_redirects=False) as client:
                    async with client.stream('POST', self.origin + path, headers=headers, json={}) as response:
                        if response.status_code != 200:
                            raise PortError('cloud_invocation_unavailable')
                        body = bytearray()
                        async for part in response.aiter_bytes():
                            body.extend(part)
                            if len(body) > 800_000:
                                raise PortError('cloud_response_too_large')
                        return json.loads(body)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise PortError('cloud_invocation_outcome_unconfirmed') from None


class McpTransport(CloudTransport):
    async def call(self, reference, name, arguments):
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        try:
            headers = await self.headers(reference)
            async with asyncio.timeout(110):
                async with httpx.AsyncClient(headers=headers, trust_env=False,
                    timeout=httpx.Timeout(110, connect=5), follow_redirects=False) as client:
                    async with streamable_http_client(self.origin + '/mcp', http_client=client) as (read, write, _):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            result = await session.call_tool(name, arguments)
            value = result.model_dump(mode='json')
            if len(json.dumps(value, ensure_ascii=False).encode('utf-8')) > 800_000:
                raise PortError('mcp_response_too_large')
            return value
        except asyncio.CancelledError:
            raise
        except Exception:
            raise PortError('mcp_outcome_unconfirmed') from None


class MoodleRefreshTransport:
    """Separate read-only sync authority; no browser-session invocation grant.

    Scheduler and backend can only trigger the account fixed in worker settings.
    Neither tokens, account selectors, grades nor LMS URLs travel in this call.
    """
    def __init__(self, origin):
        self.origin = origin
        self.tokens = RefreshingIdTokenHeaders(origin)

    async def refresh(self):
        try:
            async with asyncio.timeout(100):
                values = await self.tokens(self)
                headers = {**values, 'X-Serverless-Authorization': values['Authorization']}
                async with httpx.AsyncClient(timeout=httpx.Timeout(95, connect=5),
                        trust_env=False, follow_redirects=False) as client:
                    async with client.stream('POST', self.origin + '/internal/moodle/sync',
                            headers=headers, json={'force': True}) as response:
                        data = bytearray()
                        async for chunk in response.aiter_bytes():
                            data.extend(chunk)
                            if len(data) > 8192:
                                raise PortError('moodle_sync_response_invalid')
                        if response.status_code != 200:
                            raise PortError('moodle_sync_unavailable')
                        value = json.loads(data)
                        if not isinstance(value, dict):
                            raise PortError('moodle_sync_response_invalid')
        except asyncio.CancelledError:
            raise
        except Exception:
            raise PortError('moodle_sync_unavailable') from None
