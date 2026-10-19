"""Provider-neutral duplex contract. No SDK imports or production fallback.

W05 owns local ADK development; W06 owns managed production. The local fake is
an explicitly selected test implementation, never an authenticated cloud path.
All adapter coroutines (including tool work) must acknowledge cancellation and
must not launch detached tasks. receive and send run concurrently; one caller
serializes each direction. Tokens/PCM/tool data must never be logged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol

from .device_auth import VoiceAccess
from .protocol import AudioFrame, Input, Mute, ServerMessage
from .sessions import ResumeState, StreamLease


class ProviderMode(StrEnum):
    LOCAL_FAKE = "local_fake"
    LOCAL_ADK_DEVELOPMENT = "local_adk_development"
    MANAGED_PRODUCTION = "managed_production"


@dataclass(frozen=True)
class ResponseBoundary:
    generation: int
    phase: Literal["started", "completed", "interrupted"]


@dataclass(frozen=True)
class OutputAudio:
    generation: int
    pcm: bytes = field(repr=False)


@dataclass(frozen=True)
class OutputTranscript:
    generation: int
    transcript_id: str
    speaker: Literal["user", "assistant"]
    revision: int
    final: bool
    text: str = field(repr=False)


@dataclass(frozen=True)
class ResumeUpdate:
    token: str = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True)
class ToolRequest:
    call_id: str
    tool: str
    arguments: dict = field(repr=False)


@dataclass(frozen=True)
class ToolResult:
    status: Literal["success", "failed", "unavailable"]
    value: object = field(default=None, repr=False)


UpstreamEvent = ResponseBoundary | OutputAudio | OutputTranscript | ResumeUpdate | ToolRequest
InputPacket = AudioFrame | Input | Mute
OutputPacket = AudioFrame | ServerMessage


class ClientTransport(Protocol):
    async def receive(self) -> str | bytes | None:
        """Complete W02 text/binary message, or None on disconnect; bounded read."""
        ...

    async def send(self, packet: OutputPacket) -> None: ...
    async def close(self) -> None: ...


class BidiSession(Protocol):
    async def send(self, packet: InputPacket) -> None: ...
    async def receive(self) -> UpstreamEvent | None: ...
    async def tool_result(self, call_id: str, result: ToolResult) -> None: ...
    async def cancel(self) -> None:
        """Propagate terminal cancellation to model and all tools; no replay."""
        ...
    async def close(self) -> None: ...


class BidiProvider(Protocol):
    mode: ProviderMode

    async def open(self, lease: StreamLease, resume: ResumeState) -> BidiSession:
        """Transport context is NOT remote authentication or plan approval.

        Managed adapters require platform IAM plus independent durable binding
        validation. Rebuild native SDK state from bounded untrusted text/token;
        never replay audio, pending tools or model-generated resource claims.
        Return a fresh connection-owned handle; close partially opened resources
        if this coroutine is cancelled before returning that handle. Serialize
        SDK writes internally when send() and tool_result() run concurrently.
        """
        ...


class ReadToolHandler(Protocol):
    async def execute(self, access: VoiceAccess, request: ToolRequest) -> ToolResult:
        """Injected server handler; independently guards configured resources.

        W04 checks the durable fence/name immediately before dispatch. W05/MCP
        still enforce arguments, resources and service identity independently.
        """
        ...


def require_provider_mode(provider: BidiProvider, *, production: bool, selected: ProviderMode):
    if (not isinstance(selected, ProviderMode) or provider.mode != selected
            or (production and selected != ProviderMode.MANAGED_PRODUCTION)):
        raise ValueError("voice_provider_mode_rejected")
