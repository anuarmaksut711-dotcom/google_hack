"""Network-denied repository tests, no SDK/HTTP clients or live authority."""

import asyncio
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import socket

import pytest
from pydantic import ValidationError

from campuspulse_recovery.domain.ports import BindingConflict, PortError
from campuspulse_recovery.security.context import AccessDenied, ServerRunBinding
from campuspulse_recovery.storage.plans import _decode, _encode
from campuspulse_recovery.voice import sessions as module
from campuspulse_recovery.voice.device_auth import DeviceAuth, VoiceCredential
from campuspulse_recovery.voice.sessions import (
    FirestoreVoiceSessions, HistoryNote, ResumeState, _Claim, _path,
)
from campuspulse_recovery.web.auth import WebAuth, WebSession, digest
from tests.storage.test_plans import FakeDocuments, Harness, NOW, run

TOKEN = "s" * 43


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("network denied in W04")
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)


class ManualClock:
    def __init__(self):
        self.value, self.mono, self.sleepers = NOW, 0., []

    def __call__(self):
        return self.value

    def monotonic(self):
        return self.mono

    async def sleep(self, seconds):
        future = asyncio.get_running_loop().create_future()
        record = (self.mono + seconds, future)
        self.sleepers.append(record)
        try:
            await future
        finally:
            self.sleepers.remove(record)

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)
        self.mono += seconds
        for deadline, future in self.sleepers:
            if deadline <= self.mono and not future.done():
                future.set_result(None)


class SessionSetup(Harness):
    def __init__(self, documents=None, *, credential_seconds=1200, parent_seconds=3600):
        super().__init__(documents)
        self.clock = ManualClock()
        self.parent_path = "cp_web_sessions/" + digest("synthetic-parent")
        self.credential_path = "cp_voice_credentials/" + digest(TOKEN)
        binding = ServerRunBinding(owner_id=self.config.student.trusted_user_id,
            signin_principal_id=self.config.student.allowed_signin_principal,
            session_id="parent_session", run_id="parent_run", config_revision=self.config.config_revision,
            expires_at=NOW + timedelta(seconds=parent_seconds))
        self.parent = WebSession(binding=binding, principal_id=binding.signin_principal_id,
            authenticated_at=NOW, expires_at=binding.expires_at, csrf_digest=digest("synthetic-csrf"))
        self.credential = VoiceCredential(parent_hash=digest("synthetic-parent"), binding=binding,
            device_id="device_synthetic", client_kind="windows", expires_at=NOW + timedelta(seconds=credential_seconds))
        self.documents.data[self.parent_path] = _encode("web_session", self.parent)
        self.documents.data[self.credential_path] = _encode("voice_credential", self.credential)
        self.sessions = self.repository()

    def repository(self, documents=None):
        auth = WebAuth(documents or self.documents, self.policy, verifier=object(),
                       origin="https://campus.test", environment="local", clock=self.clock)
        return FirestoreVoiceSessions(DeviceAuth(auth))

    def replace_credential(self, **changes):
        self.credential = VoiceCredential.model_validate(self.credential.model_dump() | changes)
        self.documents.data[self.credential_path] = _encode("voice_credential", self.credential)

    def replace_parent(self, **changes):
        self.parent = WebSession.model_validate(self.parent.model_dump() | changes)
        self.documents.data[self.parent_path] = _encode("web_session", self.parent)


def test_current_source_paths_and_no_sdk_clients():
    root = Path(__file__).resolve().parents[2]
    assert Path(module.__file__).resolve() == root / "src/campuspulse_recovery/voice/sessions.py"
    print("CURRENT_SOURCE_MODULE =", Path(module.__file__).resolve())


