"""Audience-bound, lazy platform ID tokens. Importing this module performs no IO.

The issuer is a trusted application edge, not a model tool. Cached bearer tokens,
injected issuers/clocks and runtime locks are deliberately never serialized.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import math
import re
import time
from urllib.parse import urlsplit


def cloud_run_audience(value: str) -> str:
    """Validate base URLs, including regional and multi-label Cloud Run forms.

    Shape validation is NOT proof of ownership, IAM, reachability or DNS.
    Custom domains need a separately reviewed audience policy; no silent fallback.
    """
    if (not isinstance(value, str) or not value or value != value.strip()
            or "?" in value or "#" in value
            or re.search(r"[\s\x00-\x1f\x7f]", value)):
        raise ValueError("invalid_cloud_run_audience")
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    labels = host.split(".")
    if (parsed.scheme != "https" or parsed.netloc != host or parsed.path
            or parsed.query or parsed.fragment or len(host) > 253
            or len(labels) < 3 or labels[-2:] != ["run", "app"]
            or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", p)
                   for p in labels[:-2])):
        raise ValueError("invalid_cloud_run_audience")
    return value


def bounded_seconds(value: float, *, maximum: float = 60) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("invalid_timeout")
    if not math.isfinite(value) or not 0 < value <= maximum:
        raise ValueError("invalid_timeout")
    return float(value)


@dataclass(frozen=True, repr=False)
class IssuedIdToken:
    value: str = field(repr=False)
    audience: str
    expires_at: float


class TokenAcquisitionError(RuntimeError):
    """Stable, redacted error; never includes an issuer response or bearer token."""


class PlatformIdTokenIssuer:
    """Lazy Google platform issuer; LOCAL_ONLY tests replace all credential/IO edges.

    Each refresh acquires credentials afresh from the runtime's identity. A
    bounded requests transport also limits IO if the async caller is cancelled.
    asyncio cancellation cannot kill a running Python worker thread; it contains
    no Calendar mutations and its result is discarded after caller cancellation.
    """

    async def __call__(self, audience: str, timeout_seconds: float) -> IssuedIdToken:
        return await asyncio.to_thread(self._issue, audience, timeout_seconds)

    @staticmethod
    def _issue(audience: str, timeout_seconds: float) -> IssuedIdToken:
        from google.auth.transport.requests import Request
        from google.auth.compute_engine.credentials import IDTokenCredentials

        class BoundedRequest(Request):
            def __call__(self, *args, **kwargs):
                kwargs["timeout"] = timeout_seconds
                return super().__call__(*args, **kwargs)


        import requests
        with requests.Session() as session:
            request = BoundedRequest(session=session)


            credentials = IDTokenCredentials(request=request, target_audience=audience,
                                              use_metadata_identity_endpoint=True)
            credentials.refresh(request)
            if not credentials.token or credentials.expiry is None:
                raise TokenAcquisitionError("id_token_unavailable")
            from datetime import UTC
            expiry = credentials.expiry
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=UTC)


            from google.auth import jwt
            claims = jwt.decode(credentials.token, verify=False)
            if claims.get("aud") != audience:
                raise TokenAcquisitionError("id_token_audience_mismatch")
            return IssuedIdToken(credentials.token, claims["aud"], expiry.timestamp())


class RefreshingIdTokenHeaders:
    """Async ADK header_provider, single-flight refresh per runtime/event loop.

    Nonsecret audience/config is never session/run authorization. The MCP service
    must resolve caller and run bindings independently. No context state is sent
    as authority by this provider.
    """

    def __init__(self, audience: str, *, issuer=None, clock=None,
                 refresh_skew_seconds: float = 60, timeout_seconds: float = 5):
        self.audience = cloud_run_audience(audience)
        self.timeout_seconds = bounded_seconds(timeout_seconds)
        self.refresh_skew_seconds = bounded_seconds(refresh_skew_seconds, maximum=300)
        self._issuer = issuer
        self._clock = clock or time.time
        self._cached = None
        self._lock = None

    def __repr__(self):
        return f"RefreshingIdTokenHeaders(audience={self.audience!r}, cached=<redacted>)"

    def bind_local_runtime(self, *, issuer, clock):
        """Explicit Python test injection after restore, never a JSON/model flag."""
        if self._lock is not None and self._lock.locked():
            raise RuntimeError("refresh_in_progress")
        self._issuer, self._clock, self._cached, self._lock = issuer, clock, None, None

    def _fresh(self):
        return self._cached is not None and (
            self._cached.expires_at > self._clock() + self.refresh_skew_seconds)

    async def __call__(self, readonly_context):
        if readonly_context is None:
            raise TokenAcquisitionError("adk_context_required")

        try:
            async with asyncio.timeout(self.timeout_seconds):
                if self._lock is None:
                    self._lock = asyncio.Lock()
                async with self._lock:
                    if not self._fresh():
                        self._cached = None
                        issuer = self._issuer or PlatformIdTokenIssuer()
                        token = await issuer(self.audience, self.timeout_seconds)
                        if (not isinstance(token, IssuedIdToken)
                                or token.audience != self.audience
                                or not isinstance(token.value, str) or not token.value
                                or re.search(r"[\s\x00-\x1f\x7f]", token.value)
                                or isinstance(token.expires_at, bool)
                                or not isinstance(token.expires_at, (float, int))
                                or not math.isfinite(token.expires_at)
                                or token.expires_at <= self._clock() + self.refresh_skew_seconds):
                            raise TokenAcquisitionError("invalid_issued_id_token")
                        self._cached = token
                    return {"Authorization": "Bearer " + self._cached.value}
        except asyncio.CancelledError:
            raise
        except Exception:

            raise TokenAcquisitionError("id_token_acquisition_failed") from None

    def __getstate__(self):
        return {"audience": self.audience, "timeout_seconds": self.timeout_seconds,
                "refresh_skew_seconds": self.refresh_skew_seconds}

    def __setstate__(self, state):
        self.__init__(**state)
