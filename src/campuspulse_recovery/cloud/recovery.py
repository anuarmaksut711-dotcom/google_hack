"""Website opt-in for the shared account-bound custom Agent Engine runtime."""
from pydantic import model_validator

from ..domain.models import Contract
from ..web.agent_client import AgentEngineTarget
from .accounts import account_policy, account_settings
from .backend import CloudBackend
from .engine_transport import AgentEngineTransport
from .settings import CloudSettings


class CalendarRecoverySettings(Contract):
    settings: CloudSettings
    target: AgentEngineTarget

    @model_validator(mode='after')
    def matching_target(self):
        if (self.target.project_id != self.settings.application.project_id
                or self.target.location != self.settings.application.agent_region
                or self.settings.chat.provider != 'vertex'
                or not self.settings.calendar_feed_accounts):
            raise ValueError('explicit_matching_feed_engine_required')
        return self

    def backend(self, principal, documents, *, clock):
        selected = account_settings(self.settings, principal)
        if selected is None:
            return None
        return CloudBackend(selected, account_policy(selected), documents, clock=clock,
            worker=AgentEngineTransport(self.target, audience=selected.agent_origin))
