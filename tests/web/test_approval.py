"""Synthetic HTTP actions and transactional doubles, never human consent.

Actual T04/T05 repositories are exercised. FakeDocuments rejects reads after
writes and commits atomically; it is not evidence of deployed Firestore/IAM.
"""

import asyncio
from copy import copy, deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace

import httpx
import pytest

from campuspulse_recovery.domain.models import Approval, PlanReference
from campuspulse_recovery.domain.ports import BindingConflict, PortError
from campuspulse_recovery.security.context import AccessDenied
from campuspulse_recovery.storage.plans import _decode, _encode, _key, _plan_path, plan_reference
from campuspulse_recovery.web.app import create_app
from campuspulse_recovery.web.approval import FirestoreApprovalStore
from campuspulse_recovery.web.auth import (
    CHALLENGE_COOKIE, CHALLENGE_CSRF_COOKIE, CSRF_COOKIE, SESSION_COOKIE, GoogleSignInVerifier,
    VerifiedSignIn, WebAuth, digest,
)
from tests.storage.test_plans import FakeDocuments, Harness, NOW, config, make_plan, run, sources

ORIGIN = "https://campus.test"


class Clock:
    def __init__(self):
        self.value = NOW

    def __call__(self):
        return self.value


class SyntheticVerifier:
    async def verify(self, token, *, nonce, now):
        if token != "synthetic-signed-token" or not nonce:
            raise AccessDenied("test_identity_rejected")
        return VerifiedSignIn(principal_id=config().student.allowed_signin_principal,
            authenticated_at=NOW, expires_at=NOW + timedelta(hours=1))


class Setup(Harness):
    def __init__(self, documents=None):
        super().__init__(documents=documents)
        self.clock = Clock()
        self.verifier = SyntheticVerifier()
        self.approvals = FirestoreApprovalStore(self.documents, self.policy, clock=self.clock)

    def app(self, **changes):
        return create_app(**{**dict(policy=self.policy, documents=self.documents, verifier=self.verifier,
            origin=ORIGIN, environment="local", clock=self.clock), **changes})

    def client(self, app=None):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app or self.app()), base_url=ORIGIN)

    async def seed_current(self, version=1, expected=None):
        plan = await self.seed(version=version)
        with self.request() as ctx:
            await self.plans.set_current(ctx, plan_reference(plan), expected=expected)
        return plan


async def login(client):
    challenge = await client.get("/auth/challenge")
    assert challenge.status_code == 200
    result = await client.post("/auth/signin", headers={"Origin": ORIGIN,
        "X-CSRF-Token": challenge.json()["csrf_token"]}, json={"credential": "synthetic-signed-token"})
    assert result.status_code == 200, result.text
    return {"Origin": ORIGIN, "X-CSRF-Token": result.json()["csrf_token"]}


def action(plan, key="click-1", **extra):
    return {**plan_reference(plan).model_dump(mode="json"), "request_key": key, **extra}


async def read(client, plan):
    return await client.get("/api/plan", params={"plan_id": plan.payload.plan_id, "version": plan.payload.version})


def test_authenticated_exact_approval_duplicate_reload_and_historical_lineage():
    async def scenario():
        h = Setup()
        plan = await h.seed_current()
        async with h.client() as client:
            headers = await login(client)
            session = (await client.get("/auth/session")).json()
            assert session["run_id"] != plan.payload.source_references[0].run_id
            before = await read(client, plan)
            assert before.status_code == 200 and before.json()["approval"] is None
            response = await client.post("/api/approval", headers=headers, json=action(plan))
            assert response.status_code == 200, response.text
            approved = response.json()["approval"]
            assert approved["approved_by"] == h.config.student.allowed_signin_principal
            assert approved["status"] == "active"
            h.clock.value += timedelta(seconds=1)
            duplicate = await client.post("/api/approval", headers=headers, json=action(plan))
            assert duplicate.json() == response.json()

            async with h.client(h.app()) as reload:
                reload.cookies.update(client.cookies)
                after = await read(reload, plan)
                assert after.status_code == 200
                assert after.json()["approval"] == approved
                assert after.json()["plan"] == plan.model_dump(mode="json")
            assert response.headers["cache-control"] == "no-store"
            assert "default-src 'none'" in response.headers["content-security-policy"]
    run(scenario())


