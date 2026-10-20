"""First-party email credentials and short-lived internal sign-in proofs.

Email is an unverified login identifier, never a Google subject or an access
grant to external resources. Only salted scrypt verifiers and hashed email
lookup keys are durable. Proofs stay inside the server; HTTP cannot mint one.
"""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import scrypt, sha256
import hmac
import math
import re
import secrets
from typing import Callable, Literal
from uuid import uuid4

from pydantic import Field

from ..domain.models import Contract, Identifier, Instant
from ..domain.time_rules import instant
from ..storage.plans import _decode, _encode
from .auth import VerifiedSignIn


EMAIL_ISSUER = "https://campuspulse.app/email"
_SUBJECT = re.compile(re.escape(EMAIL_ISSUER) + r"#[0-9a-f]{32}\Z")
_EMAIL = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+@"
                    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
                    r"[A-Za-z](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
_WINDOW = timedelta(minutes=15)
_PROOF_LIFETIME = timedelta(seconds=60)
_SESSION_LIFETIME = timedelta(hours=1)


class InvalidCredentials(ValueError):
    def __init__(self):
        super().__init__("invalid_credentials")


class EmailRegistrationUnavailable(ValueError):
    def __init__(self):
        super().__init__("email_registration_unavailable")


class PasswordValidationError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class PasswordRateLimited(ValueError):
    def __init__(self, retry_after):
        self.retry_after = max(1, int(retry_after))
        super().__init__("password_rate_limited")


def normalize_email(email: str) -> str:
    if type(email) is not str:
        raise PasswordValidationError("email_invalid")
    trimmed = email.strip()
    if not trimmed.isascii():
        raise PasswordValidationError("email_invalid")
    value = trimmed.lower()
    if (len(value) > 254 or not _EMAIL.fullmatch(value)
            or len(value.split("@", 1)[0]) > 64 or value.startswith(".")
            or ".." in value or ".@" in value):
        raise PasswordValidationError("email_invalid")
    return value


def _password_bytes(password: str) -> bytes:
    if type(password) is not str:
        raise PasswordValidationError("password_length")
    if len(password) < 15:
        raise PasswordValidationError("password_length")
    if len(password) > 128:
        raise PasswordValidationError("password_length")
    try:
        encoded = password.encode("utf-8")
    except UnicodeEncodeError:
        raise PasswordValidationError("password_length") from None
    if len(encoded) > 512:
        raise PasswordValidationError("password_length")
    return encoded


def _nonce_digest(nonce: str) -> str:
    if (type(nonce) is not str or not 1 <= len(nonce) <= 256
            or not nonce.isascii() or any(character.isspace() for character in nonce)):
        raise InvalidCredentials()
    return sha256(nonce.encode("ascii")).hexdigest()


class _PasswordRecord(Contract):
    version: Literal[1] = 1
    principal_id: Identifier
    salt: str = Field(pattern=r"^[0-9a-f]{32}$")
    password_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: Instant


class _AttemptWindow(Contract):
    started_at: Instant
    count: int = Field(ge=0, le=120)


@dataclass(frozen=True, repr=False)
class PasswordIdentity:
    """Authority-bound server capability; neither a bearer token nor a DTO."""

    _identity: VerifiedSignIn
    _nonce_digest: str
    _proof_expires_at: datetime
    _authority: object
    _seal: bytes

    @property
    def principal_id(self):
        return self._identity.principal_id


