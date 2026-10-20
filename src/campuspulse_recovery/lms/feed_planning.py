"""Recovery planning over the owner's existing durable iCalendar connection."""
from types import SimpleNamespace
from typing import Annotated
from urllib.parse import parse_qs, urlsplit

from pydantic import Field, StrictInt

from ..domain.models import Contract
from ..domain.ports import BindingConflict
from ..security.context import AccessDenied
from ..storage.plans import _Repository, _key
from .calendar_feed import FeedState, SITE, validate_feed_url
from .planning import MoodlePlanningStore, PlanningState, PlanningUpdate, moodle_resource_id


class FeedEffortEstimate(Contract):
    task_id: Annotated[str, Field(strict=True, min_length=1, max_length=512)]
    remaining_minutes: Annotated[StrictInt, Field(ge=0, le=100_000)] | None


class FeedPlanningState(PlanningState):
    estimates: Annotated[tuple[FeedEffortEstimate, ...], Field(max_length=500)] = ()


class FeedPlanningUpdate(PlanningUpdate):
    estimates: Annotated[tuple[FeedEffortEstimate, ...], Field(max_length=100)] = ()


class FeedPlanningRecord(Contract):
    owner_id: str
    resource_id: str
    state: FeedPlanningState


class FeedPlanningStore(MoodlePlanningStore):
    state_model = FeedPlanningState
    record_model = FeedPlanningRecord
    update_model = FeedPlanningUpdate

    def __init__(self, documents, policy, *, user_id, clock):
        _Repository.__init__(self, documents, policy)
        self.clock = clock
        owner = policy.config.student.trusted_user_id
        self.connection = SimpleNamespace(documents=documents, owner_id=owner,
            site_url=SITE, expected_user_id=user_id)
        self.resource_id = moodle_resource_id(SITE, user_id)
        self.path = 'cp_moodle_planning/' + _key(owner, self.resource_id)

    async def cached(self, context, *, reader=None):
        from ..cloud.calendar_feed import CloudCalendarFeed
        self._request(context)
        reader = reader or self.documents
        owner, user = self.connection.owner_id, self.connection.expected_user_id
        record = await reader.get('cp_moodle_accounts/' + owner)
        if record is None:
            return FeedState(owner_id=owner, expected_user_id=user, connected=False, stale=True)
        if set(record) != {'owner_id', 'url'} or record['owner_id'] != owner:
            raise AccessDenied('moodle_account_binding_mismatch')
        try:
            url = validate_feed_url(record['url'], user)
        except ValueError:
            raise BindingConflict('moodle_account_binding_changed') from None
        if parse_qs(urlsplit(url).query)['preset_time'] != ['recentupcoming']:
            raise BindingConflict('moodle_export_period_insufficient')
        feed = CloudCalendarFeed(documents=self.documents, url=url, owner_id=owner, user_id=user, clock=self.clock)
        await feed.load(reader=reader)
        self.policy.require_context(context)
        return feed.state()
