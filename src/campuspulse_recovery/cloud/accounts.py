"""Explicit server-owned bindings for the multiuser recovery composition."""
from hashlib import sha256

from ..security.resources import ResourcePolicy
from .settings import CloudSettings


def account_binding(settings):
    bindings = getattr(settings, 'calendar_feed_accounts', ())
    if not bindings:
        return None
    principal = settings.application.student.allowed_signin_principal
    return next((a for a in bindings if a.principal_id == principal), None)


def account_settings(settings, principal):
    binding = next((a for a in settings.calendar_feed_accounts if a.principal_id == principal), None)
    if binding is None:
        return None
    values = settings.model_dump()
    owner = 'google-' + sha256(principal.encode()).hexdigest()
    # This source placeholder is never sent to Sheets. It also binds sessions
    # and grants to the exact server-configured Moodle account in AppConfig.
    source = owner + '_moodle_' + str(binding.moodle_user_id)
    values['application']['student'].update(trusted_user_id=owner, allowed_signin_principal=principal,
        calendar_id=binding.calendar_id, spreadsheet_id=source)
    return CloudSettings.model_validate(values)


def account_policy(settings):
    from .planning import assignments_resource
    return ResourcePolicy(settings.application, environment='cloud',
                          assignments_resource_id=assignments_resource(settings))


def create_account_service(settings, documents, *, boundary, factory, close):
    """Route only an authenticated service's saved grant to an allowlisted app.

    Routing is not authority: the chosen worker/MCP still checks the complete
    grant, live session, config revision and every resource before dispatch.
    """
    from contextlib import AsyncExitStack, asynccontextmanager
    import inspect
    from fastapi import FastAPI
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from ..security.context import AccessDenied
    from ..cloud.authority import Grant, ServiceIdentity, grant_path
    from ..storage.plans import _decode

    if boundary not in ('agent', 'mcp') or not settings.calendar_feed_accounts:
        raise ValueError('explicit_account_service_required')
    audience = settings.agent_origin if boundary == 'agent' else settings.mcp_origin
    caller = (settings.application.identities.backend_service_account if boundary == 'agent'
              else settings.application.identities.agent_service_account)
    identity = ServiceIdentity(audience, caller)
    children = {}
    for binding in settings.calendar_feed_accounts:
        selected = account_settings(settings, binding.principal_id)
        children[binding.principal_id] = factory(selected, account_policy(selected), documents, close=lambda: None)

    @asynccontextmanager
    async def lifespan(app):
        try:
            async with AsyncExitStack() as stack:
                for child in children.values():
                    await stack.enter_async_context(child.router.lifespan_context(child))
                yield
        finally:
            result = close()
            if inspect.isawaitable(result):
                await result

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get('/healthz')
    async def health():
        return {'status': 'alive'}

    async def dispatch(scope, receive, send):
        try:
            request = Request(scope)
            await identity.verify(request)
            refs = request.headers.getlist('x-campuspulse-invocation')
            if len(refs) != 1:
                raise AccessDenied('invocation_reference_required')
            saved = _decode(await documents.get(grant_path(refs[0])), 'cloud_grant', Grant)
            child = children.get(saved.binding.signin_principal_id)
            if child is None:
                raise AccessDenied('recovery_account_not_configured')
        except Exception:
            await JSONResponse({'error': 'service_or_invocation_rejected'}, status_code=403)(scope, receive, send)
            return
        await child(scope, receive, send)

    app.mount('/', dispatch)
    return app
