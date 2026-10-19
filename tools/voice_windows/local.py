"""Explicit loopback-only real TLS/HTTP/WSS harness; synthetic identity/upstream.

No production auth helper, cloud fallback, actual Google credentials or devices.
Imports backend/crypto dependencies only when this test mode is selected.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import ipaddress
from pathlib import Path
import socket
import ssl
import tempfile
import time
from types import SimpleNamespace

from .client import NativeHTTP, PairingClient, VoiceClient, wss_connection


class MemoryDocuments:
    project_id, database_id = "campus-test-42", "(default)"
    def __init__(self):
        self.data, self.lock = {}, asyncio.Lock()

    async def get(self, path):
        return deepcopy(self.data.get(path))

    async def scan(self, collection):
        return tuple(deepcopy(v) for k, v in self.data.items() if k.rsplit("/", 1)[0] == collection)

    async def transact(self, callback):
        from campuspulse_recovery.domain.ports import BindingConflict
        async with self.lock:
            pending, written = deepcopy(self.data), False
            async def get(path):
                assert not written, "reads_before_writes"
                return deepcopy(pending.get(path))
            def create(path, value):
                nonlocal written
                written = True
                if path in pending:
                    raise BindingConflict("synthetic_duplicate")
                pending[path] = deepcopy(value)
            def set_value(path, value):
                nonlocal written
                written = True
                pending[path] = deepcopy(value)
            result = await callback(SimpleNamespace(get=get, create=create, set=set_value))
            self.data = pending
            return result


def local_config():
    from campuspulse_recovery.config import AppConfig
    return AppConfig.model_validate(dict(mode="local_development", project_id="campus-test-42",
        agent_region="europe-west4", model_location="global", web_cloud_run_region="europe-west2",
        mcp_cloud_run_region="europe-west1", firestore_location="eur3", firestore_database="(default)",
        workspace_access_mode="explicit_resource_sharing", student=dict(trusted_user_id="student-synthetic",
        allowed_signin_principal="https://accounts.google.com#synthetic-voice-only",
        spreadsheet_id="synthetic-sheet", assignments_a1_range="'Assignments'!A2:F100",
        calendar_id="synthetic-calendar@group.calendar.google.com", timezone="Asia/Qyzylorda"),
        identities=dict(backend_service_account="backend@campus-test-42.iam.gserviceaccount.com",
        agent_service_account="agent-test@campus-test-42.iam.gserviceaccount.com",
        mcp_service_account="mcp-test@campus-test-42.iam.gserviceaccount.com")))


def certificates(directory):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
    now = datetime.now(UTC)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Ephemeral CampusPulse loopback test CA")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
          .not_valid_after(now + timedelta(hours=1)).add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
          .sign(key, hashes.SHA256()))
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")]))
        .issuer_name(ca_name).public_key(leaf_key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False).sign(key, hashes.SHA256()))
    directory = Path(directory)
    ca_file, cert_file, key_file = directory / "ca.pem", directory / "server.pem", directory / "key.pem"
    ca_file.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    cert_file.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(leaf_key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    context = ssl.create_default_context(cafile=str(ca_file))
    return context, str(cert_file), str(key_file)


class FakeStream:
    def __init__(self):
        self.events = asyncio.Queue(maxsize=32)
        self.closed = self.cancelled = False
        self.input_times = []
        self.input_bytes = 0
        self.first_output_time = None
        self.turn = 0
        self.first = True

    async def send(self, packet):
        from campuspulse_recovery.voice import protocol as p
        from campuspulse_recovery.voice.ports import ResponseBoundary, OutputAudio, ToolRequest
        if isinstance(packet, p.AudioFrame):
            self.input_times.append(time.monotonic())
            self.input_bytes += len(packet.pcm)
            if self.first:
                self.first = False
                await self.events.put(ResponseBoundary(1, "started"))
                await self.events.put(OutputAudio(1, b"\x11\x00" * 480))
        elif isinstance(packet, p.Input) and packet.action == "end":
            self.turn += 1
            if self.turn == 1:
                await self.events.put(ResponseBoundary(1, "interrupted"))
                await self.events.put(ToolRequest("synthetic_read", "read_assignments", {}))

    async def receive(self):
        from campuspulse_recovery.voice.ports import OutputAudio
        event = await self.events.get()
        if isinstance(event, OutputAudio):
            await asyncio.sleep(.02)
            if self.first_output_time is None:
                self.first_output_time = time.monotonic()
        return event

    async def tool_result(self, call_id, result):
        from campuspulse_recovery.voice.ports import ResponseBoundary, OutputAudio
        assert call_id == "synthetic_read" and result.status == "success"
        await self.events.put(ResponseBoundary(2, "started"))
        for _ in range(4):
            await self.events.put(OutputAudio(2, b"\x22\x00" * 480))
        await self.events.put(ResponseBoundary(2, "completed"))

    async def cancel(self):
        self.cancelled = True

    async def close(self):
        self.closed = True


class FakeProvider:
    def __init__(self):
        from campuspulse_recovery.voice.ports import ProviderMode
        self.mode = ProviderMode.LOCAL_FAKE
        self.stream = FakeStream()
        self.tools = []

    async def open(self, lease, resume):
        return self.stream

    async def execute(self, access, request):
        from campuspulse_recovery.voice.ports import ToolResult
        access.require_tool(request.tool)
        self.tools.append(request.tool)
        return ToolResult("success", {"synthetic": True})


@asynccontextmanager
async def local_server():
    """Real listening loopback TLS socket, actual factory/route/coordinator."""
    import uvicorn
    from campuspulse_recovery.web.voice import create_voice_app, VoiceConfig, voice_server_config
    from campuspulse_recovery.web.auth import VerifiedSignIn
    from campuspulse_recovery.security.resources import ResourcePolicy
    from campuspulse_recovery.security.context import AccessDenied
    from campuspulse_recovery.voice.coordinator import Limits
    config, documents, provider, events = local_config(), MemoryDocuments(), FakeProvider(), []
    class SyntheticVerifier:
        async def verify(self, token, *, nonce, now):
            if token != "synthetic-local-signin" or not nonce:
                raise AccessDenied("synthetic_identity_denied")
            return VerifiedSignIn(principal_id=config.student.allowed_signin_principal,
                authenticated_at=now, expires_at=now + timedelta(minutes=30))
    async def journal(event):
        events.append(event)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    port = sock.getsockname()[1]
    origin = f"https://127.0.0.1:{port}"
    with tempfile.TemporaryDirectory(prefix="campuspulse-voice-tls-") as directory:
        context, cert, key = certificates(directory)
        app = create_voice_app(policy=ResourcePolicy(config, environment="local"), documents=documents,
            verifier=SyntheticVerifier(), origin=origin, environment="local",
            voice=VoiceConfig(enabled=True, mode=provider.mode, local_fake_factory=lambda: provider,
                journal=journal, limits=Limits(check_seconds=.05)))
        server = uvicorn.Server(voice_server_config(app, host="127.0.0.1", port=port,
                                                  ssl_keyfile=key, ssl_certfile=cert))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    if task.done():
                        task.result()
                    await asyncio.sleep(.01)
            yield SimpleNamespace(origin=origin, context=context, provider=provider, events=events,
                                  documents=documents, app=app)
        finally:
            server.should_exit = True
            try:
                async with asyncio.timeout(6):
                    await task
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                sock.close()


async def synthetic_pair(server):
    """A synthetic parent browser action only, through actual TLS HTTP routes."""
    import httpx
    pair = PairingClient(NativeHTTP(server.origin, server.context, local=True))
    attempt = await pair.start("Synthetic Windows test")
    async with httpx.AsyncClient(base_url=server.origin, verify=server.context, trust_env=False, timeout=5) as browser:
        challenge = await browser.get("/auth/challenge")
        signin = await browser.post("/auth/signin", headers={"Origin": server.origin,
            "X-CSRF-Token": challenge.json()["csrf_token"]}, json={"credential": "synthetic-local-signin"})
        assert signin.status_code == 200
        headers = {"Origin": server.origin, "X-CSRF-Token": signin.json()["csrf_token"]}
        page = await browser.get("/voice/pair")
        assert page.status_code == 200 and "script-src 'sha256-" in page.headers["content-security-policy"]
        preview = await browser.post("/api/voice/pairing/preview", headers=headers, json={"code": attempt.code})
        assert preview.status_code == 200
        decision = await browser.post("/api/voice/pairing/decision", headers=headers,
                                      json={"code": attempt.code, "decision": "approve"})
        assert decision.status_code == 200
    return await pair.exchange(attempt)


async def local_e2e(pcm, sink=None, trace=lambda value: None):
    async with local_server() as server:
        token = await synthetic_pair(server)
        async with wss_connection(server.origin, token, server.context) as socket:
            client = VoiceClient(socket, sink=sink, trace=trace, heartbeat=.1)
            result = await client.run(lambda c: c.finish_wav(pcm))
        stream = server.provider.stream
        assert stream.closed and stream.cancelled
        assert stream.input_bytes == len(pcm)
        assert server.provider.tools == ["read_assignments"]
        result.update(mode="local-e2e", evidence_class="windows_local", input_mode="wav_simulation",
            tls="ephemeral_ca_verified", socket_path="actual_asgi_coordinator",
            duplex=stream.first_output_time is not None and stream.first_output_time < stream.input_times[-1],
            upstream_closed=stream.closed, upstream_cancelled=stream.cancelled,
            simulated_input_ms=round(len(pcm) / 32, 2),
            observed_input_span_ms=round((stream.input_times[-1] - stream.input_times[0]) * 1000, 2))
        return result