@pytest.mark.parametrize("extra", [{"approved": True}, {"owner_id": "student-1"}, {"principal_id": "fake"},
    {"operations": []}, {"session_id": "session-1"}, {"run_id": "run-1"}, {"calendar_id": "primary"}])
def test_model_approval_or_identity_fields_are_rejected(extra):
    async def scenario():
        h = Setup()
        plan = await h.seed_current()
        async with h.client() as client:
            headers = await login(client)
            result = await client.post("/api/approval", headers=headers, json=action(plan, **extra))
            assert result.status_code == 400
            assert (await read(client, plan)).json()["approval"] is None
            assert "synthetic-signed-token" not in result.text
    run(scenario())


@pytest.mark.parametrize("case", ["anonymous", "header_identity", "csrf_missing", "csrf_wrong", "origin_missing",
    "origin_foreign", "fetch_cross_site", "host_foreign", "session_cookie_forged", "csrf_cookie_foreign"])
def test_unauthorized_session_csrf_origin_and_header_spoof(case):
    async def scenario():
        h = Setup()
        plan = await h.seed_current()
        async with h.client() as client:
            headers = await login(client)
            if case in ("anonymous", "header_identity"):
                client.cookies.clear()
            if case == "header_identity":
                headers.update({"X-User": h.config.student.allowed_signin_principal, "X-Run": "run-1", "X-Session": "session-1"})
            if case == "csrf_missing": headers.pop("X-CSRF-Token")
            if case == "csrf_wrong": headers["X-CSRF-Token"] = "foreign"
            if case == "origin_missing": headers.pop("Origin")
            if case == "origin_foreign": headers["Origin"] = "https://evil.test"
            if case == "fetch_cross_site": headers["Sec-Fetch-Site"] = "cross-site"
            if case == "host_foreign": headers["Host"] = "evil.test"
            if case == "session_cookie_forged":
                client.cookies.clear()
                client.cookies.set(SESSION_COOKIE, "made-up")
            if case == "csrf_cookie_foreign":
                headers["Cookie"] = f"{SESSION_COOKIE}={client.cookies.get(SESSION_COOKIE)}; {CSRF_COOKIE}=foreign"
            response = await client.post("/api/approval", headers=headers, json=action(plan))
            assert response.status_code == 403, response.text
            assert not any(p.startswith("cp_approvals/") for p in h.documents.data)
    run(scenario())


def test_hash_mismatch_old_version_and_key_collision():
    async def scenario():
        h = Setup()
        first = await h.seed_current()
        second = await h.seed_current(2, plan_reference(first))
        async with h.client() as client:
            headers = await login(client)
            assert (await client.post("/api/approval", headers=headers, json=action(first))).status_code == 409
            wrong = action(second, payload_hash="0" * 64)
            assert (await client.post("/api/approval", headers=headers, json=wrong)).status_code == 409
            assert (await client.post("/api/approval", headers=headers, json=action(second))).status_code == 200
            third = await h.seed_current(3, plan_reference(second))
            assert (await client.post("/api/approval", headers=headers, json=action(third))).status_code == 409
    run(scenario())


def test_expiry_equality_retry_new_key_cannot_renew_or_reactivate():
    async def scenario():
        h = Setup()
        plan = await h.seed_current()
        async with h.client() as client:
            headers = await login(client)
            approved = (await client.post("/api/approval", headers=headers, json=action(plan))).json()["approval"]
            h.clock.value += timedelta(minutes=10)
            result = await client.post("/api/approval", headers=headers, json=action(plan))
            assert result.json()["approval"]["status"] == "expired"
            for key in ("click-1", "new-click"):
                replay = (await client.post("/api/approval", headers=headers, json=action(plan, key))).json()["approval"]
                assert replay["status"] == "expired"
                assert replay["approved_at"] == approved["approved_at"]
                assert replay["expires_at"] == approved["expires_at"]
            with h.request() as ctx:
                with pytest.raises(BindingConflict):
                    await h.approvals.require_active(ctx, plan_reference(plan), h.clock())
    run(scenario())


