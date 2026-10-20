"""Password capability sessions use the same durable challenge and owner guard.

These tests use synthetic accounts and an atomic fake document adapter, never
Google credentials, a deployed Firestore instance or browser-provided identity.
"""

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from campuspulse_recovery.config import AppConfig
from campuspulse_recovery.security.context import AccessDenied
from campuspulse_recovery.security.resources import ResourcePolicy
from campuspulse_recovery.storage.plans import _decode
from campuspulse_recovery.web.auth import (
    CHALLENGE_COOKIE, CSRF_COOKIE, SESSION_COOKIE, LoginChallenge,
    VerifiedSignIn, WebAuth, digest,
)
from campuspulse_recovery.web.passwords import EmailCredentials
from tests.storage.test_plans import FakeDocuments, NOW, config
from tests.web.test_approval import Clock, ORIGIN, SyntheticVerifier


def request(handle, csrf, *, session=None, token=None, origin=ORIGIN, site="same-origin"):
    cookies = f"{CHALLENGE_COOKIE}={handle}; {CSRF_COOKIE}={csrf}"
    if session is not None:
        cookies += f"; {SESSION_COOKIE}={session}"
    headers = {"host": "campus.test", "cookie": cookies, "origin": origin,
               "sec-fetch-site": site, "x-csrf-token": csrf if token is None else token}
    return Request({"type": "http", "method": "POST", "scheme": "https",
                    "path": "/auth/email/signin", "query_string": b"",
                    "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
                    "server": ("campus.test", 443)})


def policy(principal=None):
    value = config().model_dump()
    if principal is not None:
        value["student"]["allowed_signin_principal"] = principal
        value["student"]["trusted_user_id"] = "synthetic-email-owner"
    return ResourcePolicy(AppConfig.model_validate(value), environment="local")


def auth(documents, clock, credentials=None, principal=None):
    return WebAuth(documents, policy(principal), verifier=SyntheticVerifier(),
                   origin=ORIGIN, environment="local", clock=clock,
                   email_credentials=credentials)


async def setup():
    documents, clock = FakeDocuments(), Clock()
    credentials = EmailCredentials(documents, clock=clock)
    bootstrap = auth(documents, clock, credentials)
    handle, csrf, nonce = await bootstrap.challenge()
    proof = await credentials.register("student@example.test", "synthetic password phrase", nonce=nonce)
    owner = auth(documents, clock, credentials, proof.principal_id)
    return SimpleNamespace(documents=documents, clock=clock, credentials=credentials,
                           bootstrap=bootstrap, owner=owner, handle=handle, csrf=csrf,
                           nonce=nonce, proof=proof)


def test_password_session_keeps_exact_owner_and_request_scope_and_replay_is_rejected():
    async def scenario():
        h = await setup()
        browser = request(h.handle, h.csrf)
        cookie, csrf, session = await h.owner.signin_password(browser, h.proof)
        assert session.principal_id == h.proof.principal_id
        assert session.binding.owner_id == "synthetic-email-owner"
        assert session.binding.signin_principal_id == h.proof.principal_id
        loaded = await h.owner.load(request(h.handle, csrf, session=cookie), mutation=True)
        with h.owner.request_scope(loaded) as context:
            assert h.owner.policy.require_context(context).owner_id == "synthetic-email-owner"
        with pytest.raises(AccessDenied, match="signin_challenge_expired"):
            await h.owner.signin_password(browser, h.proof)
        with pytest.raises(AccessDenied, match="session_rejected"):
            await h.bootstrap.load(request(h.handle, csrf, session=cookie))

    asyncio.run(scenario())


