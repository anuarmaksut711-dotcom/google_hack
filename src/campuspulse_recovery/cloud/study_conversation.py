"""Owner-bound website adapter for the read-only managed study agent."""
from contextvars import ContextVar

from ..domain.ports import PortError
from ..security.context import AccessDenied
from ..web.chat import ChatReply
from .engine_transport import AgentEngineTransport
from .study_grants import StudyAgentSettings, StudyGrants


class StudyConversation:
    local_preview_only = False
    owns_sources = True

    def __init__(self, settings, documents, *, policy, delegate, clock, transport=None, grants=None):
        self.settings = StudyAgentSettings.model_validate(settings)
        self.policy, self.delegate = policy, delegate
        if ((documents.project_id, documents.database_id) !=
                (policy.config.project_id, policy.config.firestore_database)
                or self.settings.target.project_id != policy.config.project_id):
            raise ValueError('matching_study_store_and_target_required')
        self.owner = policy.config.student.trusted_user_id
        self.grants = grants or StudyGrants(documents, clock=clock)
        self.transport = transport or AgentEngineTransport(self.settings.target,
                                                          audience=self.settings.agent_audience)
        self._failure = ContextVar('study_chat_failure', default=None)

    @property
    def last_failure(self):
        return self._failure.get()

    async def reply(self, *, history, message, user_id, session_id, sources):
        self._failure.set(None)
        if user_id != self.owner:
            raise AccessDenied('study_owner_mismatch')
        try:
            reference = await self.grants.issue_invocation(owner=self.owner, session=session_id,
                message=message, history=history, timezone=self.policy.config.student.timezone)
            payload = await self.transport.request(reference, seconds=120)
            # Successful Engine transport alone is insufficient: require the
            # closed runtime result and bounded, independently typed trace.
            if not isinstance(payload, dict) or set(payload) != {'reply', 'trace', 'class_schedule', 'run_id'}:
                raise ValueError('study_reply_invalid')
            result = ChatReply.model_validate({'response': payload['reply'],
                'tool_trace': payload['trace'], 'class_schedule': payload['class_schedule'],
                'agent_run_id': payload['run_id']})
            await self.grants.session(self.grants.documents, self.owner, session_id)
            return result
        except AccessDenied:
            raise
        except Exception:
            self._failure.set('chat_runtime_failed')
            # Never dispatch the same prompt to local Gemini as a fallback.
            raise PortError('study_agent_unavailable') from None

    async def suggest_title(self, **kwargs):
        return await self.delegate.suggest_title(**kwargs)

    async def transcribe(self, **kwargs):
        return await self.delegate.transcribe(**kwargs)