def test_revoke_is_durable_and_duplicate_preserves_timestamp():
    async def scenario():
        h = Setup()
        plan = await h.seed_current()
        async with h.client() as client:
            headers = await login(client)
            await client.post("/api/approval", headers=headers, json=action(plan))
            revoked = await client.post("/api/approval/revoke", headers=headers, json=action(plan, "revoke-1"))
            assert revoked.status_code == 200, revoked.text
            assert revoked.json()["approval"]["status"] == "revoked"
            h.clock.value += timedelta(seconds=1)
            assert (await client.post("/api/approval/revoke", headers=headers,
                json=action(plan, "revoke-1"))).json() == revoked.json()
            assert (await client.post("/api/approval", headers=headers, json=action(plan, "new-consent"))).json() == revoked.json()
            assert (await read(client, plan)).json()["approval"] == revoked.json()["approval"]
    run(scenario())


def test_new_current_consent_atomically_supersedes_previous():
    async def scenario():
        h = Setup()
        first = await h.seed_current()
        async with h.client() as client:
            headers = await login(client)
            await client.post("/api/approval", headers=headers, json=action(first))
            second = await h.seed_current(2, plan_reference(first))
            assert (await read(client, first)).json()["approval"]["status"] == "superseded"
            response = await client.post("/api/approval", headers=headers, json=action(second, "click-2"))
            assert response.status_code == 200, response.text
            with h.request() as ctx:
                old = _decode(h.documents.data[h.approvals._path(ctx.trusted, plan_reference(first))], "approval", Approval)
                assert old.status == "superseded"
                with pytest.raises(BindingConflict):
                    await h.approvals.require_active(ctx, plan_reference(first), NOW)
    run(scenario())


def test_stale_pointer_race_rechecked_inside_transaction():
    async def scenario():
        h = Setup()
        first = await h.seed_current()
        second = await h.seed(version=2)
        original = h.documents.transact
        raced = False

        async def transact(callback):
            nonlocal raced
            if not raced and callback.__name__ == "record":
                raced = True
                with h.request() as ctx:
                    path = h.plans._current_path(ctx.trusted)
                h.documents.data[path] = _encode("current", plan_reference(second))
            return await original(callback)

        h.documents.transact = transact
        async with h.client() as client:
            headers = await login(client)
            response = await client.post("/api/approval", headers=headers, json=action(first))
            assert raced and response.status_code == 409
            assert not any(p.startswith("cp_approvals/") for p in h.documents.data)
    run(scenario())


def test_guard_uses_supplied_transaction_not_nested_store_and_checks_revocation():
    async def scenario():
        h = Setup()
        plan = await h.seed_current()
        async with h.client() as client:
            headers = await login(client)
            await client.post("/api/approval", headers=headers, json=action(plan))
            with h.request("mcp", run="later-run") as ctx:
                before = h.documents.calls
                guard = h.approvals.transaction_guard()
                assert guard.documents is h.documents
                result = await h.documents.transact(lambda tx: guard.require_active(ctx, tx, plan_reference(plan), NOW))
                assert h.documents.calls == before + 1
                assert result.active_at(NOW)
            await client.post("/api/approval/revoke", headers=headers, json=action(plan, "revoke"))
            with h.request("mcp") as ctx:
                with pytest.raises(BindingConflict):
                    await h.documents.transact(lambda tx: guard.require_active(ctx, tx, plan_reference(plan), NOW))
    run(scenario())


def test_backend_scope_alone_model_dto_copied_expired_and_foreign_capabilities_denied():
    async def scenario():
        h = Setup()
        plan = await h.seed_current()
        with h.request() as ctx:
            approval = Approval(owner_id=ctx.trusted.owner_id, calendar_id=ctx.trusted.calendar_id,
                plan=plan_reference(plan), approved_by=ctx.trusted.principal_id, approved_at=NOW,
                expires_at=NOW + timedelta(minutes=10))
            with pytest.raises(AccessDenied):
                await h.approvals.record_ui_approval(ctx, approval)
            with pytest.raises(TypeError):
                copy(ctx)
            for fake in (ctx.as_port_context(), ctx.as_port_context().model_dump()):
                with pytest.raises(AccessDenied):
                    await h.approvals.approve(fake, plan_reference(plan), "model-approved")
            other = Setup(h.documents)
            with pytest.raises(AccessDenied):
                await other.approvals.approve(ctx, plan_reference(plan), "foreign")
        with pytest.raises(AccessDenied):
            await h.approvals.approve(ctx, plan_reference(plan), "expired-capability")
    run(scenario())


