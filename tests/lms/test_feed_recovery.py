"""Real durable ICS cache/planning behavior, without LMS or Google credentials."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest

from campuspulse_recovery.cloud.calendar_feed import CloudCalendarFeed
from campuspulse_recovery.domain.ports import BindingConflict, IncompleteRead
from campuspulse_recovery.lms.planning import MoodleAssignmentsReader, moodle_resource_id
from campuspulse_recovery.security.resources import ResourcePolicy
from campuspulse_recovery.storage.snapshots import FirestoreSnapshotStore
from tests.lms.test_multiuser import url
from tests.storage.test_plans import Harness, NOW, interval


async def setup(preset='recentupcoming'):
    from campuspulse_recovery.lms.feed_planning import FeedPlanningStore
    h = Harness()
    h.policy = ResourcePolicy(h.config, environment='local',
        assignments_resource_id=moodle_resource_id('https://lms.astanait.edu.kz', 42))
    h.snapshots = FirestoreSnapshotStore(h.documents, h.policy)
    h.now = NOW
    owner = h.config.student.trusted_user_id
    feed_url = url(42).replace('recentupcoming', preset)
    h.documents.data['cp_moodle_accounts/' + owner] = {'owner_id': owner, 'url': feed_url}
    raw = ('BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:actual-uid@lms\r\n'
           'SUMMARY:Exam preparation\r\nDTSTART:' + (NOW + timedelta(hours=6)).strftime('%Y%m%dT%H%M%SZ') +
           '\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n').encode()
    feed = CloudCalendarFeed(documents=h.documents, url=feed_url, owner_id=owner, user_id=42,
        clock=lambda: h.now, transport=httpx.MockTransport(lambda request: httpx.Response(200, content=raw)))
    await feed.refresh()
    inputs = FeedPlanningStore(h.documents, h.policy, user_id=42, clock=lambda: h.now)
    reader = MoodleAssignmentsReader(SimpleNamespace(policy=h.policy), h.snapshots, inputs=inputs, clock=lambda: h.now)
    return h, feed, inputs, reader


def test_actual_feed_uid_effort_completion_and_revision_survive_reopen():
    async def scenario():
        from campuspulse_recovery.lms.feed_planning import FeedPlanningStore
        h, feed, inputs, reader = await setup()
        with h.request() as context:
            result = await inputs.update(context, {'expected_revision': 0,
                'estimates': [{'task_id': 'actual-uid@lms', 'remaining_minutes': 90}],
                'working_windows': [interval(NOW + timedelta(hours=1), hours=3).model_dump()]})
            assert result.revision == 1 and result.estimates[0].task_id == 'actual-uid@lms'
            reopened = FeedPlanningStore(h.documents, h.policy, user_id=42, clock=lambda: h.now)
            assert await reopened.get(context) == result
            with pytest.raises(BindingConflict, match='revision_conflict'):
                await reopened.update(context, {'expected_revision': 0})
            with pytest.raises(BindingConflict, match='task_not_found'):
                await reopened.update(context, {'expected_revision': 1,
                    'estimates': [{'task_id': 'foreign-uid', 'remaining_minutes': 5}]})
        with h.request('mcp') as context:
            snapshot = await reader.read_assignments(context, {'horizon': interval(hours=72)})
            assert len(snapshot.assignments) == 1
            assert snapshot.assignments[0].source_row == 'actual-uid@lms'
            assert snapshot.assignments[0].remaining_minutes == 90
        await feed.set_local_status('actual-uid@lms', True)
        with h.request('mcp') as context:
            assert not (await reader.read_assignments(context, {'horizon': interval(hours=72)})).assignments
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['stale', 'wrong_user', 'foreign_owner', 'narrow_export'])
def test_changed_or_stale_feed_cannot_become_empty_valid_work(change):
    async def scenario():
        h, feed, inputs, reader = await setup('weeknow' if change == 'narrow_export' else 'recentupcoming')
        if change == 'stale': h.now += timedelta(minutes=31)
        elif change == 'wrong_user':
            h.documents.data['cp_moodle_accounts/' + feed.owner_id]['url'] = url(43)
        elif change == 'foreign_owner':
            h.documents.data['cp_moodle_accounts/' + feed.owner_id]['owner_id'] = 'other-owner'
        with h.request('mcp') as context:
            with pytest.raises((IncompleteRead, BindingConflict, PermissionError)):
                await reader.read_assignments(context, {'horizon': interval(hours=72)})
    asyncio.run(scenario())
