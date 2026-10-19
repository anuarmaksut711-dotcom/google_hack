"""Voice v1 codec and connection-local validators; see docs/voice/protocol.md.

These values are NOT authentication, approval, durable leases or tool policy.
The caller serializes access, authorizes independently and bounds all queues.
"""

from __future__ import annotations

import json
import math
import struct
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel, ConfigDict, Field, StrictBool, StrictInt, StringConstraints,
    TypeAdapter, ValidationError,
)

VERSION = "1"
UINT32_MAX = 0xFFFFFFFF
HEADER = struct.Struct("!4sBBHIIII")
HEADER_SIZE = 24
MAX_CONTROL_BYTES = 2048
MAX_EVENT_BYTES = 32768
MAX_SESSION_MS = 540000
START_TIMEOUT_MS = 5000
CLOSE_TIMEOUT_MS = 2000
HEARTBEAT_INTERVAL_MS = 10000
HEARTBEAT_TIMEOUT_MS = 30000
IDLE_TIMEOUT_MS = 120000
MAX_QUEUED_INPUT_PCM = 6400
MAX_QUEUED_OUTPUT_PCM = 9600
MAX_QUEUED_EVENTS = 32
INPUT_RATE = 16000
OUTPUT_RATE = 24000
INPUT_FORMAT = "pcm_s16le_16000_mono"
OUTPUT_FORMAT = "pcm_s16le_24000_mono"
MAX_INPUT_PCM = 1280
MAX_OUTPUT_PCM = 1920

U32 = Annotated[StrictInt, Field(ge=0, le=UINT32_MAX)]
PositiveU32 = Annotated[StrictInt, Field(ge=1, le=UINT32_MAX)]
WireId = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9_-]{1,64}$")]
Text = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=4096)]
Millis = Annotated[StrictInt, Field(ge=0, le=MAX_SESSION_MS)]
ReadTool = Literal["read_assignments", "read_calendar", "solve_repair"]
ErrorCode = Literal[
    "invalid_json", "unsupported_version", "unknown_event", "invalid_message",
    "message_too_large", "invalid_frame", "invalid_length", "invalid_format",
    "stale_generation", "invalid_sequence", "invalid_state", "rate_limited",
    "limit_exceeded", "access_denied", "access_expired", "revoked",
    "upstream_unavailable", "timeout", "backpressure", "internal_error",
]


class ProtocolError(ValueError):
    """Only a closed, public code is safe to send/log; never include input."""

    def __init__(self, code: ErrorCode):
        if code not in ERROR_CODES:
            code = "internal_error"
        self.code = code
        super().__init__(code)



ERROR_CODES = frozenset(ErrorCode.__args__)


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, revalidate_instances="always")
    version: Literal["1"]


class ClientMessage(Message):
    request_id: WireId


class Start(ClientMessage):
    type: Literal["start"]
    input_format: Literal["pcm_s16le_16000_mono"]
    output_format: Literal["pcm_s16le_24000_mono"]


class BoundControl(ClientMessage):
    connection_generation: PositiveU32


class Input(BoundControl):
    type: Literal["input"]
    action: Literal["begin", "end"]


class Mute(BoundControl):
    type: Literal["mute"]
    muted: StrictBool


class Stop(BoundControl):
    type: Literal["stop"]


class Heartbeat(BoundControl):
    type: Literal["heartbeat"]


ClientEvent = Annotated[Start | Input | Mute | Stop | Heartbeat, Field(discriminator="type")]


class ServerMessage(Message):
    event_id: WireId
    event_sequence: U32
    connection_generation: PositiveU32
    elapsed_ms: Millis


class Ready(ServerMessage):
    type: Literal["ready"]
    session_id: WireId
    run_id: WireId
    expires_in_ms: Annotated[StrictInt, Field(ge=1, le=MAX_SESSION_MS)]
    input_format: Literal["pcm_s16le_16000_mono"]
    output_format: Literal["pcm_s16le_24000_mono"]


class Generation(ServerMessage):
    type: Literal["generation"]
    generation: PositiveU32
    phase: Literal["started", "completed"]


class Interrupted(ServerMessage):
    type: Literal["interrupted"]
    generation: PositiveU32


class Transcript(ServerMessage):
    type: Literal["transcript"]
    generation: U32
    transcript_id: WireId
    speaker: Literal["user", "assistant"]
    revision: U32
    final: StrictBool
    text: Text


