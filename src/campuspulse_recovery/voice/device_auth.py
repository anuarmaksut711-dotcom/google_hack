"""Bounded device pairing rooted in the existing durable browser session.

Only the injected DocumentStore performs I/O. Transaction callbacks contain
document reads/writes only; secrets/randomness are created before callbacks.
This capability is deliberately not a WebAuth SessionCredential or plan consent.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import base64
import hmac
import re
import secrets
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import Field, StrictBool, StrictInt, StringConstraints

from ..domain.models import Contract, Digest, Identifier, Instant
from ..domain.ports import PortError
from ..security.context import AccessDenied, ServerRunBinding
from ..storage.plans import _Repository, _decode, _encode
from ..web.auth import SESSION_COOKIE, WebAuth, WebSession, digest

PAIR_LIFETIME = timedelta(minutes=5)
VOICE_LIFETIME = timedelta(minutes=20)
WINDOW = timedelta(minutes=5)
MAX_PROOF_ATTEMPTS = 5
SAFE_ERROR = "voice_access_denied"
READ_TOOLS = frozenset({"read_assignments", "read_calendar", "solve_repair"})
Challenge = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9_-]{43}$")]
DeviceName = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=80)]


def _reject():
    raise AccessDenied(SAFE_ERROR) from None


def proof_challenge(verifier: str) -> str:
    """S256, ASCII RFC 7636 verifier alphabet, 43..128 characters."""
    if not isinstance(verifier, str) or re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier) is None:
        _reject()
    return base64.urlsafe_b64encode(bytes.fromhex(digest(verifier))).rstrip(b"=").decode("ascii")


def _secret(value):
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]{43}", value) is None:
        _reject()
    return value


def _code(value):
    if not isinstance(value, str) or re.fullmatch(r"[A-HJ-NP-Z2-9]{12}", value) is None:
        _reject()
    return value


class Pairing(Contract):
    code_hash: Digest
    challenge: Challenge
    client_kind: Literal["windows", "iphone"]
    device_name: DeviceName
    device_id: Identifier
    expires_at: Instant
    state: Literal["pending", "approved", "denied", "redeemed", "revoked"] = "pending"
    attempts: Annotated[StrictInt, Field(ge=0, le=MAX_PROOF_ATTEMPTS)] = 0
    parent_hash: Digest | None = None
    binding: ServerRunBinding | None = None
    credential_hash: Digest | None = None


class CodeIndex(Contract):
    grant_hash: Digest
    expires_at: Instant


class VoiceCredential(Contract):
    parent_hash: Digest
    binding: ServerRunBinding
    device_id: Identifier
    client_kind: Literal["windows", "iphone"]
    expires_at: Instant
    scope: Literal["voice:read"] = "voice:read"
    revoked: StrictBool = False


class Budget(Contract):
    expires_at: Instant
    count: Annotated[StrictInt, Field(ge=0, le=120)]


@dataclass(frozen=True, repr=False)
class PairingOffer:
    pairing_code: str
    grant: str
    expires_at: datetime


@dataclass(frozen=True, repr=False)
class IssuedCredential:
    credential: str
    expires_at: datetime
    scope: str = "voice:read"


@dataclass(frozen=True, repr=False)
class DeviceView:
    device_name: str
    client_kind: str
    expires_at: datetime


@dataclass(frozen=True)
class VoiceAccess:
    """Server-loaded identity for W04/W07; never accepted as client claims.

    Consumers must call authenticate again before tools/at heartbeat boundaries.
    This object is not accepted by recovery approval/context APIs.
    """
    device_id: str
    binding: ServerRunBinding = field(repr=False)
    expires_at: datetime = field(repr=False)
    scope: str = "voice:read"

    def require_tool(self, name: str):
        if name not in READ_TOOLS:
            _reject()


class DeviceAuth(_Repository):
    """No in-memory production fallback. Inject the *same* WebAuth/store/policy.

    Test doubles are explicit local WebAuth dependencies. Production uses the
    existing FirestoreDocuments adapter and verified Google browser sign-in.
    """

    def __init__(self, auth: WebAuth):
        super().__init__(auth.documents, auth.policy)
        self.auth = auth

    def _pair_path(self, grant_hash):
        return "cp_voice_pairings/" + grant_hash

    def _credential_path(self, credential_hash):
        return "cp_voice_credentials/" + credential_hash

    def _budget_path(self, kind, subject="global"):
        return "cp_voice_budgets/" + digest(kind + ":" + subject)

    def _budget(self, raw, now, limit):
        budget = None if raw is None else _decode(raw, "voice_budget", Budget)
        if budget is None or now >= budget.expires_at:
            budget = Budget(count=0, expires_at=now + WINDOW)
        if budget.count >= limit:
            _reject()
        return budget.model_copy(update={"count": budget.count + 1})

    def _check_parent(self, parent, now):
        self.policy.require_principal("backend", parent.principal_id)
        b = parent.binding
        self.policy.require_binding(b, session_id=b.session_id, run_id=b.run_id, now=now)
        if (parent.revoked or not parent.authenticated_at <= now < parent.expires_at
                or b.signin_principal_id != parent.principal_id or b.expires_at != parent.expires_at):
            _reject()

    async def _browser(self, request):
        try:
            credential = await self.auth.load(request, mutation=True)
            with self.auth.request_scope(credential) as context:
                self.policy.require_context(context)
            return digest(self.auth._cookie(request, SESSION_COOKIE)), credential.session
        except Exception:
            _reject()

    async def _run(self, callback):
        try:
            return await self.documents.transact(callback)
        except PortError:

            raise PortError("voice_storage_unavailable") from None
        except Exception:
            _reject()

    async def start(self, *, challenge: str, client_kind: str, device_name: str):
        _secret(challenge)
        if (client_kind not in ("windows", "iphone") or not isinstance(device_name, str)
                or not 1 <= len(device_name) <= 80 or any(ord(c) < 32 or 127 <= ord(c) < 160 for c in device_name)):
            _reject()
        code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(12))
        grant = secrets.token_urlsafe(32)
        value = Pairing(code_hash=digest(code), challenge=challenge, client_kind=client_kind,
            device_name=device_name, device_id="dev_" + uuid4().hex,
            expires_at=self.auth.now() + PAIR_LIFETIME)
        path, index = self._pair_path(digest(grant)), "cp_voice_codes/" + digest(code)
        budget_path = self._budget_path("start")

        async def create_pairing(tx):
            raw = await tx.get(budget_path)
            now = self.auth.now()
            if now >= value.expires_at:
                _reject()
            budget = self._budget(raw, now, 20)
            tx.set(budget_path, _encode("voice_budget", budget))
            tx.create(path, _encode("voice_pairing", value))
            tx.create(index, _encode("voice_code", CodeIndex(grant_hash=digest(grant), expires_at=value.expires_at)))

        await self._run(create_pairing)
        return PairingOffer(code, grant, value.expires_at)

    async def _browser_pairing(self, request, code, *, decision=None):
        code = _code(code)
        parent_hash, expected = await self._browser(request)
        budget_path = self._budget_path("browser", parent_hash)

        async def browser_pairing(tx):
            parent = _decode(await tx.get("cp_web_sessions/" + parent_hash), "web_session", WebSession)
            raw_budget = await tx.get(budget_path)
            raw_index = await tx.get("cp_voice_codes/" + digest(code))
            index = None if raw_index is None else _decode(raw_index, "voice_code", CodeIndex)
            raw = None if index is None else await tx.get(self._pair_path(index.grant_hash))
            pair = None if raw is None else _decode(raw, "voice_pairing", Pairing)
            now = self.auth.now()
            self._check_parent(parent, now)
            if parent != expected:
                _reject()
            budget = self._budget(raw_budget, now, 20)
            valid = (pair is not None and now < pair.expires_at and now < index.expires_at
                and pair.code_hash == digest(code) and pair.state == "pending"
                and pair.parent_hash in (None, parent_hash))
            if decision is not None:
                valid = valid and pair is not None and pair.parent_hash == parent_hash and pair.binding == parent.binding
            tx.set(budget_path, _encode("voice_budget", budget))
            if not valid:
                return None
            updates = {"parent_hash": parent_hash, "binding": parent.binding}
            if decision is not None:
                updates["state"] = "approved" if decision else "denied"
            tx.set(self._pair_path(index.grant_hash), _encode("voice_pairing", pair.model_copy(update=updates)))
            return DeviceView(pair.device_name, pair.client_kind, pair.expires_at)

        result = await self._run(browser_pairing)
        if result is None:
            _reject()
        return result

    async def preview(self, request, code):
        return await self._browser_pairing(request, code)

    async def decide(self, request, code, *, approve: bool):
        if type(approve) is not bool:
            _reject()
        await self._browser_pairing(request, code, decision=approve)

    async def exchange(self, *, grant: str, verifier: str):
        grant_hash = digest(_secret(grant))

        try:
            challenge = proof_challenge(verifier)
        except AccessDenied:
            challenge = ""
        token = secrets.token_urlsafe(32)
        token_hash = digest(token)
        issued_at = self.auth.now()
        budget_path = self._budget_path("exchange")

        async def redeem_pairing(tx):
            raw = await tx.get(self._pair_path(grant_hash))
            pair = None if raw is None else _decode(raw, "voice_pairing", Pairing)
            raw_budget = await tx.get(budget_path)
            parent = None
            if pair is not None and pair.parent_hash is not None:
                parent = _decode(await tx.get("cp_web_sessions/" + pair.parent_hash), "web_session", WebSession)
            now = self.auth.now()
            budget = self._budget(raw_budget, now, 120)
            valid = (pair is not None and pair.state == "approved" and now < pair.expires_at
                and pair.attempts < MAX_PROOF_ATTEMPTS and parent is not None)
            if valid:
                self._check_parent(parent, now)
                valid = pair.binding == parent.binding
            matches = pair is not None and hmac.compare_digest(pair.challenge, challenge)
            expires = min(parent.expires_at, issued_at + VOICE_LIFETIME) if parent else issued_at
            valid = valid and matches and now < expires
            tx.set(budget_path, _encode("voice_budget", budget))
            if not valid:
                if pair is not None and pair.state in ("pending", "approved"):
                    attempts = pair.attempts + 1
                    tx.set(self._pair_path(grant_hash), _encode("voice_pairing", pair.model_copy(update={
                        "attempts": attempts, "state": "revoked" if attempts >= MAX_PROOF_ATTEMPTS else pair.state})))
                return None
            value = VoiceCredential(parent_hash=pair.parent_hash, binding=pair.binding,
                device_id=pair.device_id, client_kind=pair.client_kind, expires_at=expires)
            tx.set(self._pair_path(grant_hash), _encode("voice_pairing", pair.model_copy(update={
                "state": "redeemed", "credential_hash": token_hash})))
            tx.create(self._credential_path(token_hash), _encode("voice_credential", value))
            return value

        try:
            value = await self._run(redeem_pairing)
        except PortError:


            async def reconcile_issuance(tx):
                pair = _decode(await tx.get(self._pair_path(grant_hash)), "voice_pairing", Pairing)
                raw = await tx.get(self._credential_path(token_hash))
                value = None if raw is None else _decode(raw, "voice_credential", VoiceCredential)
                parent = None if value is None else _decode(await tx.get(
                    "cp_web_sessions/" + value.parent_hash), "web_session", WebSession)
                now = self.auth.now()
                if (pair.state != "redeemed" or pair.credential_hash != token_hash or value is None
                        or value.revoked or now >= value.expires_at or pair.binding != value.binding
                        or pair.parent_hash != value.parent_hash or pair.device_id != value.device_id):
                    raise PortError("voice_storage_unavailable")
                self._check_parent(parent, now)
                if value.binding != parent.binding:
                    _reject()
                return value
            value = await self._run(reconcile_issuance)
        if value is None:
            _reject()
        return IssuedCredential(token, value.expires_at)

    async def authenticate(self, token: str) -> VoiceAccess:
        path = self._credential_path(digest(_secret(token)))

        async def check_voice(tx):
            value = _decode(await tx.get(path), "voice_credential", VoiceCredential)
            parent = _decode(await tx.get("cp_web_sessions/" + value.parent_hash), "web_session", WebSession)
            now = self.auth.now()
            self._check_parent(parent, now)
            if value.revoked or now >= value.expires_at or value.binding != parent.binding or value.scope != "voice:read":
                _reject()
            return VoiceAccess(value.device_id, value.binding, min(value.expires_at, parent.expires_at))

        return await self._run(check_voice)

    async def revoke(self, request, code: str):
        """Browser revokes its exact paired device using its original code."""
        code = _code(code)
        parent_hash, expected = await self._browser(request)
        budget_path = self._budget_path("revoke", parent_hash)

        async def revoke_pairing(tx):
            raw_index = await tx.get("cp_voice_codes/" + digest(code))
            index = None if raw_index is None else _decode(raw_index, "voice_code", CodeIndex)
            raw_pair = None if index is None else await tx.get(self._pair_path(index.grant_hash))
            pair = None if raw_pair is None else _decode(raw_pair, "voice_pairing", Pairing)
            parent = _decode(await tx.get("cp_web_sessions/" + parent_hash), "web_session", WebSession)
            credential = None if pair is None or pair.credential_hash is None else _decode(await tx.get(
                self._credential_path(pair.credential_hash)), "voice_credential", VoiceCredential)
            raw_budget = await tx.get(budget_path)
            now = self.auth.now()
            self._check_parent(parent, now)
            if parent != expected:
                _reject()
            budget = self._budget(raw_budget, now, 20)
            valid = (pair is not None and pair.code_hash == digest(code)
                and pair.parent_hash == parent_hash and pair.binding == parent.binding)
            if credential is not None:
                valid = valid and (credential.parent_hash, credential.binding, credential.device_id) == (
                    parent_hash, parent.binding, pair.device_id)
            tx.set(budget_path, _encode("voice_budget", budget))
            if not valid:
                return False
            tx.set(self._pair_path(index.grant_hash), _encode("voice_pairing", pair.model_copy(update={"state": "revoked"})))
            if credential is not None:
                tx.set(self._credential_path(pair.credential_hash), _encode("voice_credential",
                    credential.model_copy(update={"revoked": True})))
            return True

        if not await self._run(revoke_pairing):
            _reject()
