"""Opt-in managed-agent transport. No eager SDK, ADC or default cloud target.

Stock async_stream_query does NOT establish CampusPulse run/approval authority.
Production must supply ManagedRuntimeProtocol over a separately authenticated
runtime binding channel. That channel is deferred; there is no fabricated stock
query/reconcile endpoint here. Model text/session state is never that channel.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextvars import ContextVar
import logging
import sys
from typing import Annotated, Literal, Protocol

from pydantic import Field, StrictInt, model_validator

from ..domain.models import (
    ApplyPlanInput, Contract, PlanReference, RepairResult, SolveRepairInput,
    canonical_json,
)
from ..observability.events import TechnicalId
from ..security.context import TrustedRequestContext

_PRIVATE_QUERY = ContextVar("campuspulse_private_query", default=False)


class _PrivateQueryFilter(logging.Filter):
    def filter(self, record):
        # SDK1.165.1's SSE parser logs malformed raw lines. Suppress that
        # specific logger only in this invocation's async context.
        return not _PRIVATE_QUERY.get()


class AgentIntent(Contract):
    intent_id: TechnicalId
    run_id: TechnicalId
    action: Literal["propose", "apply"]
    plan: PlanReference | None = None
    apply: ApplyPlanInput | None = None

    @model_validator(mode="after")
    def exact_intent(self):
        if self.action == "propose":
            if self.plan is not None or self.apply is not None:
                raise ValueError("proposal_has_no_apply_authority")
        elif (self.plan is None or self.apply is None or
              (self.plan.plan_id, self.plan.version) != (self.apply.plan_id, self.apply.version)):
            raise ValueError("exact_apply_reference_required")
        return self


class RemoteFrame(Contract):
    """Private protocol envelope, NOT a stock ADK text/event schema or proof."""
    run_id: TechnicalId
    intent_id: TechnicalId
    kind: Literal["progress", "terminal"]
    status: Literal["pending", "candidate", "no_change", "needs_input", "infeasible",
                    "partial", "unknown", "applied_unverified", "verification_failed",
                    "stale", "unavailable"]
    candidate_input: SolveRepairInput | None = None
    candidate: RepairResult | None = None

    @model_validator(mode="after")
    def closed_result(self):
        if self.kind == "progress":
            if self.status != "pending" or self.candidate_input is not None or self.candidate is not None:
                raise ValueError("invalid_progress")
        elif self.status == "pending":
            raise ValueError("terminal_pending")
        if self.status == "candidate":
            if self.candidate_input is None or self.candidate is None or self.kind != "terminal":
                raise ValueError("candidate_pair_required")
        elif self.candidate_input is not None or self.candidate is not None:
            raise ValueError("unexpected_candidate")
        return self


class StreamLimits(Contract):
    seconds: Annotated[float, Field(gt=0, le=180)] = 120.0
    chunks: Annotated[StrictInt, Field(ge=1, le=512)] = 128
    chunk_bytes: Annotated[StrictInt, Field(ge=128, le=800_000)] = 400_000
    total_bytes: Annotated[StrictInt, Field(ge=128, le=2_000_000)] = 1_000_000
    close_seconds: Annotated[float, Field(gt=0, le=5)] = 1.0


class RemoteClient(Protocol):
    def stream(self, context: TrustedRequestContext, intent: AgentIntent) -> AsyncIterator[RemoteFrame]: ...


async def consume(client: RemoteClient, context, intent, limits: StreamLimits, *, checkpoint) -> RemoteFrame:
    """Require one bounded terminal AND clean EOF; always close the iterator.

    A terminal followed by a disconnect is ambiguous, never confirmed success.
    Only normalized data is returned; no raw model text is persisted or logged.
    """
    stream = client.stream(context, intent)
    terminal = None
    count = size = 0
    try:
        async with asyncio.timeout(limits.seconds):
            async for raw in stream:
                await checkpoint()
                frame = RemoteFrame.model_validate(raw)
                count += 1
                length = len(canonical_json(frame).encode("utf-8"))
                size += length
                if count > limits.chunks or length > limits.chunk_bytes or size > limits.total_bytes:
                    raise ValueError("stream_budget_exceeded")
                if (frame.run_id, frame.intent_id) != (intent.run_id, intent.intent_id) or terminal is not None:
                    raise ValueError("stream_identity_or_order_mismatch")
                if frame.kind == "terminal":
                    terminal = frame
            await checkpoint()
            if terminal is None:
                raise ValueError("terminal_required")
            if intent.action == "apply" and terminal.status in ("candidate", "no_change", "needs_input", "infeasible"):
                raise ValueError("invalid_apply_terminal")
            return terminal
    finally:
        # No task survives the bounded HTTP invocation, even on cancellation.
        async with asyncio.timeout(limits.close_seconds):
            await stream.aclose()


class AgentEngineTarget(Contract):
    project_id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{4,61}[a-z0-9]$")]
    location: Annotated[str, Field(pattern=r"^[a-z]+-[a-z]+[0-9]+$")]
    resource_name: Annotated[str, Field(pattern=r"^projects/[0-9]+/locations/[a-z]+-[a-z]+[0-9]+/reasoningEngines/[0-9]+$")]

    @model_validator(mode="after")
    def same_location(self):
        if self.resource_name.split("/")[3] != self.location:
            raise ValueError("agent_location_mismatch")
        return self


class ManagedQuery(Contract):
    """Server protocol output. Session must already exist and be durably bound."""
    user_id: TechnicalId
    session_id: TechnicalId
    message: Annotated[str, Field(min_length=1, max_length=4096)]


class ManagedRuntimeProtocol(Protocol):
    async def prepare(self, context, intent: AgentIntent) -> ManagedQuery:
        """Authenticate backend platform identity and bind exact run + allowed
        plan/apply at runtime, using server-owned durable authority. Mandatory
        missing production integration; forwarded JSON is NOT sufficient.
        Must revalidate approval/source/session before granting apply dispatch.
        """
        ...

    def decode(self, context, intent: AgentIntent, raw: object) -> RemoteFrame:
        """Validate trusted bounded runtime envelope, never parse model text as proof."""
        ...


class PlatformAgentClient:
    """Lazy SDK1.165.1 explicit-target query; platform credentials stay backend.

    No retry, session-existence reconciliation, credential export or browser
    Vertex calls. Factory injection is for isolated FAKE SDK tests. Production
    target ownership/IAM and protocol provisioning require separate validation.
    """
    def __init__(self, target: AgentEngineTarget, *, protocol: ManagedRuntimeProtocol,
                 checkpoint, limits: StreamLimits | None = None, client_factory=None):
        if protocol is None or checkpoint is None:
            raise ValueError("runtime_authority_protocol_required")
        self.target = AgentEngineTarget.model_validate(target)
        self.protocol, self.checkpoint = protocol, checkpoint
        self.limits = limits or StreamLimits()
        self.client_factory = client_factory

    async def stream(self, context, intent):
        import json

        context.require_active()
        await self.checkpoint(context, intent)
        query = ManagedQuery.model_validate(await self.protocol.prepare(context, intent))
        await self.checkpoint(context, intent)
        if self.target.project_id != context.trusted.project_id:
            raise ValueError("agent_project_mismatch")
        if self.client_factory is None:
            import vertexai
            factory = vertexai.Client
        else:
            factory = self.client_factory
        # Client/get may perform SDK IO. They remain within the caller's bound;
        # never detach a thread whose RPC could outlive the HTTP invocation.
        http_options = {"timeout": max(1, int(self.limits.seconds * 1000)),
                        "retry_options": {"attempts": 1}}
        client = factory(project=self.target.project_id, location=self.target.location,
                         http_options=http_options)
        stream = None
        logger = logging.getLogger("vertexai_genai.agentengines")
        privacy = _PrivateQueryFilter()
        logger.addFilter(privacy)
        token = _PRIVATE_QUERY.set(True)
        try:
            remote = client.agent_engines.get(name=self.target.resource_name,
                                             config={"http_options": http_options})
            await self.checkpoint(context, intent)
            stream = remote.async_stream_query(message=query.message, user_id=query.user_id,
                session_id=query.session_id, run_config={"max_llm_calls": 8})
            count = size = 0
            async for raw in stream:
                await self.checkpoint(context, intent)
                count += 1
                length = len(json.dumps(raw, ensure_ascii=True, allow_nan=False).encode())
                size += length
                if count > self.limits.chunks or length > self.limits.chunk_bytes or size > self.limits.total_bytes:
                    raise ValueError("raw_stream_budget_exceeded")
                yield RemoteFrame.model_validate(self.protocol.decode(context, intent, raw))
        finally:
            # Each closer gets its own budget. Catch cancellation only while
            # attempting the remaining original resources, then propagate it.
            primary = sys.exception()
            cleanup_error = None
            try:
                closers = ([] if stream is None else [lambda: stream.aclose()])
                closers.append(lambda: client.aio.aclose())
                for close in closers:
                    try:
                        async with asyncio.timeout(self.limits.close_seconds):
                            await close()
                    except BaseException as error:
                        if cleanup_error is None or isinstance(error, asyncio.CancelledError):
                            cleanup_error = error
                # Vertex1.165.1 has no public sync Client.close(). This is the
                # same pinned per-request SDK HTTP client, not a substitute for
                # async cleanup. A synchronous close is not hard-clock proven.
                try:
                    client._api_client.close()
                except BaseException as error:
                    if cleanup_error is None or isinstance(error, asyncio.CancelledError):
                        cleanup_error = error
            finally:
                # Restore privacy even when any resource closer fails.
                try:
                    _PRIVATE_QUERY.reset(token)
                finally:
                    logger.removeFilter(privacy)
            if isinstance(primary, asyncio.CancelledError):
                raise primary
            if cleanup_error is not None:
                raise cleanup_error
