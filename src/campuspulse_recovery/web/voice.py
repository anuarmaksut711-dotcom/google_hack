"""Native WSS in the existing backend. Explicit startup ports, no model fallback."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
import os
from typing import Callable, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..config import CLOUD_MARKERS
from ..observability.voice_events import VoiceEvent, FirestoreVoiceJournal, project
from ..security.context import AccessDenied
from ..storage.plans import _decode
from ..voice import protocol as p
from ..voice.coordinator import Limits, VoiceCoordinator
from ..voice.device_auth import DeviceAuth
from ..voice.ports import ProviderMode
from ..voice.sessions import FirestoreVoiceSessions, ResumeState, _Claim, _path
from .voice_pairing import PairingPageHeaders, create_pairing_router
from .auth import WebAuth

WS_PATH = "/api/voice/ws"


def create_voice_app(*, policy, documents, verifier, origin: str, environment: str,
                     clock=lambda: datetime.now(UTC), voice=None):
    """Supported single-service composition, preserving the base factory.

    Two WebAuth instances use identical durable dependencies. Each creates its
    own local SessionCredential; capabilities are never transferred between them.
    """
    from .app import create_app
    app = create_app(policy=policy, documents=documents, verifier=verifier,
        origin=origin, environment=environment, clock=clock)
    if voice is not None:
        auth = WebAuth(documents, policy, verifier=verifier, origin=origin,
            environment=environment, clock=clock)
        mount_voice(app, auth=auth, environment=environment, config=voice)
    return app


@dataclass(frozen=True)
class ManagedBindings:
    target: object


    verify_deployment: Callable
    issue_reference: Callable
    credentials_provider: Callable


@dataclass(frozen=True)
class VoiceConfig:
    enabled: bool = False
    mode: ProviderMode = ProviderMode.MANAGED_PRODUCTION
    managed: ManagedBindings | None = None
    local_fake_factory: Callable | None = None
    journal: Callable | None = None
    evidence_class: Literal["windows_local", "cloud_live"] = "windows_local"
    clock: object | None = None
    limits: Limits = Limits()


class EphemeralVoiceSessions(FirestoreVoiceSessions):
    """WSS never checkpoints conversation or provider handles. Keep retirement."""
    @staticmethod
    def ephemeral(resume):
        return ResumeState(seen_call_ids=resume.seen_call_ids)

    async def acquire(self, token):
        lease, resume = await super().acquire(token)
        return lease, self.ephemeral(resume)

    async def checkpoint(self, lease, resume):
        await super().checkpoint(lease, self.ephemeral(resume))


def _same_fence(a, b):
    return a.model_dump(exclude={"lease_until"}) == b.model_dump(exclude={"lease_until"})


async def authorize_current(sessions, initial):
    """Fresh durable authority, independent of W04's possibly held local lock.

    Only lease_until can advance. One same-fence renewal race retry shares a
    two-second budget. The serialized initial lease is never current authority.
    """
    async with asyncio.timeout(2):
        async def latest(tx):
            claim = _decode(await tx.get(_path(initial.device_id)), "voice_stream", _Claim)
            if not _same_fence(initial, claim.lease):
                raise AccessDenied("voice_stream_stale")
            return claim.lease
        captured = await sessions._run(latest)
        for attempt in range(2):
            try:
                access = await sessions.check(captured)
                if sessions.now() >= min(captured.lease_until, captured.deadline, access.expires_at):
                    raise AccessDenied("voice_stream_expired")
                return access
            except AccessDenied as error:
                if attempt or str(error) != "voice_stream_stale":
                    raise
                renewed = await sessions._run(latest)
                if renewed.lease_until <= captured.lease_until:
                    raise
                captured = renewed


class SocketTransport:
    def __init__(self, websocket, coordinator, config, now, journal):
        self.websocket, self.coordinator, self.config, self.now = websocket, coordinator, config, now
        self.journal = journal
        self.disconnected = self.closed = False

    @property
    def evidence_class(self):
        return self.config.evidence_class

    async def finish(self, reason):
        if self.coordinator.lease is not None:
            event = VoiceEvent(session_id=self.coordinator.lease.session_id,
                run_id=self.coordinator.lease.run_id, event_id="evt_terminal", occurred_at=self.now(),
                kind="closed", evidence_class=self.evidence_class, status=reason,
                duration_ms=self.coordinator._elapsed(), cleanup_failed=self.coordinator.cleanup_failed)
            async with asyncio.timeout(.25):
                await self.journal(event)

    async def receive(self):
        try:
            message = await self.websocket.receive()
        except WebSocketDisconnect:
            self.disconnected = True
            return None
        if message["type"] == "websocket.disconnect":
            self.disconnected = True
            return None
        if message["type"] != "websocket.receive":
            raise p.ProtocolError("invalid_message")
        binary, text = message.get("bytes"), message.get("text")
        if (binary is None) == (text is None):
            raise p.ProtocolError("invalid_message")
        if binary is not None:
            if not isinstance(binary, bytes) or len(binary) > p.HEADER_SIZE + p.MAX_INPUT_PCM:
                raise p.ProtocolError("message_too_large")
            return binary
        if not isinstance(text, str) or len(text) > p.MAX_CONTROL_BYTES:
            raise p.ProtocolError("message_too_large")
        try:
            if len(text.encode("utf-8")) > p.MAX_CONTROL_BYTES:
                raise p.ProtocolError("message_too_large")
        except UnicodeError:
            raise p.ProtocolError("invalid_message") from None
        return text

    async def send(self, packet):

        if self.disconnected or self.closed:



            if isinstance(packet, p.Closed):
                return
            if isinstance(packet, p.ToolFinished) and packet.status == "cancelled":
                await self.record(packet)
                return
            raise WebSocketDisconnect()
        if isinstance(packet, p.AudioFrame):
            await self.websocket.send_bytes(p.encode_audio(packet))
        else:
            await self.websocket.send_text(p.encode_json(packet, client=False))
            await self.record(packet)

    async def record(self, packet):
        if packet.type != "closed":
            event = project(packet, lease=self.coordinator.lease, now=self.now(),
                evidence_class=self.evidence_class)
            if event is not None:
                async with asyncio.timeout(.25):
                    await self.journal(event)

    async def close(self):
        if not self.closed:
            self.closed = True
            if not self.disconnected:
                await self.websocket.close(code=1000)


def native_token(websocket, auth, *, local_fake):
    """WSS/TLS scope and exact Host; forwarding headers grant no authority."""
    headers = websocket.headers
    if (websocket.scope.get("scheme") != "wss" or headers.getlist("host") != [auth.host]
            or websocket.scope.get("query_string") or websocket.scope.get("subprotocols")
            or "cookie" in headers or "origin" in headers
            or headers.get("sec-fetch-site") not in (None, "none")
            or len(headers.getlist("authorization")) != 1):
        raise AccessDenied("voice_access_denied")
    if local_fake and (not websocket.client or websocket.client.host not in ("127.0.0.1", "::1")):
        raise AccessDenied("voice_access_denied")
    value = headers["authorization"]
    if not value.startswith("Bearer ") or len(value) > 263:
        raise AccessDenied("voice_access_denied")
    token = value[7:]
    if not token or not token.isascii() or any(c.isspace() for c in token):
        raise AccessDenied("voice_access_denied")
    return token


def mount_voice(app, *, auth, environment, config: VoiceConfig):
    if type(config) is not VoiceConfig:
        raise ValueError("explicit_voice_config_required")
    if (type(config.enabled) is not bool or not isinstance(config.mode, ProviderMode)
            or config.evidence_class not in ("windows_local", "cloud_live")
            or (config.mode == ProviderMode.LOCAL_FAKE and config.evidence_class != "windows_local")):
        raise ValueError("explicit_voice_config_required")
    if not config.enabled:
        return
    production = environment == "cloud" or bool(CLOUD_MARKERS.intersection(os.environ))
    fake = config.mode == ProviderMode.LOCAL_FAKE
    if fake:
        if (production or auth.policy.config.mode != "local_development" or urlsplit(auth.origin).hostname not in
                ("localhost", "127.0.0.1", "::1") or not callable(config.local_fake_factory)
                or config.managed is not None):
            raise ValueError("voice_local_fake_requires_explicit_loopback")
    elif config.mode == ProviderMode.MANAGED_PRODUCTION:
        from .agent_client import AgentEngineTarget
        binding = config.managed
        if (binding is None or config.local_fake_factory is not None or config.clock is not None
                or not all(callable(x) for x in (binding.verify_deployment,
                    binding.issue_reference, binding.credentials_provider))):
            raise ValueError("verified_managed_voice_binding_required")
        target = AgentEngineTarget.model_validate(binding.target)
        if target.project_id != auth.policy.config.project_id or binding.verify_deployment(auth.policy, target) is not True:
            raise ValueError("verified_managed_voice_binding_required")
    else:
        raise ValueError("voice_provider_mode_rejected")
    devices = DeviceAuth(auth)
    sessions = EphemeralVoiceSessions(devices)
    journal = config.journal or FirestoreVoiceJournal(auth.documents).append
    router = APIRouter()

    @router.websocket(WS_PATH)
    async def voice(websocket: WebSocket):
        transport = None
        outcome = "upstream_error"
        try:
            token = native_token(websocket, auth, local_fake=fake)
            async with asyncio.timeout(2):
                await devices.authenticate(token)
            if fake:
                provider = config.local_fake_factory()
            else:
                from ..cloud.voice_client import ManagedVoiceProvider
                async def issue(lease):
                    return await binding.issue_reference(sessions, lease)
                async def authorize(initial):
                    return await authorize_current(sessions, initial)
                provider = ManagedVoiceProvider(target, issue_reference=issue,
                    authorize=authorize, credentials_provider=binding.credentials_provider)
            coordinator = VoiceCoordinator(sessions, provider, selected_mode=config.mode,
                production=production, tools=provider if hasattr(provider, "execute") else None,
                clock=config.clock, limits=config.limits)
            transport = SocketTransport(websocket, coordinator, config, auth.now, journal)
            async with asyncio.timeout(2):
                await websocket.accept()
            result = await coordinator.run(token, transport)
            outcome = result.reason
        except asyncio.CancelledError:
            outcome = "disconnected"
            raise
        except Exception:

            pass
        finally:
            try:
                async with asyncio.timeout(config.limits.close_seconds):
                    if transport is not None:
                        await transport.close()
                        await transport.finish(outcome)
                    else:
                        await websocket.close(code=1008)
            except Exception:
                pass

    app.include_router(create_pairing_router(device_auth=devices))
    app.include_router(router)

    app.add_middleware(PairingPageHeaders)


def voice_server_config(app, *, host="127.0.0.1", port=8000, ssl_keyfile=None, ssl_certfile=None):
    """Required bounded Uvicorn server profile. No arbitrary proxy/header trust.

    Direct TLS or an independently verified proxy scope adapter is required.
    Access/server protocol logs are disabled: even rejected URLs may be secrets.
    """
    import uvicorn
    return uvicorn.Config(app, host=host, port=port, ws="websockets",
        ws_max_size=p.MAX_CONTROL_BYTES, ws_max_queue=4, ws_per_message_deflate=False,
        ws_ping_interval=10, ws_ping_timeout=10, proxy_headers=False,
        access_log=False, log_config=None, log_level="critical", limit_concurrency=16,
        timeout_graceful_shutdown=5, ssl_keyfile=ssl_keyfile, ssl_certfile=ssl_certfile)