def test_password_capability_cannot_select_another_policy_or_replace_verification():
    async def scenario():
        h = await setup()
        browser = request(h.handle, h.csrf)
        with pytest.raises(AccessDenied, match="signin_rejected"):
            await h.bootstrap.signin_password(browser, h.proof)
        claim = VerifiedSignIn(principal_id=h.proof.principal_id, authenticated_at=NOW,
                               expires_at=NOW + timedelta(hours=1))
        with pytest.raises(AccessDenied, match="signin_rejected"):
            await h.owner.signin_password(browser, claim)
        other = EmailCredentials(h.documents, clock=h.clock)
        other_auth = auth(h.documents, h.clock, other, h.proof.principal_id)
        with pytest.raises(AccessDenied, match="signin_rejected"):
            await other_auth.signin_password(browser, h.proof)
        assert not any(p.startswith("cp_web_sessions/") for p in h.documents.data)
        await h.owner.signin_password(browser, h.proof)

    asyncio.run(scenario())


@pytest.mark.parametrize("case", ["csrf", "origin", "cross_site", "nonce", "expired"])
def test_password_session_requires_current_same_origin_csrf_and_nonce(case):
    async def scenario():
        h = await setup()
        options = {}
        if case == "csrf":
            options["token"] = "foreign-token"
        if case == "origin":
            options["origin"] = "https://foreign.test"
        if case == "cross_site":
            options["site"] = "cross-site"
        if case == "nonce":
            h.handle, h.csrf, _ = await h.owner.challenge()
        if case == "expired":
            h.clock.value += timedelta(minutes=5)
        with pytest.raises(AccessDenied):
            await h.owner.signin_password(request(h.handle, h.csrf, **options), h.proof)
        assert not any(p.startswith("cp_web_sessions/") for p in h.documents.data)

    asyncio.run(scenario())


def test_password_session_proof_rechecked_after_transaction_delay_before_any_write():
    async def scenario():
        h = await setup()
        original = h.documents.transact

        async def delayed(callback):
            h.clock.value += timedelta(seconds=61)
            return await original(callback)

        h.documents.transact = delayed
        with pytest.raises(AccessDenied, match="signin_rejected"):
            await h.owner.signin_password(request(h.handle, h.csrf), h.proof)
        challenge = _decode(h.documents.data["cp_web_challenges/" + digest(h.handle)],
                            "web_challenge", LoginChallenge)
        assert not challenge.consumed
        assert not any(p.startswith("cp_web_sessions/") for p in h.documents.data)

    asyncio.run(scenario())


def test_concurrent_password_session_attempts_consume_challenge_once():
    async def scenario():
        h = await setup()
        results = await asyncio.gather(
            h.owner.signin_password(request(h.handle, h.csrf), h.proof),
            h.owner.signin_password(request(h.handle, h.csrf), h.proof),
            return_exceptions=True)
        assert sum(isinstance(result, tuple) for result in results) == 1
        assert sum(isinstance(result, AccessDenied) for result in results) == 1
        assert sum(p.startswith("cp_web_sessions/") for p in h.documents.data) == 1

    asyncio.run(scenario())


def test_email_session_service_requires_exact_configured_store_and_google_guard(monkeypatch):
    documents, clock = FakeDocuments(), Clock()
    credentials = EmailCredentials(documents, clock=clock)
    with pytest.raises(ValueError, match="matching_email_credentials_required"):
        auth(documents, clock, SimpleNamespace(documents=documents))
    with pytest.raises(ValueError, match="matching_email_credentials_required"):
        auth(FakeDocuments(), clock, credentials)
    cloud_config = config().model_dump()
    cloud_config["mode"] = "cloud"
    cloud_policy = ResourcePolicy(AppConfig.model_validate(cloud_config), environment="cloud")
    monkeypatch.setenv("K_SERVICE", "synthetic-cloud-service")
    with pytest.raises(ValueError, match="production_google_verifier_required"):
        WebAuth(documents, cloud_policy, verifier=SyntheticVerifier(), origin=ORIGIN,
                environment="cloud", clock=clock, email_credentials=credentials)


def test_email_session_unavailable_without_explicit_credential_service():
    async def scenario():
        documents, clock = FakeDocuments(), Clock()
        google_only = auth(documents, clock)
        handle, csrf, _ = await google_only.challenge()
        with pytest.raises(AccessDenied, match="email_signin_unavailable"):
            await google_only.signin_password(request(handle, csrf), object())
        assert not any(p.startswith("cp_web_sessions/") for p in documents.data)

    asyncio.run(scenario())
