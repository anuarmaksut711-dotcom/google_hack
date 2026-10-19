"""Exercise a real pinned MCP log path with synthetic private data."""
import asyncio
import logging

from mcp.client.streamable_http import StreamableHTTPTransport
from mcp.types import JSONRPCMessage, JSONRPCResponse

from tests.agent_engine.test_runtime import install_app, settings, no_cloud


def malformed_mcp_reply():
    transport = StreamableHTTPTransport('https://unused.invalid/mcp')
    transport._maybe_extract_protocol_version_from_message(JSONRPCMessage(root=JSONRPCResponse(
        jsonrpc='2.0', id=1, result={'private_marker': 'synthetic-private-calendar-content'})))


def test_runtime_suppresses_actual_mcp_private_logs_permanently(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    # Establish that this SDK path would expose the marker without the policy.
    malformed_mcp_reply()
    assert 'synthetic-private-calendar-content' in caplog.text
    caplog.clear()
    async def handler():
        malformed_mcp_reply()
        return {'status': 'needs_input'}
    closed = []
    runtime = install_app(monkeypatch, handler, closed)
    result = asyncio.run(runtime.async_query('a' * 43, 'synthetic-token'))
    assert result['status_code'] == 200 and closed == [True]
    malformed_mcp_reply()  # No temporary global toggle restored after request.
    assert 'synthetic-private-calendar-content' not in caplog.text


def test_platform_setup_hook_installs_policy_before_query(caplog):
    from campuspulse_recovery.agent_engine.runtime import AgentEngineRuntime
    caplog.set_level(logging.DEBUG)
    runtime = AgentEngineRuntime(settings())
    runtime.set_up()
    malformed_mcp_reply()
    assert 'synthetic-private-calendar-content' not in caplog.text
