"""Canonical historical chat records must survive optional progress metadata."""
from datetime import UTC, datetime
import hashlib
import json
from typing import Annotated, Literal

from pydantic import Field, ValidationError
import pytest

from campuspulse_recovery.domain.models import Contract, Instant, canonical_json
from campuspulse_recovery.domain.ports import BindingConflict
from campuspulse_recovery.domain.study_progress import StudyProgress, StudyToolProgress
from campuspulse_recovery.observability.events import TechnicalId
from campuspulse_recovery.storage.plans import _decode, _encode
from campuspulse_recovery.web.chat import (
    ChatToolTrace, ChatTurn, Conversation, Message, Reply,
    _NullableProgressConversation, _NullableProgressTurn, _conversation,
)

NOW = datetime(2026, 10, 22, tzinfo=UTC)


class BeforeProgressTurn(Contract):
    """Frozen deployed 1382 schema, independent of the new progress field."""
    request_key: TechnicalId
    message: Message
    status: Literal['working', 'completed', 'failed', 'unknown']
    created_at: Instant
    response: Reply | None = None
    duration_ms: int | None = None
    tool_trace: Annotated[tuple[ChatToolTrace, ...], Field(max_length=12)] = ()
    agent_run_id: TechnicalId | None = None


class BeforeProgressConversation(Contract):
    turns: Annotated[tuple[BeforeProgressTurn, ...], Field(max_length=40)] = ()


def turn_values():
    return dict(request_key='saved_chat', message='Сохранённый запрос', status='completed',
                created_at=NOW, response='Сохранённый ответ', duration_ms=42)


def test_deployed_history_without_progress_keeps_exact_canonical_bytes_and_digest():
    old = _encode('chat-v1', BeforeProgressConversation(turns=(BeforeProgressTurn(**turn_values()),)))
    assert b'agent_progress' not in old['body']
    current = _conversation(old)
    assert current.turns[0].response == 'Сохранённый ответ'
    assert current.turns[0].agent_progress is None
    assert _encode('chat-v1', current) == old


def test_new_ordinary_turn_is_readable_by_the_original_deployed_decoder():
    document = _encode('chat-v1', Conversation(turns=(ChatTurn(**turn_values()),)))
    assert b'agent_progress' not in document['body']
    original = _decode(document, 'chat-v1', BeforeProgressConversation)
    assert original.turns[0].response == 'Сохранённый ответ'


def test_first_trace_rollout_explicit_null_is_verified_then_normalized():
    previous = _encode('chat-v1', _NullableProgressConversation(
        turns=(_NullableProgressTurn(**turn_values()),)))
    assert b'"agent_progress":null' in previous['body']
    current = _conversation(previous)
    assert current.turns[0].agent_progress is None
    assert b'agent_progress' not in _encode('chat-v1', current)['body']


@pytest.mark.parametrize('corruption', ['digest', 'body', 'unknown_field', 'noncanonical'])
def test_compatibility_reader_never_repairs_invalid_original_integrity(corruption):
    document = _encode('chat-v1', BeforeProgressConversation(
        turns=(BeforeProgressTurn(**turn_values()),)))
    if corruption == 'digest':
        document['digest'] = '0' * 64
    elif corruption == 'body':
        document['body'] = document['body'].replace(b'42', b'43')
    else:
        value = json.loads(document['body'])
        if corruption == 'unknown_field':
            value['turns'][0]['private_unknown_field'] = 'rejected'
            document['body'] = canonical_json(value).encode('utf-8')
        else:
            document['body'] = json.dumps(value, ensure_ascii=False).encode('utf-8')
        document['digest'] = hashlib.sha256(document['body']).hexdigest()
    with pytest.raises(BindingConflict, match='storage_record_integrity_failed'):
        _conversation(document)


def test_real_final_and_partial_progress_remain_durable_after_recreation():
    for success in (True, False):
        progress = StudyProgress(phase='tools', tool_calls=(
            StudyToolProgress(call_id=1, tool='read_assignments', status='completed',
                              duration_ms=52, result_count=15),
            StudyToolProgress(call_id=2, tool='read_schedule', status='running'),
        )).settled(success=success)
        values = {**turn_values(), 'agent_progress': progress}
        if not success:
            values.update(status='failed', response=None)
        document = _encode('chat-v1', Conversation(turns=(ChatTurn(**values),)))
        recreated = _conversation(document)
        assert recreated.turns[0].agent_progress == progress
        assert recreated.turns[0].agent_progress.tool_calls[1].status == 'unknown'
        assert _encode('chat-v1', recreated) == document


@pytest.mark.parametrize('status', ['completed', 'partial', 'failed', 'unavailable'])
def test_github_observations_are_typed_and_durable_without_source_content(status):
    observation = dict(tool='read_github_assignment', status=status, duration_ms=25, result_count=3)
    trace = ChatToolTrace(**observation)
    progress = StudyProgress(phase='completed', tool_calls=(StudyToolProgress(call_id=1, **observation),))
    value = ChatTurn(**turn_values(), tool_trace=(trace,), agent_progress=progress)
    document = _encode('chat-v1', Conversation(turns=(value,)))
    restored = _conversation(document).turns[0]
    assert restored.tool_trace == (trace,)
    assert restored.agent_progress == progress
    assert _encode('chat-v1', Conversation(turns=(restored,))) == document


@pytest.mark.parametrize('tool', ['write_github_issue', 'read_unknown', 'apply_plan'])
def test_new_github_allowlist_keeps_unknown_and_mutating_tools_closed(tool):
    observation = dict(tool=tool, status='completed', duration_ms=25, result_count=1)
    with pytest.raises(ValidationError):
        ChatToolTrace(**observation)
    with pytest.raises(ValidationError):
        StudyToolProgress(call_id=1, **observation)


def test_github_observations_reject_repository_source_contents():
    observation = dict(tool='read_github_assignment', status='completed', duration_ms=25,
                       result_count=1, readme='Untrusted repository text')
    with pytest.raises(ValidationError):
        ChatToolTrace(**observation)
    with pytest.raises(ValidationError):
        StudyToolProgress(call_id=1, **observation)