class ToolStarted(ServerMessage):
    type: Literal["tool_started"]
    call_id: WireId
    tool: ReadTool


class ToolFinished(ServerMessage):
    type: Literal["tool_finished"]
    call_id: WireId
    tool: ReadTool
    status: Literal["success", "failed", "unavailable", "cancelled"]
    duration_ms: Millis


class HeartbeatAck(ServerMessage):
    type: Literal["heartbeat_ack"]
    request_id: WireId


class Error(ServerMessage):
    type: Literal["error"]
    code: ErrorCode
    fatal: StrictBool


class Closed(ServerMessage):
    type: Literal["closed"]
    reason: Literal[
        "stopped", "expired", "revoked", "disconnected", "timeout",
        "upstream_error", "protocol_error", "backpressure",
    ]


ServerEvent = Annotated[
    Ready | Generation | Interrupted | Transcript | ToolStarted | ToolFinished |
    HeartbeatAck | Error | Closed,
    Field(discriminator="type"),
]
CLIENT_ADAPTER = TypeAdapter(ClientEvent)
SERVER_ADAPTER = TypeAdapter(ServerEvent)


def _object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("invalid_json")
        result[key] = value
    return result


def _constant(_: str) -> None:
    raise ProtocolError("invalid_json")


def _validate(data: object, *, client: bool) -> ClientEvent | ServerEvent:
    if not isinstance(data, dict):
        raise ProtocolError("invalid_message")
    if data.get("version") != VERSION:
        raise ProtocolError("unsupported_version")
    adapter = CLIENT_ADAPTER if client else SERVER_ADAPTER
    known = {"start", "input", "mute", "stop", "heartbeat"} if client else {
        "ready", "generation", "interrupted", "transcript", "tool_started",
        "tool_finished", "heartbeat_ack", "error", "closed",
    }
    if not isinstance(data.get("type"), str) or data["type"] not in known:
        raise ProtocolError("unknown_event")
    failed = False
    try:
        value = adapter.validate_python(data)
    except ValidationError:
        failed = True
    if failed:

        raise ProtocolError("invalid_message")
    return value


def parse_json(payload: str | bytes, *, client: bool) -> ClientEvent | ServerEvent:
    """One complete WebSocket TEXT message. bytes means UTF-8 text, never PCM."""
    if type(payload) not in {str, bytes}:
        raise ProtocolError("invalid_json")
    failed = False
    try:
        size = len(payload.encode("utf-8")) if isinstance(payload, str) else len(payload)
    except UnicodeError:
        failed = True
    if failed:
        raise ProtocolError("invalid_json")
    if size > (MAX_CONTROL_BYTES if client else MAX_EVENT_BYTES):
        raise ProtocolError("message_too_large")
    try:
        decoded = payload.decode("utf-8") if isinstance(payload, bytes) else payload
        data = json.loads(decoded, object_pairs_hook=_object, parse_constant=_constant)

        json.dumps(data, ensure_ascii=False).encode("utf-8")
    except (ValueError, UnicodeError, RecursionError):
        failed = True
    if failed:
        raise ProtocolError("invalid_json")
    return _validate(data, client=client)


