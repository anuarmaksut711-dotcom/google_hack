"""Actual installed ADK Runner, synthetic BaseLlm; no external Gemini calls."""
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
            parts.append(types.Part(text='Visible synthetic answer'))
        yield LlmResponse(content=types.Content(role='model', parts=parts))


def adapter(model):
    return AdkConversationAgent(ConversationTarget(project_id='campus-test-42',
        location='us-central1', model='gemini-synthetic'), model_factory=lambda: model)


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