def test_two_independent_repositories_race_and_stale_owner_is_fenced():
    async def scenario():
        h = SessionSetup()
        second = h.repository()
        results = await asyncio.gather(h.sessions.acquire(TOKEN), second.acquire(TOKEN), return_exceptions=True)
        assert sum(isinstance(x, BindingConflict) for x in results) == 1
        lease, _ = next(x for x in results if not isinstance(x, Exception))
        assert lease.connection_generation == 1
        await second.check(lease, tool="read_calendar")
        await h.sessions.release(lease)
        successor, _ = await second.acquire(TOKEN)
        assert successor.connection_generation == 2 and successor.holder_id != lease.holder_id
        for operation in (h.sessions.check(lease), h.sessions.renew(lease), h.sessions.release(lease)):
            with pytest.raises(AccessDenied):
                await operation
        await second.check(successor)
        with pytest.raises(AccessDenied):
            await second.check(successor, tool="apply_plan")
        with pytest.raises(AccessDenied):
            await second.check(successor, tool="verify_plan")
    run(scenario())


def test_crash_lease_expiry_new_generation_and_bounded_context_without_audio():
    async def scenario():
        h = SessionSetup()
        old, _ = await h.sessions.acquire(TOKEN)
        resume = ResumeState(history=(HistoryNote(speaker="user", text="Есть свободный час"),),
            transport_token="synthetic-resumption", transport_expires_at=NOW + timedelta(seconds=100))
        await h.sessions.checkpoint(old, resume)

        restarted = FakeDocuments()
        restarted.data = deepcopy(h.documents.data)
        fresh = h.repository(restarted)
        with pytest.raises(BindingConflict):
            await fresh.acquire(TOKEN)
        h.clock.advance(16)
        new, restored = await fresh.acquire(TOKEN)
        assert new.connection_generation == 2 and new.run_id != old.run_id
        assert new.session_id == old.session_id and restored == resume
        with pytest.raises(AccessDenied): await fresh.check(old)
        assert TOKEN not in repr(restarted.data)
        assert "synthetic-resumption" not in repr(restored)
        assert "Есть свободный час" not in repr(restored)
        assert b"pcm" not in restarted.data[_path(old.device_id)]["body"]
    run(scenario())


@pytest.mark.parametrize("boundary", ["credential_expiry", "parent_expiry", "revoke", "signout", "config", "resource"])
def test_authority_is_reloaded_at_every_boundary(boundary):
    async def scenario():
        h = SessionSetup(credential_seconds=10 if boundary == "credential_expiry" else 1200,
                         parent_seconds=10 if boundary == "parent_expiry" else 3600)
        lease, _ = await h.sessions.acquire(TOKEN)
        if boundary.endswith("expiry"):
            h.clock.advance(10)
        elif boundary == "revoke": h.replace_credential(revoked=True)
        elif boundary == "signout": h.replace_parent(revoked=True)
        else:
            cfg = h.config.model_dump()
            if boundary == "config": cfg["student"]["spreadsheet_id"] = "sheet-new"
            else: cfg["student"]["calendar_id"] = "calendar-new"
            from campuspulse_recovery.config import AppConfig
            from campuspulse_recovery.security.resources import ResourcePolicy
            h.sessions.policy = h.sessions.devices.policy = ResourcePolicy(AppConfig.model_validate(cfg), environment="local")
        for operation in (h.sessions.check(lease), h.sessions.renew(lease), h.sessions.checkpoint(lease, ResumeState())):
            with pytest.raises(AccessDenied): await operation
        with pytest.raises(AccessDenied): await h.sessions.acquire(TOKEN)

        await h.sessions.release(lease)
    run(scenario())


def test_sessions_exceed_45_seconds_without_extending_credential_or_parent():
    async def scenario():
        h = SessionSetup(credential_seconds=75)
        lease, _ = await h.sessions.acquire(TOKEN)
        assert lease.deadline == NOW + timedelta(seconds=75)
        for _ in range(7):
            h.clock.advance(10)
            lease = await h.sessions.renew(lease)
            assert lease.lease_until <= lease.deadline
        await h.sessions.check(lease)
        h.clock.advance(5)
        with pytest.raises(AccessDenied): await h.sessions.renew(lease)
    run(scenario())


