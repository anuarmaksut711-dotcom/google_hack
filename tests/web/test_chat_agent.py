"""Actual installed ADK Runner, synthetic BaseLlm; no external Gemini calls."""
import json
import pytest
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import PrivateAttr

from campuspulse_recovery.agent.conversation import AdkConversationAgent, ConversationTarget
from campuspulse_recovery.web.chat import ChatTurn
from tests.storage.test_plans import NOW, run


class SyntheticModel(BaseLlm):
    _requests: list = PrivateAttr(default_factory=list)
    _only_thought: bool = PrivateAttr(default=False)

    async def generate_content_async(self, llm_request, stream=False):
        self._requests.append(llm_request)
        parts = [types.Part(text='private thought', thought=True)]
        if not self._only_thought:
            answer = ('Алгоритм Крускала' if 'conversation title' in str(llm_request.config.system_instruction)
                      else 'Visible synthetic answer')
            if llm_request.config.response_schema:
                answer = json.dumps({'kind': 'message', 'message': answer, 'blocks': []})
            parts.append(types.Part(text=answer))
        yield LlmResponse(content=types.Content(role='model', parts=parts))


def adapter(model):
    return AdkConversationAgent(ConversationTarget(project_id='campus-test-42',
        location='us-central1', model='gemini-synthetic'), model_factory=lambda: model)


def test_topic_title_uses_only_message_without_sources_history_or_tools():
    async def scenario():
        model = SyntheticModel(model='gemini-synthetic')
        title = await adapter(model).suggest_title(message='Объясни алгоритм Крускала на примере')
        assert title == 'Алгоритм Крускала'
        request, = model._requests
        assert [(c.role, c.parts[0].text) for c in request.contents] == [
            ('user', 'Объясни алгоритм Крускала на примере')]
        assert not request.tools_dict and request.config.max_output_tokens <= 128
    run(scenario())


def test_transcription_passes_audio_to_configured_model_without_tools_or_history():
    async def scenario():
        model = SyntheticModel(model='gemini-synthetic')
        text = await adapter(model).transcribe(audio=b'audio fixture', mime_type='audio/webm')
        assert text == 'Visible synthetic answer'
        request, = model._requests
        assert request.model == 'gemini-synthetic'
        assert len(request.contents) == 1
        assert request.contents[0].parts[0].inline_data.data == b'audio fixture'
        assert request.contents[0].parts[0].inline_data.mime_type == 'audio/webm'
        assert not request.tools_dict
        assert 'Transcribe only' in request.config.system_instruction
        assert 'Do not translate' in request.config.system_instruction
        assert 'mixed' in request.config.system_instruction.lower()
        assert 'Russian' in request.config.system_instruction and 'Kazakh' in request.config.system_instruction
        assert 'spoken language is Russian' not in request.config.system_instruction
        assert 'spoken language is Kazakh' not in request.config.system_instruction
    run(scenario())


@pytest.mark.parametrize('options,language_codes,transcript', [
    ({}, [], 'Завтра у меня две пары.'),
    ({}, [], 'Ертең менде екі сабақ бар.'),
    ({}, [], 'Ертең емтихан, помоги составить план.'),
    ({'language': 'auto'}, [], 'Ертең емтихан, помоги составить план.'),
    ({'language': 'ru-RU'}, ['ru-RU'], 'Завтра у меня две пары.'),
    ({'language': 'kk-KZ'}, ['kk-KZ'], 'Ертең менде екі сабақ бар.'),
])
def test_gemini_api_uses_dedicated_asr_and_reads_transcription_parts(monkeypatch, options, language_codes, transcript):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    async def scenario():
        response = types.GenerateContentResponse(candidates=[types.Candidate(
            finish_reason=types.FinishReason.STOP,
            content=types.Content(parts=[types.Part(
                audio_transcription=types.Transcription(text=transcript))]))])
        generate = AsyncMock(return_value=response)
        client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate),
            aclose=AsyncMock()), close=Mock())
        agent = AdkConversationAgent(ConversationTarget(provider='gemini_api', model='gemini-synthetic'))
        monkeypatch.setattr(agent, '_create_model', lambda: SimpleNamespace(api_client=client))
        assert await agent.transcribe(audio=b'voice', mime_type='audio/mp4', **options) == transcript
        values = generate.call_args.kwargs
        assert values['model'] == 'gemini-3.5-transcribe'
        assert values['config'].audio_transcription_config.language_codes == language_codes
        assert values['contents'][0].parts[0].inline_data.data == b'voice'
        assert values['contents'][0].parts[0].inline_data.mime_type == 'audio/m4a'
        assert values['config'].system_instruction is None
        client.aio.aclose.assert_awaited_once()
    run(scenario())


