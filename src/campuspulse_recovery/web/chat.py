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
from pydantic import Field, StrictInt, StringConstraints, TypeAdapter

from ..domain.models import Contract, Instant, canonical_json
from ..domain.chat_plan import ClassSchedule, render_advice
from ..domain.study_progress import StudyProgress, StudyToolProgress
from ..domain.ports import BindingConflict
from ..observability.events import TechnicalId
from ..security.context import AccessDenied
from ..storage.plans import _Repository, _decode, _encode
from .app import body, _json
from .workspace import ChatId, WorkspaceStore

Message = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=4000)]
Reply = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=12000)]


class ChatToolTrace(Contract):
    """Confirmed runtime observations; no prompts, tokens or source contents."""
    tool: Literal['read_assignments', 'read_schedule']
    status: Literal['completed', 'failed', 'unavailable', 'not_connected']
    duration_ms: Annotated[StrictInt, Field(ge=0, le=150_000)]
    result_count: Annotated[StrictInt, Field(ge=0, le=1000)] = 0


class ChatReply(Contract):
    response: Reply
    tool_trace: Annotated[tuple[ChatToolTrace, ...], Field(max_length=12)] = ()
    class_schedule: ClassSchedule | None = None
    agent_run_id: TechnicalId | None = None


class ChatRequest(Contract):
    request_key: TechnicalId
    message: Message
    chat_id: ChatId = 'default'


class ChatTurn(Contract):
    request_key: TechnicalId
    message: Message
    status: Literal['working', 'completed', 'failed', 'unknown']
    created_at: Instant
    response: Reply | None = None
    duration_ms: int | None = None
    tool_trace: Annotated[tuple[ChatToolTrace, ...], Field(max_length=12)] = ()
    agent_run_id: TechnicalId | None = None
    # Preserve the exact chat-v1 bytes of pre-progress turns. In particular,
    # adding a default None must not invalidate an existing canonical digest.
    agent_progress: StudyProgress | None = Field(default=None, exclude_if=lambda value: value is None)


class Conversation(Contract):
    turns: Annotated[tuple[ChatTurn, ...], Field(max_length=40)] = ()


class _NullableProgressTurn(ChatTurn):
    """Exact shape emitted by the first progress rollout, including null."""
    agent_progress: StudyProgress | None = None


class _NullableProgressConversation(Contract):
    turns: Annotated[tuple[_NullableProgressTurn, ...], Field(max_length=40)] = ()


def _conversation(document):
    if document is None:
        return Conversation()
    try:
        return _decode(document, 'chat-v1', Conversation)
    except BindingConflict:
        # Verify the original bytes and digest against that exact historical
        # model before converting; never strip fields or repair stored hashes.
        previous = _decode(document, 'chat-v1', _NullableProgressConversation)
        return Conversation.model_validate(previous.model_dump(mode='json'))


def history_limit_reached(chat):
    return len(chat.turns) >= 40 or len(canonical_json(chat).encode()) > 180_000


class ChatAgent(Protocol):
    async def reply(self, *, history: tuple[ChatTurn, ...], message: str,
                    user_id: str, session_id: str, sources: dict) -> str | ChatReply: ...


class ChatStore(_Repository):
    def __init__(self, *args, owner_history=False, working_seconds=90, **kwargs):
        super().__init__(*args, **kwargs)
        self.owner_history = owner_history
        self.working_seconds = working_seconds
        self.workspace = WorkspaceStore(*args, owner_history=owner_history, **kwargs)

    def path(self, context, chat_id='default'):
        return self.workspace.conversation_path(context, chat_id)

    async def read(self, context, chat_id='default'):
        async def transact(tx):
            await self.workspace.require_chat(context, chat_id, reader=tx)
            saved = await tx.get(self.path(context, chat_id))
            return _conversation(saved)
        return await self.documents.transact(transact)

    async def claim(self, context, value, now):
        path = self.path(context, value.chat_id)
        async def transact(tx):
            await self.workspace.require_chat(context, value.chat_id, reader=tx)
            saved = await tx.get(path)
            chat = _conversation(saved)
            previous = next((t for t in chat.turns if t.request_key == value.request_key), None)
            if previous:
                if previous.message != value.message:
                    raise BindingConflict('chat_request_changed')
                return chat, False
            if history_limit_reached(chat):
                raise BindingConflict('chat_history_limit')
            turns = []
            for turn in chat.turns:
                if turn.status == 'working':
                    if now < turn.created_at + timedelta(seconds=self.working_seconds):
                        raise BindingConflict('chat_turn_in_progress')
                    turn = turn.model_copy(update={'status': 'unknown'})
                turns.append(turn)
            turns.append(ChatTurn(request_key=value.request_key, message=value.message,
                                  status='working', created_at=now))
            chat = Conversation(turns=tuple(turns))
            tx.set(path, _encode('chat-v1', chat))
            return chat, True
        return await self.documents.transact(transact)

    async def finish(self, context, key, *, response, duration_ms, chat_id='default',
                     tool_trace=(), agent_run_id=None, agent_progress=None):
        path = self.path(context, chat_id)
        async def transact(tx):
            await self.workspace.require_chat(context, chat_id, reader=tx)
            chat = _conversation(await tx.get(path))
            turns = list(chat.turns)
            for index, turn in enumerate(turns):
                if turn.request_key == key:

                    if turn.status != 'working':
                        raise BindingConflict('chat_turn_settled')
                    turns[index] = ChatTurn(**{**turn.model_dump(),
                        'status': 'failed' if response is None else 'completed',
                        'response': response, 'duration_ms': duration_ms,
                        'tool_trace': tool_trace, 'agent_run_id': agent_run_id,
                        'agent_progress': agent_progress})
                    result = Conversation(turns=tuple(turns))
                    tx.set(path, _encode('chat-v1', result))
                    return result
            raise BindingConflict('chat_turn_missing')
        return await self.documents.transact(transact)