def test_renewal_fence_and_expired_transport_and_no_counter_wrap():
    async def scenario():
        h = SessionSetup()
        old, _ = await h.sessions.acquire(TOKEN)
        h.clock.advance(5)
        lease = await h.sessions.renew(old)
        with pytest.raises(AccessDenied): await h.sessions.release(old)
        await h.sessions.checkpoint(lease, ResumeState(transport_token="synthetic", transport_expires_at=NOW + timedelta(seconds=6)))
        await h.sessions.release(lease)
        h.clock.advance(2)
        lease, resume = await h.sessions.acquire(TOKEN)
        assert resume.transport_token is None
        raw = _decode(h.documents.data[_path(lease.device_id)], "voice_stream", _Claim)
        last = type(lease).model_validate(lease.model_dump() | {"connection_generation": 0xFFFFFFFF})
        h.documents.data[_path(lease.device_id)] = _encode("voice_stream", _Claim(lease=last, released=True))
        with pytest.raises(BindingConflict, match="voice_generation_exhausted"):
            await h.sessions.acquire(TOKEN)
        assert raw.lease.connection_generation == 2
    run(scenario())


@pytest.mark.parametrize("committed", [True, False])
def test_ambiguous_commit_reconciles_same_candidate_only(committed):
    class LostAck(FakeDocuments):
        armed = False
        async def transact(self, callback):
            if not self.armed:
                return await super().transact(callback)
            self.armed = False
            if committed:
                await super().transact(callback)
            raise PortError("synthetic_lost_ack")
    async def scenario():
        documents = LostAck()
        h = SessionSetup(documents)
        documents.armed = True
        if committed:
            lease, _ = await h.sessions.acquire(TOKEN)
            assert lease.connection_generation == 1
            await h.sessions.check(lease)
            assert _decode(documents.data[_path(lease.device_id)], "voice_stream", _Claim).lease == lease
        else:
            with pytest.raises(PortError): await h.sessions.acquire(TOKEN)
            assert not any(x.startswith("cp_voice_streams/") for x in documents.data)
    run(scenario())


def test_expiry_after_final_document_read_and_transaction_retry():
    class Retrying(FakeDocuments):
        armed = False
        async def transact(self, callback):
            if self.armed:
                self.armed = False
                before = deepcopy(self.data)
                await super().transact(callback)
                self.data = before
                h.clock.advance(10)
            return await super().transact(callback)
    async def scenario():
        nonlocal h
        h = SessionSetup(Retrying(), credential_seconds=10)
        h.documents.armed = True
        with pytest.raises(AccessDenied): await h.sessions.acquire(TOKEN)
        assert not any(x.startswith("cp_voice_streams/") for x in h.documents.data)
    h = None
    run(scenario())


def test_resume_schema_rejects_unbounded_or_raw_audio_state():
    for fields in ({"history": [dict(speaker="user", text="a")]*17},
                   {"transport_token": "a"*4097}, {"raw_audio": b"synthetic"},
                   {"history": [dict(speaker="user", text="я"*512)]*16},
                   {"transport_token": "synthetic-no-expiry"}):
        with pytest.raises(ValidationError): ResumeState.model_validate(fields)


def test_expiry_after_last_read_cannot_acquire_and_stalled_store_is_bounded():
    class DelayedRead(FakeDocuments):
        armed = False
        async def transact(self, callback):
            async def wrap(tx):
                original = tx.get
                async def get(path):
                    raw = await original(path)
                    if self.armed and path.startswith("cp_voice_streams/"):
                        self.armed = False
                        h.clock.advance(10)
                    return raw
                tx.get = get
                return await callback(tx)
            return await super().transact(wrap)
    async def scenario():
        nonlocal h
        h = SessionSetup(DelayedRead(), credential_seconds=10)
        h.documents.armed = True
        with pytest.raises(AccessDenied): await h.sessions.acquire(TOKEN)
        assert not any(x.startswith("cp_voice_streams/") for x in h.documents.data)
        h = SessionSetup()
        lease, _ = await h.sessions.acquire(TOKEN)
        h.sessions.transaction_seconds = .01
        cancelled = asyncio.Event()
        async def stall(callback):
            try: await asyncio.Event().wait()
            finally: cancelled.set()
        h.documents.transact = stall
        with pytest.raises(PortError): await h.sessions.check(lease)
        assert cancelled.is_set()
    h = None
    run(scenario())
