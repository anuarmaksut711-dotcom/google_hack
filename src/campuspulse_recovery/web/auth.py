"""Verified sign-in and durable opaque server sessions for T04.

There is no development identity/header bypass. Injected test verifiers are
accepted only in explicit local mode, outside all configured cloud markers.
Tokens are never logged or persisted; only random cookie digests are stored.
"""

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import hmac
import os
import secrets
from typing import Callable, Protocol
from urllib.parse import urlsplit
from uuid import uuid4

from ..config import CLOUD_MARKERS
from ..domain.models import Contract, Identifier, Instant
from ..domain.ports import BindingConflict
from ..domain.time_rules import instant
from ..security.context import AccessDenied, AuthenticatedCaller, RequestContextFactory, ServerRunBinding
from ..storage.plans import _Repository, _decode, _encode

SESSION_COOKIE = "__Host-cp-session"
CSRF_COOKIE = "__Host-cp-csrf"
CHALLENGE_COOKIE = "__Host-cp-login"


def digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


class VerifiedSignIn(Contract):
    principal_id: Identifier
    authenticated_at: Instant
    expires_at: Instant


class SignInVerifier(Protocol):
    async def verify(self, token: str, *, nonce: str, now: datetime) -> VerifiedSignIn: ...


class GoogleSignInVerifier:
    """Official verifier checks signature/certs, audience, issuer and validity.

    Token nonce additionally binds Google Identity Services' sign-in response
    to the one-use server challenge. Email never selects the student identity.
    """
    def __init__(self, client_id: str):
        if not client_id or not client_id.endswith(".apps.googleusercontent.com"):
            raise ValueError("explicit_google_client_id_required")
        self.client_id = client_id

    async def verify(self, token: str, *, nonce: str, now: datetime) -> VerifiedSignIn:
        def check():
            import requests
            from google.auth.transport.requests import Request
            from google.oauth2.id_token import verify_oauth2_token

            class BoundedRequest(Request):
                def __call__(self, *args, **kwargs):
                    kwargs['timeout'] = 5
                    return super().__call__(*args, **kwargs)
            try:
                with requests.Session() as transport:
                    claims = verify_oauth2_token(token, BoundedRequest(session=transport), audience=self.client_id)
                if (claims.get("iss") not in ("accounts.google.com", "https://accounts.google.com")
                        or claims.get("aud") != self.client_id
                        or not isinstance(claims.get("sub"), str) or not claims["sub"]
                        or claims.get("nonce") != nonce):
                    raise ValueError()
                value = VerifiedSignIn(principal_id="https://accounts.google.com#" + claims["sub"],
                    authenticated_at=datetime.fromtimestamp(claims["iat"], UTC),
                    expires_at=datetime.fromtimestamp(claims["exp"], UTC))
                if not value.authenticated_at <= now < value.expires_at:
                    raise ValueError()
                return value
            except Exception:
                raise AccessDenied("signin_rejected") from None

        try:
            async with asyncio.timeout(8):
                return await asyncio.to_thread(check)
        except TimeoutError:
            raise AccessDenied('signin_unavailable') from None


class LoginChallenge(Contract):
    nonce: Identifier
    csrf_digest: Identifier
    expires_at: Instant
    consumed: bool = False


class WebSession(Contract):
    binding: ServerRunBinding
    principal_id: Identifier
    authenticated_at: Instant
    expires_at: Instant
    csrf_digest: Identifier
    revoked: bool = False


@dataclass(frozen=True)
class SessionCredential:
    """Internal local handle. It is never accepted from HTTP JSON/headers."""
    session: WebSession
    authority: object


