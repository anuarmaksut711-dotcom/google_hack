"""Session-bound capabilities for the read-only study agent.

Only trusted backend code issues invocation grants. Model-visible tools never
accept an owner, session, Moodle URL, or capability reference from the model.
"""
from datetime import UTC, datetime, timedelta
import json
import re
import secrets
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator

from ..agent.mcp_auth import cloud_run_audience
from ..config import ServiceAccount
from ..domain.models import Contract, Identifier, IanaZone, Instant
from ..security.context import AccessDenied
from ..web.agent_client import AgentEngineTarget
from ..web.auth import digest
from .connected_calendar import CalendarReadGrants

STUDY_TOOLS = ('read_assignments', 'read_schedule')


class StudyAgentSettings(Contract):
    target: AgentEngineTarget
    agent_audience: str
    mcp_origin: str
    backend_service_account: ServiceAccount
    agent_service_account: ServiceAccount

    @model_validator(mode='after')
    def explicit_origins(self):
        cloud_run_audience(self.agent_audience)
        cloud_run_audience(self.mcp_origin)
        return self


class _HistoryTurn(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    message: Annotated[str, Field(strict=True, min_length=1, max_length=4000)]
    response: Annotated[str, Field(strict=True, min_length=1, max_length=12000)]


class _Invocation(Contract):
    invocation_id: Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]
    owner: Identifier
    session: Identifier
    message: Annotated[str, Field(strict=True, min_length=1, max_length=4000)]
    history: Annotated[tuple[_HistoryTurn, ...], Field(max_length=40)]
    timezone: IanaZone
    expires: Instant
    consumed: StrictBool = False
    tool_count: Annotated[StrictInt, Field(ge=0, le=8)] = 0


class _ToolGrant(Contract):
    invocation_id: Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]
    owner: Identifier
    session: Identifier
    timezone: IanaZone
    expires: Instant
    tool: Literal['read_assignments', 'read_schedule']
    consumed: StrictBool = False


def _reference(value):
    if not isinstance(value, str) or re.fullmatch(r'[A-Za-z0-9_-]{43}', value) is None:
        raise AccessDenied('study_grant_rejected')
    return digest(value)


def _public(value):
    return value.model_dump(mode='json', exclude={'schema_version', 'consumed', 'tool_count'})


class StudyGrants:
    def __init__(self, documents, clock=lambda: datetime.now(UTC)):
        self.documents, self.clock = documents, clock
        self.sessions = CalendarReadGrants(documents, clock=clock)

    async def session(self, reader, owner, session):
        return await self.sessions.session(reader, owner, session)

    async def issue_invocation(self, owner, session, message, history, timezone):
        reference = secrets.token_urlsafe(32)
        turns = []
        for turn in history:
            value = turn.model_dump(mode='json') if hasattr(turn, 'model_dump') else dict(turn)
            if value.get('status', 'completed') != 'completed':
                continue
            turns.append(_HistoryTurn(message=value['message'], response=value['response']))
        if len(history) > 40:
            raise ValueError('study_history_too_large')
        record = _Invocation(invocation_id=_reference(reference), owner=owner, session=session,
            message=message, history=tuple(turns), timezone=timezone,
            expires=self.clock() + timedelta(seconds=150))
        if len(json.dumps(_public(record), ensure_ascii=False).encode()) > 180_000:
            raise ValueError('study_invocation_too_large')
        async def save(tx):
            current = await self.session(tx, owner, session)
            path = 'cp_study_invocations/' + record.invocation_id
            await tx.get(path)
            saved = record.model_copy(update={'expires': min(record.expires, current.expires_at)})
            tx.create(path, saved.model_dump(mode='json'))
        await self.documents.transact(save)
        return reference

    async def consume_invocation(self, reference):
        path = 'cp_study_invocations/' + _reference(reference)
        async def claim(tx):
            raw = await tx.get(path)
            if raw is None:
                raise AccessDenied('study_grant_rejected')
            record = _Invocation.model_validate(raw)
            if record.invocation_id != path.rsplit('/', 1)[1] or record.consumed or self.clock() >= record.expires:
                raise AccessDenied('study_grant_rejected')
            await self.session(tx, record.owner, record.session)
            tx.set(path, {**record.model_dump(mode='json'), 'consumed': True})
            return _public(record)
        return await self.documents.transact(claim)

    async def issue_tool(self, invocation, tool):
        if tool not in STUDY_TOOLS:
            raise AccessDenied('study_tool_rejected')
        value = _Invocation.model_validate(invocation)
        reference = secrets.token_urlsafe(32)
        async def save(tx):
            parent_path = 'cp_study_invocations/' + value.invocation_id
            raw = await tx.get(parent_path)
            if raw is None:
                raise AccessDenied('study_grant_rejected')
            parent = _Invocation.model_validate(raw)
            if (not parent.consumed or _public(parent) != _public(value)
                    or self.clock() >= parent.expires or parent.tool_count >= 8):
                raise AccessDenied('study_grant_rejected')
            current = await self.session(tx, parent.owner, parent.session)
            child_path = 'cp_study_tool_grants/' + _reference(reference)
            await tx.get(child_path)
            child = _ToolGrant(invocation_id=parent.invocation_id, owner=parent.owner,
                session=parent.session, timezone=parent.timezone, tool=tool,
                expires=min(parent.expires, current.expires_at, self.clock() + timedelta(seconds=60)))
            tx.set(parent_path, {**parent.model_dump(mode='json'), 'tool_count': parent.tool_count + 1})
            tx.create(child_path, child.model_dump(mode='json'))
        await self.documents.transact(save)
        return reference

    async def consume_tool(self, reference, tool):
        if tool not in STUDY_TOOLS:
            raise AccessDenied('study_tool_rejected')
        path = 'cp_study_tool_grants/' + _reference(reference)
        async def claim(tx):
            raw = await tx.get(path)
            if raw is None:
                raise AccessDenied('study_grant_rejected')
            child = _ToolGrant.model_validate(raw)
            if child.tool != tool or child.consumed or self.clock() >= child.expires:
                raise AccessDenied('study_grant_rejected')
            parent_raw = await tx.get('cp_study_invocations/' + child.invocation_id)
            if parent_raw is None:
                raise AccessDenied('study_grant_rejected')
            parent = _Invocation.model_validate(parent_raw)
            if (not parent.consumed or self.clock() >= parent.expires or
                    (parent.owner, parent.session, parent.timezone) != (child.owner, child.session, child.timezone)):
                raise AccessDenied('study_grant_rejected')
            await self.session(tx, child.owner, child.session)
            tx.set(path, {**child.model_dump(mode='json'), 'consumed': True})
            return _public(child)
        return await self.documents.transact(claim)