def encode_json(event: ClientMessage | ServerMessage, *, client: bool) -> str:
    if not isinstance(event, Message):
        raise ProtocolError("invalid_message")
    validated = _validate(event.model_dump(mode="python", warnings=False), client=client)
    result = json.dumps(validated.model_dump(mode="json"), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    parse_json(result, client=client)
    return result


@dataclass(frozen=True)
class AudioFrame:
    kind: Literal["input", "output"]
    connection_generation: int
    generation: int
    sequence: int
    pcm: bytes


def _u32(value: int, *, positive: bool = False) -> None:
    if type(value) is not int or not (int(positive) <= value <= UINT32_MAX):
        raise ProtocolError("invalid_frame")


def _frame(frame: AudioFrame) -> None:
    if frame.kind not in {"input", "output"}:
        raise ProtocolError("invalid_frame")
    _u32(frame.connection_generation, positive=True)
    _u32(frame.generation)
    _u32(frame.sequence)
    if (frame.kind == "input" and frame.generation != 0) or (frame.kind == "output" and frame.generation == 0):
        raise ProtocolError("invalid_frame")
    maximum = MAX_INPUT_PCM if frame.kind == "input" else MAX_OUTPUT_PCM
    if type(frame.pcm) is not bytes or not 2 <= len(frame.pcm) <= maximum or len(frame.pcm) % 2:
        raise ProtocolError("invalid_length")


def encode_audio(frame: AudioFrame) -> bytes:
    _frame(frame)
    return HEADER.pack(
        b"CPVA", 1, 1 if frame.kind == "input" else 2, HEADER_SIZE,
        frame.connection_generation, frame.generation, frame.sequence, len(frame.pcm),
    ) + frame.pcm


def decode_audio(payload: bytes, *, expected_kind: Literal["input", "output"] | None = None) -> AudioFrame:
    if type(payload) is not bytes or not HEADER_SIZE <= len(payload) <= HEADER_SIZE + MAX_OUTPUT_PCM:
        raise ProtocolError("invalid_length")
    magic, version, kind, header_size, connection, generation, sequence, length = HEADER.unpack_from(payload)
    if magic != b"CPVA" or header_size != HEADER_SIZE or kind not in {1, 2}:
        raise ProtocolError("invalid_frame")
    if version != 1:
        raise ProtocolError("unsupported_version")
    if length != len(payload) - HEADER_SIZE:
        raise ProtocolError("invalid_length")
    frame = AudioFrame("input" if kind == 1 else "output", connection, generation, sequence, payload[HEADER_SIZE:])
    _frame(frame)
    if expected_kind is not None and frame.kind != expected_kind:
        raise ProtocolError("invalid_frame")
    return frame


class _Bucket:
    def __init__(self, rate: float, capacity: float):
        self.rate, self.capacity = rate, capacity
        self.tokens, self.last = capacity, None

    def available(self, amount: int, now: float) -> float:
        if type(now) not in {int, float} or not math.isfinite(now) or now < 0 or (self.last is not None and now < self.last):
            raise ProtocolError("invalid_state")
        tokens = min(self.capacity, self.tokens + (0 if self.last is None else now - self.last) * self.rate)
        if tokens + 1e-7 < amount:
            raise ProtocolError("rate_limited")
        return tokens - amount

    def commit(self, tokens: float, now: float) -> None:
        self.tokens, self.last = tokens, now


class AudioBudget:
    """Per direction, per connection; never reset on interruption or mute."""

    def __init__(self, kind: Literal["input", "output"]):
        if kind not in {"input", "output"}:
            raise ProtocolError("invalid_frame")
        self.kind = kind
        rate = (INPUT_RATE if kind == "input" else OUTPUT_RATE) * 2
        self.bytes = _Bucket(rate, rate * 0.08)
        self.frames = _Bucket(100, 10)

    def consume(self, frame: AudioFrame, *, now: float) -> None:
        _frame(frame)
        if frame.kind != self.kind:
            raise ProtocolError("invalid_frame")
        byte_tokens = self.bytes.available(len(frame.pcm), now)
        frame_tokens = self.frames.available(1, now)
        self.bytes.commit(byte_tokens, now)
        self.frames.commit(frame_tokens, now)


class Phase(StrEnum):
    NEW = "new"
    STARTING = "starting"
    ACTIVE = "active"
    CLOSING = "closing"
    CLOSED = "closed"


class ProtocolState:
    """Pure, bounded single-connection ordering checker, NOT durable authority.

    Pass a trusted generation allocated by W04, or the authenticated server's
    ready value on a client. Call accept_* only on this connection, in order.
    now is an injected monotonic clock. Expiry/revocation are enforced by W04/07.
    """

    def __init__(self, connection_generation: int):
        _u32(connection_generation, positive=True)
        self.connection_generation = connection_generation
        self.phase = Phase.NEW
        self.generation = 0
        self.output_open = False
        self.input_open = False
        self.muted = False
        self.input_sequence = self.output_sequence = self.event_sequence = 0
        self.elapsed_ms = 0
        self.transcripts: dict[str, tuple[str, int, int, bool]] = {}
        self.tools: dict[str, tuple[str, bool]] = {}
        self.input_budget, self.output_budget = AudioBudget("input"), AudioBudget("output")
        self.control_budget = _Bucket(20, 20)

    def _connection(self, generation: int) -> None:
        if generation != self.connection_generation:
            raise ProtocolError("stale_generation")

    def accept_client(self, event: ClientMessage, *, now: float) -> None:
        event = _validate(event.model_dump(mode="python", warnings=False), client=True)
        tokens = self.control_budget.available(1, now)
        if isinstance(event, Start):
            if self.phase != Phase.NEW:
                raise ProtocolError("invalid_state")
            self.phase = Phase.STARTING
        else:
            self._connection(event.connection_generation)
            if self.phase != Phase.ACTIVE:
                raise ProtocolError("invalid_state")
            if isinstance(event, Stop):
                self.phase, self.input_open, self.output_open = Phase.CLOSING, False, False
            elif isinstance(event, Mute):
                self.muted = event.muted
            elif isinstance(event, Input):
                desired = event.action == "begin"
                if self.input_open == desired:
                    raise ProtocolError("invalid_state")
                self.input_open = desired
        self.control_budget.commit(tokens, now)

    def accept_server(self, event: ServerMessage) -> None:
        event = _validate(event.model_dump(mode="python", warnings=False), client=False)
        self._connection(event.connection_generation)
        if event.event_sequence != self.event_sequence:
            raise ProtocolError("invalid_sequence")
        if event.elapsed_ms < self.elapsed_ms:
            raise ProtocolError("invalid_sequence")
        if self.phase in {Phase.NEW, Phase.CLOSED}:
            raise ProtocolError("invalid_state")
        if isinstance(event, Closed):
            self.phase, self.input_open, self.output_open = Phase.CLOSED, False, False
        elif isinstance(event, Error):
            if self.phase == Phase.STARTING and not event.fatal:
                raise ProtocolError("invalid_state")
            if event.fatal:
                self.phase, self.input_open, self.output_open = Phase.CLOSING, False, False
        elif isinstance(event, Ready):
            if self.phase != Phase.STARTING:
                raise ProtocolError("invalid_state")
            self.phase, self.input_open = Phase.ACTIVE, True
        elif self.phase != Phase.ACTIVE:
            raise ProtocolError("invalid_state")
        elif isinstance(event, Generation):
            if event.phase == "started":
                if self.output_open or event.generation != self.generation + 1:
                    raise ProtocolError("stale_generation")
                self.generation, self.output_open, self.output_sequence = event.generation, True, 0
            else:
                self._output(event.generation)
                self.output_open = False
        elif isinstance(event, Interrupted):
            self._output(event.generation)
            self.output_open = False
        elif isinstance(event, Transcript):
            self._transcript(event)
        elif isinstance(event, ToolStarted):
            if event.call_id in self.tools:
                raise ProtocolError("invalid_sequence")
            if len(self.tools) >= 64 or sum(not done for _, done in self.tools.values()) >= 4:
                raise ProtocolError("limit_exceeded")
            self.tools[event.call_id] = (event.tool, False)
        elif isinstance(event, ToolFinished):
            if self.tools.get(event.call_id) != (event.tool, False):
                raise ProtocolError("invalid_sequence")
            self.tools[event.call_id] = (event.tool, True)
        self.event_sequence += 1
        self.elapsed_ms = event.elapsed_ms

    def _output(self, generation: int) -> None:
        if generation != self.generation or not self.output_open:
            raise ProtocolError("stale_generation")

    def _transcript(self, event: Transcript) -> None:
        if event.speaker == "user":
            if event.generation != 0:
                raise ProtocolError("stale_generation")
        else:
            self._output(event.generation)
        prior = self.transcripts.get(event.transcript_id)
        if prior is None:
            if event.revision != 0:
                raise ProtocolError("invalid_sequence")
            if len(self.transcripts) >= 128:
                raise ProtocolError("limit_exceeded")
        elif prior[:2] != (event.speaker, event.generation) or prior[3] or event.revision != prior[2] + 1:
            raise ProtocolError("invalid_sequence")
        self.transcripts[event.transcript_id] = (event.speaker, event.generation, event.revision, event.final)

    def accept_audio(self, frame: AudioFrame, *, now: float) -> None:
        _frame(frame)
        self._connection(frame.connection_generation)
        if self.phase != Phase.ACTIVE:
            raise ProtocolError("invalid_state")
        if frame.kind == "input":
            if self.muted or not self.input_open:
                raise ProtocolError("invalid_state")
            if frame.sequence != self.input_sequence:
                raise ProtocolError("invalid_sequence")
            self.input_budget.consume(frame, now=now)
            self.input_sequence += 1
        else:
            self._output(frame.generation)
            if frame.sequence != self.output_sequence:
                raise ProtocolError("invalid_sequence")
            self.output_budget.consume(frame, now=now)
            self.output_sequence += 1