def test_adk_receives_history_and_sources_but_has_no_execution_tools():
    async def scenario():
        model = SyntheticModel(model='gemini-synthetic')
        history = (ChatTurn(request_key='one', message='I have two hours.', response='What is due?',
            status='completed', created_at=NOW),
            ChatTurn(request_key='failed', message='not delivered', status='failed', created_at=NOW))
        text = await adapter(model).reply(history=history, message='Math tomorrow',
            user_id='student-1', session_id='session-1', sources={'assignments': ['TEST data'], 'calendar': []})
        assert text == 'Visible synthetic answer'
        assert len(model._requests) == 1
        request = model._requests[0]
        contents = [(c.role, ''.join(p.text or '' for p in c.parts)) for c in request.contents]
        assert contents == [('user', 'I have two hours.'), ('model', 'What is due?'), ('user', 'Math tomorrow')]
        assert not request.tools_dict
        assert 'TEST data' in str(request.config.system_instruction)
    run(scenario())


def test_thought_only_response_does_not_become_visible_answer():
    async def scenario():
        model = SyntheticModel(model='gemini-synthetic')
        model._only_thought = True
        with pytest.raises(ValueError, match='chat_response_unavailable'):
            await adapter(model).reply(history=(), message='hello', user_id='student-1',
                session_id='session-1', sources={})
    run(scenario())


def test_calendar_advice_profile_can_plan_in_chat_without_recovery_buttons_or_tools():
    async def scenario():
        model = SyntheticModel(model='gemini-synthetic')
        agent = AdkConversationAgent(ConversationTarget(provider='gemini_api',
            model='gemini-synthetic'), model_factory=lambda: model, profile='calendar_advice')
        await agent.reply(history=(), message='Plan my week', user_id='student-1',
            session_id='session-1', sources={'assignments':['Moodle assignment due October 24']})
        request, = model._requests
        instruction = str(request.config.system_instruction)
        assert 'конкретные даты' in instruction and 'Moodle assignment due October 24' in instruction
        assert 'Прошедший срок не доказывает' in instruction
        assert 'Не направляй к кнопке' in instruction
        assert request.config.response_schema is not None
        # Exercise the real GenAI converter: the Developer API rejects this
        # keyword even when false, while the server still rejects extra fields.
        from google import genai
        from google.genai import _transformers
        with genai.Client(api_key='synthetic-unused-key') as sdk:
            wire = _transformers.t_schema(sdk._api_client, request.config.response_schema)
            assert 'additionalProperties' not in wire.model_dump_json(by_alias=True, exclude_none=True)
            assert wire.properties['blocks'].items.properties['start'].type == 'STRING'
        assert not request.tools_dict
    run(scenario())


def test_cloud_factory_rejects_local_runner_as_agent_engine_substitute():
    from campuspulse_recovery.web.views.app import create_ui_app
    from tests.web.test_approval import Setup, ORIGIN
    h = Setup()
    local = adapter(SyntheticModel(model='gemini-synthetic'))
    with pytest.raises(ValueError, match='agent_engine_chat_runtime_required'):
        create_ui_app(policy=h.policy, documents=h.documents, verifier=h.verifier,
            origin=ORIGIN, environment='cloud', clock=h.clock, chat_agent=local)
