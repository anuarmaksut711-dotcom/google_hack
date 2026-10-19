"""One actual ADK agent; local candidate only, no eager client/credential IO."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from google.adk.agents import Agent
from google.adk.agents.run_config import RunConfig
from google.adk.events import Event
from google.adk.models.google_llm import Gemini
from google.adk.tools import BaseTool
from google.adk.tools.mcp_tool.mcp_tool import McpTool

from .instructions import RECOVERY_INSTRUCTIONS
from .mcp_auth import RefreshingIdTokenHeaders
from .smoke_agent import SerializableReadMcpToolset, SmokeConfig
from .solve_tool import solve_repair_tool
from . import policy


class _BoundedRemoteTool(BaseTool):
    """Preserve actual MCP discovery/schema/transport; own bounded retry policy.

    ADK 2.6.3 decorates MCPTool._run_async_impl with one blind retry on Exception.
    Use that exact installed implementation without its retry wrapper. Reads get
    at most two counted attempts; apply and verify get one. In particular apply
    is NEVER blindly retried after an ambiguous dispatch. Upgrade requires QA.
    """
    def __init__(self, tool, timeout_seconds):
        super().__init__(name=tool.name, description=tool.description)
        self._tool = tool
        self._timeout_seconds = timeout_seconds

    def _get_declaration(self):
        return self._tool._get_declaration()

    async def run_async(self, *, args, tool_context):
        state = policy.current(tool_context)
        method = getattr(McpTool._run_async_impl, "__wrapped__", None)
        if method is None or not isinstance(self._tool, McpTool):
            return {"isError": True, "code": "unsupported_mcp_sdk"}
        for attempt in range(2 if self.name.startswith("read_") else 1):
            state.require_active(tool_context)
            if state.pending != self.name or state.remote_attempts >= policy.MAX_REMOTE_ATTEMPTS:
                return {"isError": True, "code": "remote_attempt_budget"}
            state.remote_attempts += 1  # Includes every actual SDK attempt.
            try:
                async with asyncio.timeout(self._timeout_seconds):
                    return await method(self._tool, args=args, tool_context=tool_context, credential=None)
            except asyncio.CancelledError:
                raise
            except Exception:
                if not self.name.startswith("read_") or attempt == 1:
                    return {"isError": True, "code": "transport_outcome_unconfirmed"}
        return {"isError": True, "code": "transport_outcome_unconfirmed"}


class RecoveryMcpToolset(SerializableReadMcpToolset):
    """T13 bearer-only transport/serialization recipe with the four fixed tools.

    Parent READ_TOOLS and read2 smoke builder remain byte-identical. __reduce__
    reconstructs type(self), restoring this full4 constructor before discovery.
    """
    def __init__(self, config, headers, local_transport=None):
        super().__init__(config, headers, local_transport)
        self.tool_filter = list(policy.REMOTE_TOOLS)

    async def get_tools(self, readonly_context=None):
        # Direct unauthenticated Runner usage must not reach discovery/tokens.
        policy.current(readonly_context)
        tools = await super().get_tools(readonly_context)
        if {t.name for t in tools} != set(policy.REMOTE_TOOLS):
            raise ValueError("exact_four_remote_tools_required")
        return [_BoundedRemoteTool(t, self._smoke_config.timeout_seconds) for t in tools]


def build_recovery_agent(config: SmokeConfig, *, headers=None, local_transport=None) -> Agent:
    """Construct Vertex Gemini lazily; actual cloud deployment remains deferred."""
    provider = headers if headers is not None else RefreshingIdTokenHeaders(
        config.mcp_audience, timeout_seconds=config.timeout_seconds)
    return Agent(name="campuspulse_recovery", model=Gemini(model=config.model,
        client_kwargs={"vertexai": True, "project": config.project_id, "location": config.vertex_location}),
        instruction=RECOVERY_INSTRUCTIONS,
        tools=[RecoveryMcpToolset(config, provider, local_transport), solve_repair_tool],
        before_agent_callback=policy.before_agent,
        before_model_callback=policy.before_model, after_model_callback=policy.after_model,
        before_tool_callback=policy.before_tool, after_tool_callback=policy.after_tool,
        on_tool_error_callback=policy.on_tool_error, on_model_error_callback=policy.on_model_error)


@dataclass
class RecoveryRun:
    """Backend receives candidate separately; this result confers no approval."""
    events: list
    status: str
    text: str
    candidate_input: object
    candidate: object
    observations: list


async def run_recovery(runner, *, new_message) -> RecoveryRun:
    """Execute inside backend_invocation; no identity from model/session state.

    Trusted startup owns runner/session creation and keeps request scope active.
    This wrapper fixes positive LLM/tool/attempt/deadline limits. Caller owns the
    runner and must close it in finally (including cancellation). No provider or
    capability is written to session state, state_delta or serialized Agent.
    """
    state = policy.current()
    events = []
    stream = runner.run_async(user_id=state.context.trusted.owner_id,
        session_id=state.context.session_id, new_message=new_message,
        run_config=RunConfig(max_llm_calls=policy.MAX_LLM_CALLS))
    try:
        async with asyncio.timeout(policy.RUN_TIMEOUT_SECONDS):
            async for event in stream:
                events.append(event)
    except asyncio.CancelledError:
        raise  # A dispatched apply remains unknown; never auto-reapply.
    except Exception:
        if state.status not in ("partial", "unknown", "verification_failed", "applied_unverified", "stale"):
            state.status = "unavailable"
        events.append(Event(author="campuspulse_recovery", content=policy.response(state).content))
    finally:
        await stream.aclose()
    return RecoveryRun(events, state.status, policy.final_text(state), state.candidate_input,
                       state.candidate, list(state.observations))
