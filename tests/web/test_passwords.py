"""Credential tests exercise real scrypt and transactional durable boundaries."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import wraps
from hashlib import scrypt, sha256
import threading

import pytest

from campuspulse_recovery.storage.sqlite_documents import SqliteDocuments
from campuspulse_recovery.web.auth import VerifiedSignIn
from campuspulse_recovery.web.passwords import (
    EMAIL_ISSUER, EmailCredentials, EmailRegistrationUnavailable, InvalidCredentials,
    PasswordIdentity, PasswordRateLimited, PasswordValidationError, normalize_email,
)
from tests.storage.test_plans import FakeDocuments


NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)
PASSWORD = "correct horse battery staple"
NONCE = "random-server-challenge"


def run_async(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return run


async def cheap_derive(password, salt):
    """Only flood/admission tests replace expensive KDF, never normal auth tests."""
    await asyncio.sleep(0)
    return sha256(salt + password).digest()


@run_async
async def test_real_credentials_normalize_email_keep_password_and_survive_restart(tmp_path):
    def store():
        return SqliteDocuments(tmp_path, project_id="test-project", database_id="(default)")

    credentials = EmailCredentials(store(), clock=lambda: NOW)
    password = "  passphrase with significant spaces  "
    registered = await credentials.register("  Student+School@Example.COM ", password, nonce=NONCE)
    assert registered.principal_id.startswith(EMAIL_ISSUER + "#")
    first = credentials.check(registered, nonce=NONCE, now=NOW)
    assert first.expires_at == NOW + timedelta(hours=1)

    restarted = EmailCredentials(store(), clock=lambda: NOW + timedelta(seconds=2))
    signed_in = await restarted.authenticate("student+school@example.com", password, nonce="new-nonce")
    assert signed_in.principal_id == registered.principal_id
    with pytest.raises(InvalidCredentials):
        await restarted.authenticate("student+school@example.com", password.strip(), nonce="new-nonce")
    with pytest.raises(InvalidCredentials):
        restarted.check(registered, nonce=NONCE, now=NOW + timedelta(seconds=2))
    raw = (tmp_path / "documents.sqlite3").read_bytes()
    assert b"student+school@example.com" not in raw
    assert password.encode() not in raw


@run_async
async def test_password_storage_uses_random_salt_real_scrypt_and_no_email_or_password():
    documents = FakeDocuments()
    credentials = EmailCredentials(documents, clock=lambda: NOW)
    await credentials.register("alice@example.com", PASSWORD, nonce=NONCE)
    await credentials.register("bob@example.com", PASSWORD, nonce=NONCE)
    records = [document for path, document in documents.data.items() if path.startswith("cp_email_credentials/")]
    assert len(records) == 2
    assert records[0]["body"] != records[1]["body"]
    import json
    values = [json.loads(record["body"]) for record in records]
    assert values[0]["salt"] != values[1]["salt"]
    assert values[0]["password_hash"] != values[1]["password_hash"]
    for value in values:
        expected = scrypt(PASSWORD.encode(), salt=bytes.fromhex(value["salt"]),
            n=2**15, r=8, p=3, maxmem=64 * 1024 * 1024, dklen=32)
        assert value["password_hash"] == expected.hex()
    durable = repr(documents.data)
    assert "alice@example.com" not in durable and "bob@example.com" not in durable
    assert PASSWORD not in durable


@run_async
async def test_duplicate_registration_cannot_replace_credentials():
    documents = FakeDocuments()
    credentials = EmailCredentials(documents, clock=lambda: NOW)
    original = await credentials.register("alice@example.com", PASSWORD, nonce=NONCE)
    with pytest.raises(EmailRegistrationUnavailable):
        await credentials.register("ALICE@example.com", "a different long passphrase", nonce=NONCE)
    signed_in = await credentials.authenticate("alice@example.com", PASSWORD, nonce=NONCE)
    assert signed_in.principal_id == original.principal_id
    with pytest.raises(InvalidCredentials):
        await credentials.authenticate("alice@example.com", "a different long passphrase", nonce=NONCE)


@run_async
async def test_parallel_registration_across_store_instances_creates_exactly_one_account():
    documents = FakeDocuments()
    first, second = (EmailCredentials(documents, clock=lambda: NOW) for _ in range(2))
    outcomes = await asyncio.gather(
        first.register("alice@example.com", PASSWORD, nonce=NONCE),
        second.register("Alice@example.com", "another secure long password", nonce=NONCE),
        return_exceptions=True)
    assert sum(type(value) is PasswordIdentity for value in outcomes) == 1
    assert sum(type(value) is EmailRegistrationUnavailable for value in outcomes) == 1
    assert len([path for path in documents.data if path.startswith("cp_email_credentials/")]) == 1


@run_async
async def test_wrong_password_and_unknown_email_use_same_kdf_and_generic_error(monkeypatch):
    credentials = EmailCredentials(FakeDocuments(), clock=lambda: NOW)
    await credentials.register("alice@example.com", PASSWORD, nonce=NONCE)
    calls = []
    original_derive = credentials._derive

    async def counted(password, salt):
        calls.append((len(password), len(salt)))
        return await original_derive(password, salt)

    monkeypatch.setattr(credentials, "_derive", counted)
    errors = []
    for email in ("alice@example.com", "missing@example.com"):
        with pytest.raises(InvalidCredentials) as error:
            await credentials.authenticate(email, "wrong password with enough characters", nonce=NONCE)
        errors.append(str(error.value))
    assert errors == ["invalid_credentials", "invalid_credentials"]
    assert len(calls) == 2 and calls[0] == calls[1]


@run_async
async def test_authority_nonce_expiry_and_forged_identity_are_rejected():
    credentials = EmailCredentials(FakeDocuments(), clock=lambda: NOW)
    proof = await credentials.register("alice@example.com", PASSWORD, nonce=NONCE)
    assert credentials.check(proof, nonce=NONCE, now=NOW).principal_id == proof.principal_id
    forged_identity = VerifiedSignIn(principal_id=EMAIL_ISSUER + "#" + "f" * 32,
        authenticated_at=NOW, expires_at=NOW + timedelta(hours=1))
    for forged in (
        {"principal_id": proof.principal_id},
        replace(proof, _identity=forged_identity),
        replace(proof, _proof_expires_at=NOW + timedelta(days=1)),
        replace(proof, _authority=object()),
        replace(proof, _seal=b"forged"),
    ):
        with pytest.raises(InvalidCredentials):
            credentials.check(forged, nonce=NONCE, now=NOW)
    for nonce, now in (("different-nonce", NOW), (NONCE, NOW - timedelta(seconds=1)),
                       (NONCE, NOW + timedelta(seconds=60)), (NONCE, NOW.replace(tzinfo=None))):
        with pytest.raises(InvalidCredentials):
            credentials.check(proof, nonce=nonce, now=now)
    other_authority = EmailCredentials(credentials.documents, clock=lambda: NOW)
    with pytest.raises(InvalidCredentials):
        other_authority.check(proof, nonce=NONCE, now=NOW)


@run_async
async def test_parallel_unknown_account_attempts_are_durably_limited(monkeypatch):
    documents = FakeDocuments()
    first, second = (EmailCredentials(documents, clock=lambda: NOW) for _ in range(2))
    monkeypatch.setattr(first, "_derive", cheap_derive)
    monkeypatch.setattr(second, "_derive", cheap_derive)
    results = await asyncio.gather(*[
        (first if index % 2 else second).authenticate("unknown@example.com", PASSWORD, nonce=NONCE)
        for index in range(14)], return_exceptions=True)
    assert sum(type(value) is InvalidCredentials for value in results) == 10
    assert sum(type(value) is PasswordRateLimited for value in results) == 4
    assert all(value.retry_after == 900 for value in results if isinstance(value, PasswordRateLimited))


@run_async
async def test_rate_limit_survives_sqlite_restart_and_expires(monkeypatch, tmp_path):
    now = [NOW]

    def credentials():
        store = SqliteDocuments(tmp_path, project_id="test-project", database_id="(default)")
        value = EmailCredentials(store, clock=lambda: now[0])
        monkeypatch.setattr(value, "_derive", cheap_derive)
        return value

    original = credentials()
    for _ in range(10):
        with pytest.raises(InvalidCredentials):
            await original.authenticate("unknown@example.com", PASSWORD, nonce=NONCE)
    restarted = credentials()
    now[0] += timedelta(seconds=30)
    with pytest.raises(PasswordRateLimited) as error:
        await restarted.authenticate("UNKNOWN@example.com", PASSWORD, nonce=NONCE)
    assert error.value.retry_after == 870
    now[0] = NOW + timedelta(minutes=15)
    with pytest.raises(InvalidCredentials):
        await restarted.authenticate("unknown@example.com", PASSWORD, nonce=NONCE)


@run_async
async def test_service_admission_limits_fresh_email_flood_and_registration(monkeypatch):
    credentials = EmailCredentials(FakeDocuments(), clock=lambda: NOW)
    monkeypatch.setattr(credentials, "_derive", cheap_derive)
    for index in range(120):
        with pytest.raises(InvalidCredentials):
            await credentials.authenticate(f"unknown{index}@example.com", PASSWORD, nonce=NONCE)
    with pytest.raises(PasswordRateLimited) as error:
        await credentials.register("fresh@example.com", PASSWORD, nonce=NONCE)
    assert error.value.retry_after == 900
    assert await credentials.documents.get("cp_email_credentials/" + sha256(b"fresh@example.com").hexdigest()) is None


@pytest.mark.parametrize("email", ["", "bad", "a@@example.com", ".a@example.com", "a.@example.com",
    "a..b@example.com", "a@example..com", "a@-example.com", "a@example-.com", "a@localhost",
    "a\n@example.com", "юзер@example.com", "K@example.com", "a@пример.рф", "a" * 65 + "@example.com", None])
def test_email_invalid_is_rejected(email):
    with pytest.raises(PasswordValidationError) as error:
        normalize_email(email)
    assert error.value.code == "email_invalid"


@run_async
@pytest.mark.parametrize(("password", "code"), [
    ("short password", "password_length"), ("a" * 129, "password_length"),
    ("\ud800" * 15, "password_length"), (None, "password_length"),
])
async def test_password_lengths_are_rejected_before_storage(password, code):
    documents = FakeDocuments()
    credentials = EmailCredentials(documents, clock=lambda: NOW)
    with pytest.raises(PasswordValidationError) as error:
        await credentials.register("alice@example.com", password, nonce=NONCE)
    assert error.value.code == code
    assert documents.data == {}


@run_async
async def test_unicode_passphrase_and_exact_minimum_are_supported():
    credentials = EmailCredentials(FakeDocuments(), clock=lambda: NOW)
    password = "мой длинный пароль из слов"
    original = await credentials.register("alice@example.com", password, nonce=NONCE)
    assert (await credentials.authenticate("alice@example.com", password, nonce=NONCE)).principal_id == original.principal_id
    await credentials.register("minimum@example.com", "a" * 15, nonce=NONCE)


@run_async
async def test_invalid_nonce_never_creates_account():
    documents = FakeDocuments()
    credentials = EmailCredentials(documents, clock=lambda: NOW)
    for nonce in (None, "", "has spaces", "юникод", "a" * 257):
        with pytest.raises(InvalidCredentials):
            await credentials.register("alice@example.com", PASSWORD, nonce=nonce)
    assert documents.data == {}


@run_async
async def test_cancelled_requests_keep_hash_memory_slots_until_native_work_finishes(monkeypatch):
    from campuspulse_recovery.web import passwords

    credentials = EmailCredentials(FakeDocuments(), clock=lambda: NOW)
    release, first_two = threading.Event(), threading.Event()
    lock = threading.Lock()
    running = [0]
    started = [0]
    maximum = [0]

    def blocked_kdf(password, **options):
        with lock:
            running[0] += 1
            started[0] += 1
            maximum[0] = max(maximum[0], running[0])
            if started[0] == 2:
                first_two.set()
        release.wait(timeout=5)
        with lock:
            running[0] -= 1
        return b"x" * 32

    monkeypatch.setattr(passwords, "scrypt", blocked_kdf)
    first, second = [asyncio.create_task(credentials._derive(b"password", b"s" * 16)) for _ in range(2)]
    try:
        assert await asyncio.to_thread(first_two.wait, 2)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        third = asyncio.create_task(credentials._derive(b"password", b"s" * 16))
        await asyncio.sleep(0.05)
        assert started[0] == 2
        release.set()
        assert await second == b"x" * 32
        assert await third == b"x" * 32
        assert maximum[0] <= 2
    finally:
        release.set()
        await asyncio.gather(first, second, return_exceptions=True)