@pytest.mark.parametrize("field", ["owner_id", "signin_principal_id", "config_revision"])
def test_foreign_durable_session_binding_rejected(field):
    async def scenario():
        h = Setup()
        plan = await h.seed_current()
        async with h.client() as client:
            headers = await login(client)
            path = "cp_web_sessions/" + digest(client.cookies.get(SESSION_COOKIE))
            from campuspulse_recovery.web.auth import WebSession
            value = _decode(h.documents.data[path], "web_session", WebSession)
            binding = value.binding.model_copy(update={field: "foreign"})
            h.documents.data[path] = _encode("web_session", value.model_copy(update={"binding": binding}))
            result = await client.post("/api/approval", headers=headers, json=action(plan))
            assert result.status_code == 403
    run(scenario())


def test_cookie_flags_login_challenge_one_use_signout_and_session_expiry():
    async def scenario():
        h = Setup()
        async with h.client() as client:
            headers = await login(client)
            assert client.cookies.get(SESSION_COOKIE)
            assert client.cookies.get(CSRF_COOKIE)
            assert CHALLENGE_COOKIE not in client.cookies
            assert CHALLENGE_CSRF_COOKIE not in client.cookies
            session_cookie = next(c for c in client.cookies.jar if c.name == SESSION_COOKIE)
            assert session_cookie.secure and session_cookie.has_nonstandard_attr("HttpOnly")
            assert session_cookie.get_nonstandard_attr("SameSite") == "strict"
            cookie_snapshot = dict(client.cookies)
            assert (await client.post("/auth/signout", headers=headers)).status_code == 200
            client.cookies.update(cookie_snapshot)
            assert (await client.get("/auth/session")).status_code == 403
            client.cookies.clear()
            await login(client)
            h.clock.value += timedelta(hours=1)
            assert (await client.get("/auth/session")).status_code == 403
        assert "synthetic-signed-token" not in repr(h.documents.data)
    run(scenario())


@pytest.mark.parametrize("case", ["invalid_token", "foreign_principal", "login_csrf",
    "login_csrf_missing", "login_csrf_duplicate", "reused_challenge", "expired_challenge"])
def test_signin_cannot_use_unverified_identity_or_invalid_challenge(case):
    async def scenario():
        h = Setup()
        if case == "foreign_principal":
            async def verify(*args, **kwargs):
                return VerifiedSignIn(principal_id="https://accounts.google.com#foreign", authenticated_at=NOW,
                    expires_at=NOW + timedelta(hours=1))
            h.verifier.verify = verify
        async with h.client() as client:
            challenge = await client.get("/auth/challenge")
            cookie = client.cookies.get(CHALLENGE_COOKIE)
            csrf = challenge.json()["csrf_token"]
            headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}
            if case == "reused_challenge":
                good = await client.post("/auth/signin", headers=headers, json={"credential": "synthetic-signed-token"})
                assert good.status_code == 200
                headers["Cookie"] = f"{CHALLENGE_COOKIE}={cookie}; {CHALLENGE_CSRF_COOKIE}={csrf}"
            if case == "login_csrf": headers["X-CSRF-Token"] = "foreign"
            if case == "login_csrf_missing":
                headers["Cookie"] = f"{CHALLENGE_COOKIE}={cookie}; {CSRF_COOKIE}={csrf}"
            if case == "login_csrf_duplicate":
                headers["Cookie"] = (f"{CHALLENGE_COOKIE}={cookie}; {CHALLENGE_CSRF_COOKIE}={csrf}; "
                    f"{CHALLENGE_CSRF_COOKIE}={csrf}")
            if case == "expired_challenge": h.clock.value += timedelta(minutes=5)
            result = await client.post("/auth/signin", headers=headers,
                json={"credential": "made-up-claims" if case == "invalid_token" else "synthetic-signed-token"})
            assert result.status_code == 403
    run(scenario())


def test_cloud_rejects_development_identity_and_missing_verifier(monkeypatch):
    h = Setup()
    with pytest.raises((ValueError, AccessDenied)):
        h.app(verifier=None)
    with pytest.raises(ValueError):
        h.app(environment="cloud")
    monkeypatch.setenv("K_SERVICE", "synthetic-cloud-service")
    with pytest.raises(ValueError):
        h.app()


