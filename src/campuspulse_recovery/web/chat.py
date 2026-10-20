"""Authenticated conversation, separate from exact-plan approval/execution.

The same explicit document adapter as the application owns history and claims.
No model invocation occurs in a transaction. Unknown/failed turns are never
automatically dispatched again, including after a process restart.
"""
import asyncio
from datetime import timedelta
from time import monotonic
from typing import Annotated, Literal, Protocol

from fastapi import APIRouter, Request
from pydantic import Field, StringConstraints

from ..domain.models import Contract, Instant, canonical_json
from ..domain.chat_plan import ClassSchedule, render_advice
from ..domain.ports import BindingConflict
from ..observability.events import TechnicalId
from ..security.context import AccessDenied
from ..storage.plans import _Repository, _decode, _encode, _key
from .app import body, _json

Message = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=4000)]
Reply = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=12000)]


class ChatRequest(Contract):
    request_key: TechnicalId
    message: Message


class ChatTurn(Contract):
    request_key: TechnicalId
    message: Message
    status: Literal['working', 'completed', 'failed', 'unknown']
    created_at: Instant
    response: Reply | None = None
    duration_ms: int | None = None


class Conversation(Contract):
    turns: Annotated[tuple[ChatTurn, ...], Field(max_length=40)] = ()


class ChatAgent(Protocol):
    async def reply(self, *, history: tuple[ChatTurn, ...], message: str,
                    user_id: str, session_id: str, sources: dict) -> str: ...


class ChatStore(_Repository):
    def __init__(self, *args, owner_history=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.owner_history = owner_history

    def path(self, context):
        c = self._request(context)
        return 'cp_conversations/' + _key(c.trusted.project_id, c.trusted.owner_id,
            c.trusted.calendar_id, c.trusted.config_revision,
            'owner-history-v1' if self.owner_history else c.session_id)

    async def read(self, context):
        saved = await self.documents.get(self.path(context))
        return Conversation() if saved is None else _decode(saved, 'chat-v1', Conversation)

    async def claim(self, context, value, now):
        path = self.path(context)
        async def transact(tx):
            saved = await tx.get(path)
            chat = Conversation() if saved is None else _decode(saved, 'chat-v1', Conversation)
            previous = next((t for t in chat.turns if t.request_key == value.request_key), None)
            if previous:
                if previous.message != value.message:
                    raise BindingConflict('chat_request_changed')
                return chat, False
            if len(chat.turns) >= 40 or len(canonical_json(chat).encode()) > 180_000:
                raise BindingConflict('chat_history_limit')
            turns = []
            for turn in chat.turns:
                if turn.status == 'working':
                    if now < turn.created_at + timedelta(seconds=90):
                        raise BindingConflict('chat_turn_in_progress')
                    turn = turn.model_copy(update={'status': 'unknown'})
                turns.append(turn)
            turns.append(ChatTurn(request_key=value.request_key, message=value.message,
                                  status='working', created_at=now))
            chat = Conversation(turns=tuple(turns))
            tx.set(path, _encode('chat-v1', chat))
            return chat, True
        return await self.documents.transact(transact)

    async def finish(self, context, key, *, response, duration_ms):
        path = self.path(context)
        async def transact(tx):
            chat = _decode(await tx.get(path), 'chat-v1', Conversation)
            turns = list(chat.turns)
            for index, turn in enumerate(turns):
                if turn.request_key == key:

                    if turn.status != 'working':
                        raise BindingConflict('chat_turn_settled')
                    turns[index] = ChatTurn(**{**turn.model_dump(),
                        'status': 'failed' if response is None else 'completed',
                        'response': response, 'duration_ms': duration_ms})
                    result = Conversation(turns=tuple(turns))
                    tx.set(path, _encode('chat-v1', result))
                    return result
            raise BindingConflict('chat_turn_missing')
        return await self.documents.transact(transact)


def create_chat_router(*, auth, documents, policy, agent=None, projection=None, owner_history=False,
                       structured_advice=False):
    router = APIRouter()
    store = ChatStore(documents, policy, owner_history=owner_history)

    async def fresh(request, original):
        current = await auth.load(request)
        if current.session != original.session:
            raise AccessDenied('chat_session_changed')

    def view(chat):
        turns = [t.model_dump(mode='json') for t in chat.turns]
        for original, turn in zip(chat.turns, turns):
            if turn['status'] == 'working' and auth.now() >= original.created_at + timedelta(seconds=90):
                turn['status'] = 'unknown'
        result = {'available': agent is not None, 'turns': turns}
        if callable(getattr(agent, 'transcribe', None)):
            result['speech_available'] = True
        failure = getattr(agent, 'last_failure', None)
        if failure in {'provider_request_rejected','provider_auth_failed','provider_access_denied',
                'provider_model_unavailable','provider_quota','provider_unavailable','chat_runtime_failed'}:
            result['failure_code'] = failure
        return result

    @router.get('/api/chat')
    async def history(request: Request):
        credential = await auth.load(request)
        with auth.request_scope(credential) as context:
            chat = await store.read(context)
            await fresh(request, credential)
            return _json(view(chat))

    @router.post('/api/chat')
    async def send(request: Request):
        credential = await auth.load(request, mutation=True)
        value = await body(request, ChatRequest)
        if not value.message.strip():
            raise ValueError('empty_chat_message')
        if agent is None:
            return _json({'error': 'chat_not_configured'}, 503)
        with auth.request_scope(credential) as context:
            chat, claimed = await store.claim(context, value, auth.now())
            await fresh(request, credential)
            if not claimed:
                return _json(view(chat))
            started = monotonic()
            response = None
            try:
                async with asyncio.timeout(60):

                    sources = {} if projection is None else (await projection.project(
                        context, run=None, plan=None)).model_dump(mode='json',
                            include={'assignments', 'calendar', 'class_schedule'})
                    await fresh(request, credential)
                    response = await agent.reply(history=chat.turns[:-1], message=value.message,
                        user_id=context.trusted.owner_id, session_id=context.session_id, sources=sources)
                    if structured_advice:
                        schedule = sources.get('class_schedule')
                        if projection is not None and hasattr(projection, 'current_class_schedule'):
                            current = await projection.current_class_schedule(context)
                            if (schedule and schedule['status'] != 'not_connected' and
                                    (current is None or current.status == 'not_connected')):
                                current = ClassSchedule(status='unavailable')
                            schedule = current
                        response = render_advice(response, schedule=schedule, now=auth.now(),
                                                 timezone=policy.config.student.timezone)

                    response = ChatTurn(request_key=value.request_key, message=value.message,
                        status='completed', created_at=auth.now(), response=response).response
                    if not response.strip():
                        raise ValueError('empty_agent_response')
            except asyncio.CancelledError:
                raise
            except AccessDenied:
                raise
            except Exception:
                response = None
            await fresh(request, credential)
            chat = await store.finish(context, value.request_key, response=response,
                                      duration_ms=round((monotonic() - started) * 1000))
            await fresh(request, credential)
            return _json(view(chat))

    return router