class EmailCredentials:
    def __init__(self, documents, clock: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self.documents, self.clock = documents, clock
        self._authority = object()
        self._proof_key = secrets.token_bytes(32)
        self._dummy_salt, self._dummy_hash = secrets.token_bytes(16), secrets.token_bytes(32)
        # Admission is durable; this bound only protects a process's hash RAM.
        self._hash_slots = asyncio.Semaphore(2)
        self._hash_tasks = set()

    def now(self):
        return instant(self.clock())

    async def _derive(self, password, salt):
        await self._hash_slots.acquire()
        worker = asyncio.create_task(asyncio.to_thread(scrypt, password, salt=salt, n=2**15,
            r=8, p=3, maxmem=64 * 1024 * 1024, dklen=32))
        self._hash_tasks.add(worker)

        def completed(task):
            self._hash_tasks.discard(task)
            self._hash_slots.release()
            if not task.cancelled():
                task.exception()

        worker.add_done_callback(completed)
        # Native scrypt continues after request cancellation. Keep its memory
        # slot until that worker actually finishes, including abandoned calls.
        return await asyncio.shield(worker)

    async def _admit(self, email_key):
        now = self.now()
        paths = ("cp_email_attempts/email_" + email_key, "cp_email_attempts/service")

        async def admission(tx):
            # Firestore transactions require every read before every write.
            documents = [await tx.get(path) for path in paths]
            windows = []
            retry_after = 0
            for document, limit in zip(documents, (10, 120), strict=True):
                window = (None if document is None else
                    _decode(document, "email_attempt_window", _AttemptWindow))
                if window is None or now >= window.started_at + _WINDOW:
                    window = _AttemptWindow(started_at=now, count=0)
                if window.count >= limit or now < window.started_at:
                    retry_after = max(retry_after,
                        math.ceil((window.started_at + _WINDOW - now).total_seconds()))
                windows.append(window)
            if retry_after:
                return retry_after
            for path, window in zip(paths, windows, strict=True):
                tx.set(path, _encode("email_attempt_window",
                    window.model_copy(update={"count": window.count + 1})))
            return 0

        # Failure is raised after commit, so failed logins retain their count.
        retry_after = await self.documents.transact(admission)
        if retry_after:
            raise PasswordRateLimited(retry_after)

    def _seal(self, identity, nonce_digest, proof_expires_at):
        payload = "\0".join((identity.model_dump_json(), nonce_digest, proof_expires_at.isoformat()))
        return hmac.digest(self._proof_key, payload.encode("utf-8"), "sha256")

    def _proof(self, principal, nonce_digest):
        now = self.now()
        identity = VerifiedSignIn(principal_id=principal, authenticated_at=now,
            expires_at=now + _SESSION_LIFETIME)
        deadline = now + _PROOF_LIFETIME
        return PasswordIdentity(identity, nonce_digest, deadline, self._authority,
            self._seal(identity, nonce_digest, deadline))

    def check(self, proof, *, nonce, now):
        try:
            checked_now = instant(now)
            if (type(proof) is not PasswordIdentity or proof._authority is not self._authority
                    or not _SUBJECT.fullmatch(proof.principal_id)
                    or not hmac.compare_digest(proof._nonce_digest, _nonce_digest(nonce))
                    or not hmac.compare_digest(proof._seal,
                        self._seal(proof._identity, proof._nonce_digest, proof._proof_expires_at))
                    or not proof._identity.authenticated_at <= checked_now < proof._proof_expires_at
                    or proof._proof_expires_at != proof._identity.authenticated_at + _PROOF_LIFETIME
                    or proof._identity.expires_at != proof._identity.authenticated_at + _SESSION_LIFETIME):
                raise InvalidCredentials()
            return VerifiedSignIn.model_validate(proof._identity)
        except (AttributeError, TypeError, ValueError):
            raise InvalidCredentials() from None

    async def register(self, email, password, *, nonce):
        email_key = sha256(normalize_email(email).encode("ascii")).hexdigest()
        encoded, nonce_digest = _password_bytes(password), _nonce_digest(nonce)
        await self._admit(email_key)
        salt = secrets.token_bytes(16)
        password_hash = await self._derive(encoded, salt)
        record = _PasswordRecord(principal_id=EMAIL_ISSUER + "#" + uuid4().hex,
            salt=salt.hex(), password_hash=password_hash.hex(), created_at=self.now())
        path = "cp_email_credentials/" + email_key

        async def create(tx):
            if await tx.get(path) is not None:
                return False
            tx.create(path, _encode("email_password", record))
            return True

        if not await self.documents.transact(create):
            raise EmailRegistrationUnavailable()
        return self._proof(record.principal_id, nonce_digest)

    async def authenticate(self, email, password, *, nonce):
        email_key = sha256(normalize_email(email).encode("ascii")).hexdigest()
        encoded, nonce_digest = _password_bytes(password), _nonce_digest(nonce)
        await self._admit(email_key)
        document = await self.documents.get("cp_email_credentials/" + email_key)
        record = None
        if document is not None:
            record = _decode(document, "email_password", _PasswordRecord)
            if not _SUBJECT.fullmatch(record.principal_id):
                raise InvalidCredentials()
        actual = await self._derive(encoded, self._dummy_salt if record is None else bytes.fromhex(record.salt))
        expected = self._dummy_hash if record is None else bytes.fromhex(record.password_hash)
        matches = hmac.compare_digest(actual, expected)
        if record is None or not matches:
            raise InvalidCredentials()
        return self._proof(record.principal_id, nonce_digest)