class WebAuth(_Repository):
    def __init__(self, documents, policy, *, verifier: SignInVerifier, origin: str,
                 environment: str, clock: Callable[[], datetime] = lambda: datetime.now(UTC),
                 email_credentials=None):
        super().__init__(documents, policy)
        policy.config.require_environment(environment)
        if verifier is None:
            raise ValueError("signin_verifier_required")
        if (environment == "cloud" or CLOUD_MARKERS.intersection(os.environ)) and type(verifier) is not GoogleSignInVerifier:
            raise ValueError("production_google_verifier_required")
        if email_credentials is not None:
            from .passwords import EmailCredentials

            if type(email_credentials) is not EmailCredentials or email_credentials.documents is not documents:
                raise ValueError("matching_email_credentials_required")
        parts = urlsplit(origin)
        if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
                or parts.query or parts.fragment or parts.path or parts.netloc != parts.netloc.lower()):
            raise ValueError("explicit_https_origin_required")
        self.origin, self.host = origin, parts.netloc
        self.verifier, self.clock = verifier, clock
        self.email_credentials = email_credentials
        self._authority = object()

    def now(self):
        return instant(self.clock())

    def check_request(self, request, *, mutation=False):

        if request.url.scheme != "https" or request.headers.get("host") != self.host:
            raise AccessDenied("request_origin_rejected")
        if request.headers.get("sec-fetch-site") not in (None, "same-origin", "none"):
            raise AccessDenied("cross_site_request_rejected")
        supplied = request.headers.get("origin")
        if (mutation or supplied is not None) and supplied != self.origin:
            raise AccessDenied("request_origin_rejected")

    def check_login_navigation(self, request, *, additional_paths=()):
        """Accept external top-level links to public authentication documents.

        This grants no identity, challenge or session. All authentication and
        private routes retain check_request/check_csrf.
        """
        mode = request.headers.get("sec-fetch-mode")
        destination = request.headers.get("sec-fetch-dest")
        site = request.headers.get("sec-fetch-site")
        supplied = request.headers.get("origin")
        if (request.method != "GET" or request.url.path not in ("/login", "/register", *additional_paths)
                or request.url.scheme != "https" or request.headers.get("host") != self.host
                or mode not in (None, "navigate") or destination not in (None, "document")
                or supplied not in (None, self.origin)
                or (site not in (None, "same-origin", "none")
                    and (mode != "navigate" or destination != "document"))):
            raise AccessDenied("request_origin_rejected")

    def check_csrf(self, request, expected: str):
        self.check_request(request, mutation=True)
        header, cookie = request.headers.get("x-csrf-token"), request.cookies.get(CSRF_COOKIE)
        if (not header or not cookie or len(header) > 256 or not header.isascii() or not cookie.isascii()
                or not hmac.compare_digest(header, cookie)
                or not hmac.compare_digest(digest(header), expected)):
            raise AccessDenied("csrf_rejected")

    def _cookie(self, request, name):
        value = request.cookies.get(name)

        count = sum(part.strip().split("=", 1)[0] == name for part in request.headers.get("cookie", "").split(";"))
        if not value or len(value) > 256 or count != 1:
            raise AccessDenied("session_required")
        return value

    async def challenge(self):
        handle, csrf, nonce = (secrets.token_urlsafe(32) for _ in range(3))
        value = LoginChallenge(nonce=nonce, csrf_digest=digest(csrf), expires_at=self.now() + timedelta(minutes=5))

        async def create(tx):
            tx.create("cp_web_challenges/" + digest(handle), _encode("web_challenge", value))

        await self.documents.transact(create)
        return handle, csrf, nonce

    async def challenge_for_signin(self, request):
        """Validate the one-use browser challenge before expensive identity work.

        This read is not authorization to issue a session. Issuance always
        rechecks and consumes the exact challenge in its durable transaction.
        """
        self.check_request(request, mutation=True)
        handle = self._cookie(request, CHALLENGE_COOKIE)
        path = "cp_web_challenges/" + digest(handle)
        challenge = _decode(await self.documents.get(path), "web_challenge", LoginChallenge)
        self._cookie(request, CSRF_COOKIE)
        self.check_csrf(request, challenge.csrf_digest)
        if challenge.consumed or self.now() >= challenge.expires_at:
            raise AccessDenied("signin_challenge_expired")
        return challenge

    async def signin(self, request, token: str):
        challenge = await self.challenge_for_signin(request)
        try:
            identity = VerifiedSignIn.model_validate(await self.verifier.verify(token, nonce=challenge.nonce, now=self.now()))
        except Exception:
            raise AccessDenied("signin_rejected") from None
        return await self._issue_session(request, challenge, identity)

    async def signin_password(self, request, proof):
        """Accept only a configured credential service's internal capability.

        No HTTP field, principal identifier or model claim can substitute for
        the password service's nonce-bound proof.
        """
        if self.email_credentials is None:
            raise AccessDenied("email_signin_unavailable")
        challenge = await self.challenge_for_signin(request)

        def current_identity():
            try:
                return VerifiedSignIn.model_validate(self.email_credentials.check(
                    proof, nonce=challenge.nonce, now=self.now()))
            except Exception:
                raise AccessDenied("signin_rejected") from None

        identity = current_identity()
        return await self._issue_session(request, challenge, identity, recheck_identity=current_identity)

    async def _issue_session(self, request, challenge, identity, *, recheck_identity=None):
        try:
            self.policy.require_principal("backend", identity.principal_id)
            if not identity.authenticated_at <= self.now() < identity.expires_at:
                raise ValueError()
        except Exception:
            raise AccessDenied("signin_rejected") from None
        path = "cp_web_challenges/" + digest(self._cookie(request, CHALLENGE_COOKIE))
        cookie, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        expires = min(identity.expires_at, self.now() + timedelta(hours=1))
        binding = ServerRunBinding(owner_id=self.policy.config.student.trusted_user_id,
            signin_principal_id=identity.principal_id, session_id="ses_" + uuid4().hex,
            run_id="run_" + uuid4().hex, config_revision=self.policy.config.config_revision, expires_at=expires)
        session = WebSession(binding=binding, principal_id=identity.principal_id,
            authenticated_at=identity.authenticated_at, expires_at=expires, csrf_digest=digest(csrf))

        async def create(tx):
            latest = _decode(await tx.get(path), "web_challenge", LoginChallenge)
            if latest != challenge or latest.consumed or self.now() >= latest.expires_at or self.now() >= expires:
                raise AccessDenied("signin_challenge_expired")
            if recheck_identity is not None and recheck_identity() != identity:
                raise AccessDenied("signin_rejected")
            tx.set(path, _encode("web_challenge", latest.model_copy(update={"consumed": True})))
            tx.create("cp_web_sessions/" + digest(cookie), _encode("web_session", session))
            tx.create("cp_web_session_index/" + digest(binding.session_id),
                      {"path": "cp_web_sessions/" + digest(cookie)})

        await self.documents.transact(create)
        return cookie, csrf, session

    async def load(self, request, *, mutation=False):
        self.check_request(request, mutation=mutation)
        path = "cp_web_sessions/" + digest(self._cookie(request, SESSION_COOKIE))
        try:
            session = _decode(await self.documents.get(path), "web_session", WebSession)
            now = self.now()
            self.policy.require_principal("backend", session.principal_id)
            self.policy.require_binding(session.binding, session_id=session.binding.session_id,
                run_id=session.binding.run_id, now=now)
            if (session.revoked or not session.authenticated_at <= now < session.expires_at
                    or session.binding.signin_principal_id != session.principal_id
                    or session.binding.expires_at != session.expires_at):
                raise ValueError()
        except Exception:
            raise AccessDenied("session_rejected") from None
        if mutation:

            self._cookie(request, CSRF_COOKIE)
            self.check_csrf(request, session.csrf_digest)
        return SessionCredential(session, self._authority)

    @contextmanager
    def request_scope(self, credential):

        if type(credential) is not SessionCredential or credential.authority is not self._authority:
            raise AccessDenied("server_session_credential_required")
        session = credential.session
        authority = self

        class Authenticator:
            def authenticate(self, actual, *, boundary, now):
                if actual is not credential or boundary != "backend":
                    raise AccessDenied("server_session_credential_required")
                return AuthenticatedCaller(principal_id=session.principal_id, session_id=session.binding.session_id,
                    run_id=session.binding.run_id, authenticated_at=session.authenticated_at, expires_at=session.expires_at)

        class Bindings:
            def resolve(self, *, session_id, run_id):
                return session.binding if (session_id, run_id) == (session.binding.session_id, session.binding.run_id) else None

        factory = RequestContextFactory(authority.policy, boundary="backend", authenticator=Authenticator(),
            bindings=Bindings(), clock=self.clock)
        with factory.request(credential) as context:
            yield context

    async def signout(self, request):
        await self.load(request, mutation=True)
        path = "cp_web_sessions/" + digest(self._cookie(request, SESSION_COOKIE))

        async def revoke(tx):
            session = _decode(await tx.get(path), "web_session", WebSession)
            tx.set(path, _encode("web_session", session.model_copy(update={"revoked": True})))

        await self.documents.transact(revoke)