def test_routes_never_auto_apply_and_get_never_consents():
    async def scenario():
        h = Setup()
        plan = await h.seed_current()
        async with h.client() as client:
            await login(client)
            assert (await read(client, plan)).json()["approval"] is None
            assert (await client.get("/api/approval")).status_code == 405
            assert (await client.post("/api/apply", json=action(plan))).status_code == 404
            assert not any(p.startswith("cp_approvals/") for p in h.documents.data)
    run(scenario())


@pytest.mark.parametrize("case", ["valid", "signature", "issuer", "subject", "audience", "nonce", "expired", "future"])
def test_google_adapter_calls_official_verifier_with_exact_audience_and_nonce(monkeypatch, case):
    from google.oauth2 import id_token
    client_id = "synthetic-client.apps.googleusercontent.com"
    calls = []

    def verify(token, transport, *, audience):
        calls.append((token, audience))
        if case == "signature":
            raise ValueError("secret token must not be echoed")
        claims = {"iss": "https://accounts.google.com", "sub": "synthetic-subject", "aud": client_id,
            "nonce": "server-nonce", "iat": NOW.timestamp(), "exp": (NOW + timedelta(hours=1)).timestamp()}
        if case == "issuer": claims["iss"] = "https://evil.test"
        if case == "subject": claims["sub"] = ""
        if case == "audience": claims["aud"] = "foreign-client"
        if case == "nonce": claims["nonce"] = "foreign-nonce"
        if case == "expired": claims["exp"] = NOW.timestamp()
        if case == "future": claims["iat"] = (NOW + timedelta(seconds=1)).timestamp()
        return claims

    monkeypatch.setattr(id_token, "verify_oauth2_token", verify)

    async def inline(function):
        return function()
    monkeypatch.setattr("campuspulse_recovery.web.auth.asyncio.to_thread", inline)

    async def scenario():
        verifier = GoogleSignInVerifier(client_id)
        if case == "valid":
            result = await verifier.verify("test-id-token", nonce="server-nonce", now=NOW)
            assert result.principal_id == config().student.allowed_signin_principal
        else:
            with pytest.raises(AccessDenied, match="^signin_rejected$"):
                await verifier.verify("test-id-token", nonce="server-nonce", now=NOW)
        assert calls == [("test-id-token", client_id)]
    run(scenario())


def test_unknown_commit_outcome_retry_uses_original_durable_consent():
    async def scenario():
        h = Setup()
        plan = await h.seed_current()
        original = h.documents.transact
        once = True

        async def ambiguous(callback):
            nonlocal once
            result = await original(callback)
            if once and callback.__name__ == "record":
                once = False
                raise PortError("synthetic_ack_lost")
            return result

        h.documents.transact = ambiguous
        async with h.client() as client:
            headers = await login(client)
            first = await client.post("/api/approval", headers=headers, json=action(plan))
            assert first.status_code == 503
            h.clock.value += timedelta(seconds=5)
            second = await client.post("/api/approval", headers=headers, json=action(plan))
            assert second.status_code == 200
            assert second.json()["approval"]["approved_at"] == NOW.isoformat().replace("+00:00", "Z")
    run(scenario())


def test_duplicate_cookie_and_revoke_key_collision_fail_closed():
    async def scenario():
        h = Setup()
        plan = await h.seed_current()
        async with h.client() as client:
            headers = await login(client)
            await client.post("/api/approval", headers=headers, json=action(plan))
            assert (await client.post("/api/approval/revoke", headers=headers, json=action(plan))).status_code == 409
            duplicate = {**headers, "Cookie": f"{SESSION_COOKIE}={client.cookies.get(SESSION_COOKIE)}; "
                f"{SESSION_COOKIE}={client.cookies.get(SESSION_COOKIE)}; {CSRF_COOKIE}={client.cookies.get(CSRF_COOKIE)}"}
            assert (await client.post("/api/approval", headers=duplicate, json=action(plan))).status_code == 403
    run(scenario())


