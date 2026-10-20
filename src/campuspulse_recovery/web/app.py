"""Single UI/backend ASGI service factory, wired explicitly at trusted startup.

T16 provides sign-in, stored-plan read, consent/status/revoke. T24/T25 own the
rendered recovery UI, runtime/trace connection and execution integration. There
is deliberately no apply route, default identity or auto-created production DB.
"""

from datetime import UTC, datetime
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from ..domain.models import Contract, Identifier, PlanReference, PositiveInt
from ..domain.ports import BindingConflict, PortError
from ..security.context import AccessDenied
from ..security.resources import ResourcePolicy
from ..storage.plans import FirestorePlanStore, plan_reference
from .approval import ApprovalAction, FirestoreApprovalStore
from .auth import CHALLENGE_COOKIE, CHALLENGE_CSRF_COOKIE, CSRF_COOKIE, SESSION_COOKIE, WebAuth


class SignInBody(Contract):
    credential: str


class PlanQuery(Contract):
    plan_id: Identifier
    version: PositiveInt


async def body(request, model):
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
        raise ValueError("json_required")
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > 16_384:
            raise ValueError("request_too_large")
    return model.model_validate_json(data)


def _json(value, status=200):
    return JSONResponse(value, status_code=status)


def _set(response, name, value, age, *, httponly=True):
    response.set_cookie(name, value, max_age=age, secure=True, httponly=httponly, samesite="strict", path="/")


def create_app(*, policy: ResourcePolicy, documents, verifier, origin: str, environment: str,
               clock: Callable[[], datetime] = lambda: datetime.now(UTC), approval_store=None, email_credentials=None) -> FastAPI:
    """Mandatory verified sign-in + config/database binding; missing inputs fail closed.

    In production pass GoogleSignInVerifier(explicit client ID) and
    FirestoreDocuments(explicit AsyncClient project/database). No browser data
    may supply these objects. Google sign-in grants no Sheets/Calendar access.
    """
    auth = WebAuth(documents, policy, verifier=verifier, origin=origin, environment=environment,
                   clock=clock, email_credentials=email_credentials)
    approvals = approval_store or FirestoreApprovalStore(documents, policy, clock=clock)
    if approvals.policy is not policy or approvals.documents is not documents or approvals.clock is not clock:
        raise ValueError('matching_approval_authority_required')
    plans = FirestorePlanStore(documents, policy)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def protections(request, call_next):
        response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer", "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
            "X-Frame-Options": "DENY", "Strict-Transport-Security": "max-age=31536000"})
        return response

    @app.exception_handler(AccessDenied)
    async def denied(request, error):
        return _json({"error": "access_denied"}, 403)

    @app.exception_handler(BindingConflict)
    async def conflict(request, error):
        return _json({"error": "plan_or_request_conflict"}, 409)

    @app.exception_handler(PortError)
    async def unavailable(request, error):
        return _json({"error": "storage_outcome_unknown_retry_same_request"}, 503)

    @app.exception_handler(ValueError)
    @app.exception_handler(ValidationError)
    async def invalid(request, error):

        return _json({"error": "invalid_request"}, 400)

    @app.get("/auth/challenge")
    async def challenge(request: Request):
        auth.check_request(request)
        handle, csrf, nonce = await auth.challenge()
        response = _json({"nonce": nonce, "csrf_token": csrf})
        _set(response, CHALLENGE_COOKIE, handle, 300)
        _set(response, CHALLENGE_CSRF_COOKIE, csrf, 300)
        return response

    @app.post("/auth/signin")
    async def signin(request: Request):
        value = await body(request, SignInBody)
        if not value.credential or len(value.credential) > 12_000:
            raise ValueError("invalid_credential")
        cookie, csrf, session = await auth.signin(request, value.credential)
        response = _json({"status": "authenticated", "csrf_token": csrf})
        age = max(1, int((session.expires_at - auth.now()).total_seconds()))
        _set(response, SESSION_COOKIE, cookie, age)
        _set(response, CSRF_COOKIE, csrf, age, httponly=False)
        response.delete_cookie(CHALLENGE_COOKIE, secure=True, httponly=True, samesite="strict", path="/")
        response.delete_cookie(CHALLENGE_CSRF_COOKIE, secure=True, httponly=True, samesite="strict", path="/")
        return response

    @app.get("/auth/session")
    async def session(request: Request):
        csrf, repaired = None, False
        if request.headers.get("x-csrf-bootstrap") is not None:
            credential, csrf, repaired = await auth.bootstrap_session(request)
        else:
            credential = await auth.load(request)
        with auth.request_scope(credential) as context:
            policy.require_context(context)
            value = {"status": "authenticated", "session_id": context.session_id,
                "run_id": context.trusted.run_id, "expires_at": credential.session.expires_at.isoformat()}
            if csrf is not None:
                value["csrf_token"] = csrf
            response = _json(value)
            if repaired:
                age = int((credential.session.expires_at - auth.now()).total_seconds())
                if age < 1:
                    raise AccessDenied("session_rejected")
                _set(response, CSRF_COOKIE, csrf, age, httponly=False)
            return response

    @app.post("/auth/signout")
    async def signout(request: Request):
        await auth.signout(request)
        response = _json({"status": "signed_out"})
        for name in (SESSION_COOKIE, CSRF_COOKIE, CHALLENGE_COOKIE, CHALLENGE_CSRF_COOKIE):
            response.delete_cookie(name, secure=True, httponly=name != CSRF_COOKIE, samesite="strict", path="/")
        return response

    @app.get("/api/plan")
    async def read_plan(request: Request):
        credential = await auth.load(request)
        if set(request.query_params) != {"plan_id", "version"} or any(
                len(request.query_params.getlist(name)) != 1 for name in request.query_params):
            raise ValueError("invalid_plan_query")
        raw_version = request.query_params["version"]
        if not raw_version.isascii() or not raw_version.isdigit() or len(raw_version) > 19:
            raise ValueError("invalid_version")
        query = PlanQuery(plan_id=request.query_params["plan_id"], version=int(raw_version))
        with auth.request_scope(credential) as context:
            plan = await plans.get(context, query.plan_id, query.version)
            approval = await approvals.get(context, plan_reference(plan))
            current = await plans.get_current(context)
            policy.require_context(context)
            return _json({"plan": plan.model_dump(mode="json"),
                "current": None if current is None else current.model_dump(mode="json"),
                "approval": None if approval is None else approval.model_dump(mode="json")})

    @app.post("/api/approval")
    async def approve(request: Request):
        credential = await auth.load(request, mutation=True)
        value = await body(request, ApprovalAction)
        reference = PlanReference(plan_id=value.plan_id, version=value.version, payload_hash=value.payload_hash)
        with auth.request_scope(credential) as context:
            approval = await approvals.approve(context, reference, value.request_key)
            return _json({"approval": approval.model_dump(mode="json")})

    @app.post("/api/approval/revoke")
    async def revoke(request: Request):
        credential = await auth.load(request, mutation=True)
        value = await body(request, ApprovalAction)
        reference = PlanReference(plan_id=value.plan_id, version=value.version, payload_hash=value.payload_hash)
        with auth.request_scope(credential) as context:
            with approvals.ui_action(context, reference, value.request_key) as action:
                await approvals.invalidate(context, reference, reason="revoked", at=action.at)
            result = await approvals.get(context, reference)
            return _json({"approval": result.model_dump(mode="json")})

    return app
