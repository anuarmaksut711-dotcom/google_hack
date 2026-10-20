"""Authenticated student input for planning; no external connection selectors."""
from fastapi import Request

from ..lms.planning import PlanningUpdate
from .app import body


def install_planning_routes(app, *, auth, inputs):
    async def serve(request, *, update=False):
        credential = await auth.load(request, mutation=update)
        if request.query_params:
            raise ValueError('planning_selectors_forbidden')
        value = await body(request, inputs.update_model) if update else None
        with auth.request_scope(credential) as context:
            state = await inputs.update(context, value) if update else await inputs.get(context)
            latest = await auth.load(request)
            if latest.session != credential.session:
                from ..security.context import AccessDenied
                raise AccessDenied('session_changed')
            auth.policy.require_context(context)
            return state.model_dump(mode='json')

    @app.get('/api/moodle/planning')
    async def get_planning(request: Request):
        return await serve(request)

    @app.post('/api/moodle/planning')
    async def update_planning(request: Request):
        return await serve(request, update=True)