def test_guard_composes_with_actual_t06_store_without_nested_transactions():
    from campuspulse_recovery.domain.models import ExecutionState
    from campuspulse_recovery.storage.leases import FirestoreCalendarCoordination
    from campuspulse_recovery.storage.operations import FirestoreExecutionStore

    async def scenario():
        h = Setup()
        plan = await h.seed_current()
        leases = FirestoreCalendarCoordination(h.documents, h.policy, clock=h.clock)
        store = FirestoreExecutionStore(h.documents, h.policy, coordination=leases,
            approval_guard=h.approvals.transaction_guard())
        async with h.client() as client:
            headers = await login(client)
            await client.post("/api/approval", headers=headers, json=action(plan))
            with h.request("mcp") as ctx:
                lease = await leases.acquire(ctx, holder_id="synthetic-worker", now=NOW,
                    expires_at=NOW + timedelta(seconds=60))
                state = await store.bind_request(ctx, request_key="apply-test-only", plan=plan_reference(plan), at=NOW)
                new = ExecutionState.model_validate({**state.model_dump(), "status": "approved", "revision": 1})
                before = h.documents.calls
                saved = await store.compare_and_set(ctx, expected_revision=0, execution=new, lease=lease)
                assert saved.status == "approved" and h.documents.calls == before + 1
            await client.post("/api/approval/revoke", headers=headers, json=action(plan, "revoke"))
            with h.request("mcp") as ctx:
                with pytest.raises(BindingConflict):
                    await store._approval(ctx, SimpleNamespace(get=h.documents.get), plan_reference(plan), NOW)
    run(scenario())


class ScheduledDocuments(FakeDocuments):
    """Test-only callback latency/retry, with no external I/O or nested tx.

    A retry runs the first callback against a read-before-write isolated copy,
    drops ALL its writes, then releases scheduling for another transaction.
    The final callback uses the current committed documents via FakeDocuments.
    """
    def arm(self, name, phase):
        self.target, self.phase = name, phase
        self.started, self.resume = asyncio.Event(), asyncio.Event()
        self.discarded_attempts = 0

    async def transact(self, callback):
        if getattr(self, "target", None) == callback.__name__:
            self.target = None
            if self.phase == "retry":
                pending = deepcopy(self.data)
                written = False

                async def get(path):
                    assert not written, "Firestore requires reads before writes"
                    return deepcopy(pending.get(path))

                def create(path, value):
                    nonlocal written
                    written = True
                    assert path not in pending
                    pending[path] = deepcopy(value)

                def set_value(path, value):
                    nonlocal written
                    written = True
                    pending[path] = deepcopy(value)

                await callback(SimpleNamespace(get=get, create=create, set=set_value))
                self.discarded_attempts += 1


            self.started.set()
            await self.resume.wait()
        return await super().transact(callback)


async def invalidate_from_synthetic_ui(store, context, reference, key):

    with store.ui_action(context, reference, key) as click:
        await store.invalidate(context, reference, reason="revoked", at=click.at)


def reconstructed_approval(h, reference, documents):
    return _decode(documents.data[h.approvals._path(SimpleNamespace(
        owner_id=h.config.student.trusted_user_id, calendar_id=h.config.student.calendar_id), reference)],
        "approval", Approval)


@pytest.mark.parametrize("phase", ["entry", "retry"])
def test_delayed_or_retried_callback_supersedes_later_grant_at_fresh_time(phase):
    async def scenario():
        documents = ScheduledDocuments()
        h = Setup(documents)
        a, b = plan_reference(await h.seed()), plan_reference(await h.seed(version=2))
        store = h.approvals
        with h.request() as ctx:
            await h.plans.set_current(ctx, a if phase == "entry" else b, expected=None)
            documents.arm("record", phase)
            delayed = asyncio.create_task(store.approve(ctx, b, "delayed-b"))
            await documents.started.wait()
            assert documents.discarded_attempts == (1 if phase == "retry" else 0)
            assert not any(p.startswith("cp_approvals/") for p in documents.data)
            h.clock.value = NOW + timedelta(seconds=1)
            if phase == "retry":
                await h.plans.set_current(ctx, a, expected=b)
            grant_a = await store.approve(ctx, a, "later-a")
            await h.plans.set_current(ctx, b, expected=a)
            h.clock.value = NOW + timedelta(seconds=2)
            documents.resume.set()
            grant_b = await delayed
            terminal_a = reconstructed_approval(h, a, documents)
            assert terminal_a.status == "superseded"
            assert terminal_a.invalidated_at == h.clock()
            assert terminal_a.invalidated_at >= terminal_a.approved_at == grant_a.approved_at
            assert (terminal_a.approved_at, terminal_a.expires_at) == (NOW + timedelta(seconds=1),
                NOW + timedelta(seconds=1, minutes=10))

            assert (grant_b.approved_at, grant_b.expires_at) == (NOW, NOW + timedelta(minutes=10))
            reloaded = FirestoreApprovalStore(documents, h.policy, clock=h.clock)
            assert await reloaded.get(ctx, a) == terminal_a
            assert await reloaded.approve(ctx, a, "later-a") == terminal_a
            assert await reloaded.require_active(ctx, b, h.clock()) == grant_b
    run(scenario())