def create_chat_router(*, auth, documents, policy, agent=None, projection=None, owner_history=False,
                       structured_advice=False, additional_sources=None):
    router = APIRouter()
    owns_sources = getattr(agent, 'owns_sources', False) is True
    working_seconds = 150 if owns_sources else 90
    store = ChatStore(documents, policy, owner_history=owner_history, working_seconds=working_seconds)
    workspace = WorkspaceStore(documents, policy, owner_history=owner_history)

    async def fresh(request, original):
        current = await auth.load(request)
        if current.session != original.session:
            raise AccessDenied('chat_session_changed')

    async def progress(context, chat_id, request_key):
        read = getattr(agent, 'read_progress', None)
        if not callable(read):
            return None
        try:
            async with asyncio.timeout(2):
                return await read(user_id=context.trusted.owner_id, session_id=context.session_id,
                                  chat_id=chat_id, request_key=request_key)
        except AccessDenied:
            raise
        except Exception:
            # Observability outages must not hide durable replies/history.
            return None

    async def view(chat, context, chat_id):
        turns = [t.model_dump(mode='json') for t in chat.turns]
        for original, turn in zip(chat.turns, turns):
            if turn['status'] == 'working' and auth.now() >= original.created_at + timedelta(seconds=working_seconds):
                turn['status'] = 'unknown'
            if not turn['tool_trace']:
                turn.pop('tool_trace')
            if turn['agent_run_id'] is None:
                turn.pop('agent_run_id')
            live = (await progress(context, chat_id, original.request_key)
                    if original.status == 'working' else original.agent_progress)
            if live is not None:
                if turn['status'] != 'working':
                    live = live.settled(success=turn['status'] == 'completed')
                turn['agent_progress'] = live.model_dump(mode='json', exclude_none=True)
            else:
                turn.pop('agent_progress', None)
        result = {'available': agent is not None, 'turns': turns,
                  'limit_reached': history_limit_reached(chat)}
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
        if set(request.query_params) - {'chat_id'} or len(request.query_params.getlist('chat_id')) > 1:
            raise ValueError('invalid_chat_selector')
        chat_id = TypeAdapter(ChatId).validate_python(request.query_params.get('chat_id', 'default'))
        with auth.request_scope(credential) as context:
            chat = await store.read(context, chat_id)
            value = await view(chat, context, chat_id)
            await fresh(request, credential)
            return _json(value)

    @router.post('/api/chat')
    async def send(request: Request):
        credential = await auth.load(request, mutation=True)
        if request.query_params:
            raise ValueError('chat_selectors_in_body_required')
        value = await body(request, ChatRequest)
        if not value.message.strip():
            raise ValueError('empty_chat_message')
        if agent is None:
            return _json({'error': 'chat_not_configured'}, 503)
        with auth.request_scope(credential) as context:
            try:
                chat, claimed = await store.claim(context, value, auth.now())
            except BindingConflict as error:
                if str(error) != 'chat_history_limit':
                    raise
                await fresh(request, credential)
                return _json({'error': 'chat_history_limit'}, 409)
            await fresh(request, credential)
            if not claimed:
                turn = next(t for t in chat.turns if t.request_key == value.request_key)
                await workspace.record_activity(context, value.chat_id, chat.turns[0].message, turn.created_at)
                result = await view(chat, context, value.chat_id)
                await fresh(request, credential)
                return _json(result)
            started = monotonic()
            response = None
            tool_trace, agent_run_id = (), None
            try:
                async with asyncio.timeout(135 if owns_sources else 60):

                    sources = {} if projection is None or owns_sources else (await projection.project(
                        context, run=None, plan=None)).model_dump(mode='json',
                            include={'assignments', 'calendar', 'class_schedule'})
                    if additional_sources is not None and not owns_sources:
                        sources['google_calendar'] = await additional_sources(context)
                    if not owns_sources:
                        sources['calendar_write'] = {
                            'enabled': callable(getattr(request.app.state, 'calendar_plan_proposer', None)),
                            'now': auth.now().isoformat(), 'timezone': policy.config.student.timezone,
                            'horizon_hours': 72}
                    await fresh(request, credential)
                    reply_for_turn = getattr(agent, 'reply_for_turn', None)
                    arguments = dict(history=chat.turns[:-1], message=value.message,
                        user_id=context.trusted.owner_id, session_id=context.session_id, sources=sources)
                    response = (await reply_for_turn(chat_id=value.chat_id,
                        request_key=value.request_key, **arguments) if callable(reply_for_turn)
                        else await agent.reply(**arguments))
                    runtime_schedule = None
                    if isinstance(response, ChatReply):
                        tool_trace, agent_run_id = response.tool_trace, response.agent_run_id
                        runtime_schedule, response = response.class_schedule, response.response
                    validated_blocks, validated_timetables = [], []
                    if structured_advice:
                        schedule = runtime_schedule or sources.get('class_schedule')
                        if projection is not None and hasattr(projection, 'current_class_schedule'):
                            current = await projection.current_class_schedule(context)
                            previous = (schedule.model_dump(mode='json') if isinstance(schedule, ClassSchedule) else schedule)
                            if (previous and previous['status'] != 'not_connected' and
                                    (current is None or current.status == 'not_connected')):
                                current = ClassSchedule(status='unavailable')
                            schedule = current
                        response = render_advice(response, schedule=schedule, now=auth.now(),
                            timezone=policy.config.student.timezone,
                            validated_blocks=validated_blocks, validated_timetables=validated_timetables)
                    drafter = getattr(request.app.state, 'class_schedule_drafter', None)
                    if validated_timetables:
                        await fresh(request, credential)
                        if drafter is not None:
                            draft_id = await drafter(context, value.chat_id + ':' + value.request_key,
                                                     validated_timetables[0])
                            await fresh(request, credential)
                            response += '\n\n/planning#draft=' + draft_id
                        else:
                            response += '\n\nОткройте «Пары и время для учёбы», чтобы ввести и сохранить расписание.'
                    proposer = getattr(request.app.state, 'calendar_plan_proposer', None)
                    if validated_blocks and proposer is not None:
                        try:
                            await fresh(request, credential)
                            saved = await proposer(validated_blocks, value.chat_id + ':' + value.request_key,
                                                   context.session_id)
                            if saved:
                                response = response.replace('План остаётся рекомендацией в чате.',
                                    'Откройте «Предложенное расписание»: проверьте события, подтвердите и добавьте их в Google Calendar.')
                            else:
                                response += '\n\nЧтобы сохранить события, подключите Google Calendar.'
                        except BindingConflict as error:
                            response = ('Чтобы проверить и сохранить план, заполните пары и проверьте даты на странице «Пары и время для учёбы». Если пар нет, явно сохраните пустое расписание на нужный период. Затем запросите новый план.'
                                if str(error).startswith('calendar_class_schedule') else
                                'Не удалось сохранить этот вариант: время уже занято или календарь изменился. Попросите составить новый план.')

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
            final_progress = await progress(context, value.chat_id, value.request_key)
            if tool_trace:
                # The completed runtime response survives an auxiliary progress
                # outage. Use its independently typed facts to settle all calls.
                final_progress = StudyProgress(phase='completed' if response is not None else 'failed',
                    tool_calls=tuple(StudyToolProgress(call_id=index,
                        **item.model_dump(exclude={'schema_version'}))
                        for index, item in enumerate(tool_trace[:8], 1)))
            elif final_progress is not None:
                final_progress = final_progress.settled(success=response is not None)
            await fresh(request, credential)
            chat = await store.finish(context, value.request_key, response=response,
                duration_ms=round((monotonic() - started) * 1000), chat_id=value.chat_id,
                tool_trace=tool_trace, agent_run_id=agent_run_id, agent_progress=final_progress)
            turn = next(t for t in chat.turns if t.request_key == value.request_key)
            title = None
            if await workspace.needs_title(context, value.chat_id) and callable(getattr(agent, 'suggest_title', None)):
                try:
                    async with asyncio.timeout(8):
                        title = await agent.suggest_title(message=chat.turns[0].message)
                except Exception:
                    pass  # A title failure must not hide an already saved reply.
            await fresh(request, credential)
            await workspace.record_activity(context, value.chat_id, chat.turns[0].message, turn.created_at, title=title)
            result = await view(chat, context, value.chat_id)
            await fresh(request, credential)
            return _json(result)

    return router