@pytest.mark.parametrize("phase", ["entry", "retry"])
def test_queued_new_grant_expired_at_callback_cannot_commit_or_extend_lifetime(phase):
    async def scenario():
        documents = ScheduledDocuments()
        h = Setup(documents)
        a, b = plan_reference(await h.seed()), plan_reference(await h.seed(version=2))
        store = FirestoreApprovalStore(documents, h.policy, clock=h.clock, lifetime=timedelta(seconds=1))
        with h.request() as ctx:
            await h.plans.set_current(ctx, a, expected=None)
            original = await store.approve(ctx, a, "a")
            await h.plans.set_current(ctx, b, expected=a)
            documents.arm("record", phase)
            delayed = asyncio.create_task(store.approve(ctx, b, "queued-b"))
            await documents.started.wait()
            h.clock.value = NOW + timedelta(seconds=2)
            documents.resume.set()
            with pytest.raises(BindingConflict, match="approval_expired_before_commit"):
                await delayed
            assert store._path(ctx.trusted, b) not in documents.data
            assert f"cp_ui_requests/{_key(ctx.trusted.owner_id, ctx.trusted.calendar_id, 'queued-b')}" not in documents.data
            reloaded = FirestoreApprovalStore(documents, h.policy, clock=h.clock, lifetime=timedelta(seconds=1))
            terminal = await reloaded.get(ctx, a)
            assert terminal.status == "expired" and terminal.invalidated_at == original.expires_at
            assert reconstructed_approval(h, a, documents) == terminal
            replay = await reloaded.approve(ctx, a, "a")
            assert replay == terminal
    run(scenario())


def test_delayed_revoke_of_later_grant_uses_callback_time_and_survives_reload():
    async def scenario():
        documents = ScheduledDocuments()
        h = Setup(documents)
        plan = await h.seed_current()
        reference = plan_reference(plan)
        with h.request() as ctx:
            documents.arm("revoke", "entry")
            delayed = asyncio.create_task(invalidate_from_synthetic_ui(h.approvals, ctx, reference, "early-revoke"))
            await documents.started.wait()
            h.clock.value = NOW + timedelta(seconds=1)
            grant = await h.approvals.approve(ctx, reference, "later-grant")
            h.clock.value = NOW + timedelta(seconds=2)
            documents.resume.set()
            await delayed
            value = reconstructed_approval(h, reference, documents)
            assert value.status == "revoked" and value.invalidated_at == h.clock()
            assert value.invalidated_at >= value.approved_at == grant.approved_at
            assert value.expires_at == grant.expires_at
            reloaded = FirestoreApprovalStore(documents, h.policy, clock=h.clock)
            assert await reloaded.get(ctx, reference) == value
            await invalidate_from_synthetic_ui(reloaded, ctx, reference, "early-revoke")
            assert await reloaded.approve(ctx, reference, "later-grant") == value
            with pytest.raises(BindingConflict):
                await reloaded.require_active(ctx, reference, h.clock())
    run(scenario())


def test_retried_revoke_retains_another_committed_terminal_outcome():
    async def scenario():
        documents = ScheduledDocuments()
        h = Setup(documents)
        plan = await h.seed_current()
        reference = plan_reference(plan)
        with h.request() as ctx:
            grant = await h.approvals.approve(ctx, reference, "grant")
            h.clock.value = NOW + timedelta(seconds=1)
            documents.arm("revoke", "retry")
            delayed = asyncio.create_task(invalidate_from_synthetic_ui(h.approvals, ctx, reference, "retried-revoke"))
            await documents.started.wait()
            assert documents.discarded_attempts == 1
            assert reconstructed_approval(h, reference, documents) == grant
            h.clock.value = NOW + timedelta(seconds=2)
            await invalidate_from_synthetic_ui(h.approvals, ctx, reference, "winning-revoke")
            h.clock.value = NOW + timedelta(seconds=3)
            documents.resume.set()
            await delayed
            terminal = reconstructed_approval(h, reference, documents)
            assert terminal.status == "revoked" and terminal.invalidated_at == NOW + timedelta(seconds=2)
            assert (terminal.approved_at, terminal.expires_at) == (grant.approved_at, grant.expires_at)
            reloaded = FirestoreApprovalStore(documents, h.policy, clock=h.clock)
            assert await reloaded.get(ctx, reference) == terminal
            await invalidate_from_synthetic_ui(reloaded, ctx, reference, "retried-revoke")
            assert await reloaded.get(ctx, reference) == terminal
    run(scenario())


@pytest.mark.parametrize("phase", ["entry", "retry"])
def test_revoke_callback_rechecks_exclusive_expiry_after_wait_or_retry(phase):
    async def scenario():
        documents = ScheduledDocuments()
        h = Setup(documents)
        reference = plan_reference(await h.seed_current())
        store = FirestoreApprovalStore(documents, h.policy, clock=h.clock, lifetime=timedelta(seconds=1))
        with h.request() as ctx:
            grant = await store.approve(ctx, reference, "grant")
            documents.arm("revoke", phase)
            delayed = asyncio.create_task(invalidate_from_synthetic_ui(store, ctx, reference, "revoke"))
            await documents.started.wait()
            h.clock.value = grant.expires_at
            documents.resume.set()
            await delayed
            terminal = reconstructed_approval(h, reference, documents)
            assert terminal.status == "expired" and terminal.invalidated_at == grant.expires_at
            assert (terminal.approved_at, terminal.expires_at) == (grant.approved_at, grant.expires_at)
            reloaded = FirestoreApprovalStore(documents, h.policy, clock=h.clock, lifetime=timedelta(seconds=1))
            assert await reloaded.get(ctx, reference) == terminal
            assert await reloaded.approve(ctx, reference, "grant") == terminal
    run(scenario())


def test_observed_pointer_supersession_stays_terminal_if_original_pointer_returns():
    async def scenario():
        h = Setup()
        a, b = plan_reference(await h.seed()), plan_reference(await h.seed(version=2))
        with h.request() as ctx:
            await h.plans.set_current(ctx, a, expected=None)
            grant = await h.approvals.approve(ctx, a, "a")
            await h.plans.set_current(ctx, b, expected=a)
            h.clock.value += timedelta(seconds=1)
            terminal = await h.approvals.get(ctx, a)
            assert terminal.status == "superseded"
            await h.plans.set_current(ctx, a, expected=b)
            h.clock.value += timedelta(seconds=1)
            reloaded = FirestoreApprovalStore(h.documents, h.policy, clock=h.clock)
            assert await reloaded.get(ctx, a) == terminal
            assert await reloaded.approve(ctx, a, "a") == terminal
            assert await reloaded.approve(ctx, a, "new-key") == terminal
            assert (terminal.approved_at, terminal.expires_at) == (grant.approved_at, grant.expires_at)
            with pytest.raises(BindingConflict):
                await reloaded.require_active(ctx, a, h.clock())
    run(scenario())


def test_supersession_samples_time_after_last_transaction_read():
    async def scenario():
        h = Setup()
        a, b = plan_reference(await h.seed_current()), plan_reference(await h.seed(version=2))
        with h.request() as ctx:
            grant = await h.approvals.approve(ctx, a, "a")
            await h.plans.set_current(ctx, b, expected=a)
            original = h.documents.transact
            a_path = h.approvals._path(ctx.trusted, a)
            after_read = NOW + timedelta(seconds=1)

            async def transact(callback):
                if callback.__name__ != "record":
                    return await original(callback)

                async def invoke(tx):
                    async def get(path):
                        value = await tx.get(path)
                        if path == a_path:

                            h.clock.value = after_read
                        return value
                    return await callback(SimpleNamespace(get=get, create=tx.create, set=tx.set))
                return await original(invoke)

            h.documents.transact = transact
            grant_b = await h.approvals.approve(ctx, b, "b")
            terminal = reconstructed_approval(h, a, h.documents)
            assert terminal.invalidated_at == after_read
            assert terminal.approved_at == grant.approved_at
            assert grant_b.approved_at == NOW and grant_b.expires_at == NOW + timedelta(minutes=10)
            reloaded = FirestoreApprovalStore(h.documents, h.policy, clock=h.clock)
            assert await reloaded.get(ctx, a) == terminal
    run(scenario())
