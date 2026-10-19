"""Moodle planning uses durable student input and cached LMS data, never HTTP."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from campuspulse_recovery.domain.models import Interval, SolveRepairInput
from campuspulse_recovery.domain.ports import BindingConflict, IncompleteRead
from campuspulse_recovery.domain.solver import solve_repair
from campuspulse_recovery.security.context import AccessDenied
from campuspulse_recovery.security.resources import ResourcePolicy
from campuspulse_recovery.storage.plans import _encode
from campuspulse_recovery.storage.snapshots import FirestoreSnapshotStore
from campuspulse_recovery.lms.models import MoodleCourse, MoodleSnapshot, MoodleTask
from campuspulse_recovery.lms.sync import MoodleSync
from tests.storage.test_plans import Harness, NOW, interval, sources


SITE = 'https://lms.astanait.edu.kz'


def lms_snapshot(*, deadline=NOW + timedelta(hours=6), status='todo', grade=0):
    return MoodleSnapshot(site_url=SITE, user_id=42, fetched_at=NOW,
        courses=(MoodleCourse(id=3, title='Math'),),
        tasks=(MoodleTask(id='assign:9', course_id=3, course_title='Math',
            title='Exam preparation', due_at=deadline, submission_status=status,
            grade_value=grade, url=SITE + '/mod/assign/view.php?id=91', kind='assignment'),))


async def setup(snapshot=None, *, documents=None, cfg=None):
    from campuspulse_recovery.lms.planning import MoodlePlanningStore, MoodleAssignmentsReader, moodle_resource_id
    h = Harness(documents=documents, cfg=cfg)
    h.policy = ResourcePolicy(h.config, environment='local',
        assignments_resource_id=moodle_resource_id(SITE, 42))
    h.snapshots = FirestoreSnapshotStore(h.documents, h.policy)
    h.clock = NOW
    async def fetch():
        return snapshot or lms_snapshot()
    connection = MoodleSync(h.documents, owner_id=h.config.student.trusted_user_id,
        site_url=SITE, expected_user_id=42, fetch_snapshot=fetch, clock=lambda: h.clock)
    await connection.refresh()
    inputs = MoodlePlanningStore(h.documents, h.policy, connection=connection, clock=lambda: h.clock)
    reader = MoodleAssignmentsReader(SimpleNamespace(policy=h.policy), h.snapshots,
        inputs=inputs, clock=lambda: h.clock)
    return h, inputs, reader


def update(**changes):
    from campuspulse_recovery.lms.planning import PlanningUpdate
    return PlanningUpdate.model_validate({
        'expected_revision': 0,
        'working_windows': [interval(NOW + timedelta(hours=1), hours=4).model_dump()],
        'estimates': [{'task_id': 'assign:9', 'remaining_minutes': 90}], **changes})


def test_input_is_durable_and_keeps_unknown_distinct_from_zero():
    async def scenario():
        from campuspulse_recovery.lms.planning import MoodlePlanningStore, PlanningUpdate
        h, inputs, _ = await setup()
        with h.request() as ctx:
            empty = await inputs.get(ctx)
            assert empty.revision == 0 and empty.working_windows is None and not empty.estimates
            saved = await inputs.update(ctx, update())
            assert saved.revision == 1 and saved.updated_at == NOW
            reopened = MoodlePlanningStore(h.documents, h.policy, connection=inputs.connection, clock=lambda: h.clock)
            assert await reopened.get(ctx) == saved
            done = await reopened.update(ctx, PlanningUpdate(expected_revision=1,
                estimates=({'task_id': 'assign:9', 'remaining_minutes': 0},)))
            assert done.estimates[0].remaining_minutes == 0
            assert done.working_windows == saved.working_windows
            unknown = await reopened.update(ctx, PlanningUpdate(expected_revision=2,
                estimates=({'task_id': 'assign:9', 'remaining_minutes': None},)))
            assert unknown.estimates[0].remaining_minutes is None
    asyncio.run(scenario())


def test_concurrent_input_updates_have_one_winner_and_no_lost_edit():
    async def scenario():
        h, inputs, _ = await setup()
        with h.request() as ctx:
            results = await asyncio.gather(inputs.update(ctx, update()),
                inputs.update(ctx, update(estimates=[{'task_id': 'assign:9', 'remaining_minutes': 30}])),
                return_exceptions=True)
            assert sum(isinstance(r, BindingConflict) for r in results) == 1
            saved = await inputs.get(ctx)
            assert saved.revision == 1 and saved.estimates[0].remaining_minutes in (30, 90)
            with pytest.raises(BindingConflict, match='planning_revision_conflict'):
                await inputs.update(ctx, update())
            assert await inputs.get(ctx) == saved
    asyncio.run(scenario())


@pytest.mark.parametrize('boundary', ['agent', 'mcp'])
def test_only_authenticated_backend_can_change_student_input(boundary):
    async def scenario():
        h, inputs, _ = await setup()
        with h.request(boundary) as ctx:
            with pytest.raises(AccessDenied):
                await inputs.update(ctx, update())
            assert (await inputs.get(ctx)).revision == 0
        with h.request() as ctx:
            with pytest.raises(AccessDenied):
                await inputs.update(ctx.as_port_context(), update())
        with pytest.raises(AccessDenied):
            await inputs.get(ctx)
    asyncio.run(scenario())


def test_unknown_task_and_stored_owner_substitution_are_rejected():
    async def scenario():
        h, inputs, _ = await setup()
        with h.request() as ctx:
            with pytest.raises(BindingConflict, match='moodle_task_not_found'):
                await inputs.update(ctx, update(estimates=[{'task_id': 'assign:99', 'remaining_minutes': 30}]))
            await inputs.update(ctx, update())
            from campuspulse_recovery.lms.planning import PlanningRecord
            record = PlanningRecord(owner_id='other-student', resource_id=inputs.resource_id,
                state=await inputs.get(ctx))
            h.documents.data[inputs.path] = _encode('moodle_planning', record)
            with pytest.raises(AccessDenied):
                await inputs.get(ctx)
    asyncio.run(scenario())


@pytest.mark.parametrize('changes', [
    {'expected_revision': True},
    {'estimates': [{'task_id': 'assign:9', 'remaining_minutes': True}]},
    {'estimates': [{'task_id': 'assign:9', 'remaining_minutes': -1}]},
    {'estimates': [{'task_id': 'assign:9', 'remaining_minutes': 1.5}]},
    {'estimates': [{'task_id': 'assign:9', 'remaining_minutes': 100001}]},
    {'estimates': [{'task_id': 'assign:9', 'remaining_minutes': 1}] * 2},
    {'working_windows': [interval().model_dump()] * 2},
    {'working_windows': [interval(NOW + timedelta(minutes=1)).model_dump()]},
    {'owner_id': 'foreign'}, {'site_url': 'https://foreign.test'},
    {'revision': 999}, {'updated_at': NOW},
])
def test_invalid_or_forged_input_rejected(changes):
    with pytest.raises(ValidationError):
        update(**changes)


def test_projected_moodle_snapshot_drives_real_solver_without_sheet_calls():
    async def scenario():
        h, inputs, reader = await setup()
        with h.request() as ctx:
            state = await inputs.update(ctx, update())
        with h.request('mcp') as ctx:
            assignments = await reader.read(ctx, interval(hours=72))
            assert assignments.assignments[0].remaining_minutes == 90
            assert assignments.assignments[0].deadline.at == NOW + timedelta(hours=6)
            assert assignments.source.reference.resource_id.startswith('moodle:')
            assert assignments.source.reference.resource_id != h.config.student.spreadsheet_id
            assert await h.snapshots.get_assignments(ctx, assignments.source.reference) == assignments
            _, calendar = sources(h.config)
            await h.snapshots.put_calendar(ctx, calendar)
            constraints = inputs.constraints(state, now=NOW)
            result = solve_repair(SolveRepairInput(assignments=assignments, calendar=calendar,
                constraints=constraints, now=NOW, plan_id='moodle-plan', version=1))
            assert result.status == 'feasible'
            assert sum(block.interval.minutes for block in result.blocks) == 90
            assert all(block.interval.start >= NOW + timedelta(hours=1) for block in result.blocks)
            # An existing Sheet snapshot is not accepted as this Moodle source.
            old, _ = sources(h.config)
            with pytest.raises(AccessDenied):
                h.policy.require_source(ctx, old.source.reference)
    asyncio.run(scenario())


def test_missing_estimate_or_hours_produces_needs_input_instead_of_invented_work():
    async def scenario():
        h, inputs, reader = await setup()
        with h.request('mcp') as ctx:
            assignments = await reader.read(ctx, interval(hours=72))
            _, calendar = sources(h.config)
            state = await inputs.get(ctx)
            result = solve_repair(SolveRepairInput(assignments=assignments, calendar=calendar,
                constraints=inputs.constraints(state, now=NOW), now=NOW, plan_id='moodle-plan', version=1))
            assert result.status == 'needs_input'
            assert {f.code for f in result.findings} >= {'needs_input_working_windows', 'needs_input_remaining_minutes'}
    asyncio.run(scenario())


@pytest.mark.parametrize('status,minutes,count', [('submitted', 60, 1), ('graded', 60, 1),
    ('todo', 0, 0), ('unknown', None, 1), ('todo', 60, 1), ('graded', None, 1)])
def test_completion_is_based_on_lms_status_or_explicit_zero_not_a_grade(status, minutes, count):
    async def scenario():
        h, inputs, reader = await setup(lms_snapshot(status=status, grade=0))
        with h.request() as ctx:
            await inputs.update(ctx, update(estimates=[{'task_id': 'assign:9', 'remaining_minutes': minutes}]))
        with h.request('mcp') as ctx:
            result = await reader.read(ctx, interval(hours=72))
            assert len(result.assignments) == count
            if count:
                assert result.assignments[0].remaining_minutes == minutes
    asyncio.run(scenario())


@pytest.mark.parametrize('status,count', [('submitted', 0), ('graded', 1), ('unknown', 1)])
def test_only_unoverridden_submission_can_imply_no_remaining_work(status, count):
    async def scenario():
        h, _, reader = await setup(lms_snapshot(status=status, grade=0))
        with h.request('mcp') as ctx:
            result = await reader.read(ctx, interval(hours=72))
            assert len(result.assignments) == count
            if count:
                assert result.assignments[0].remaining_minutes is None
    asyncio.run(scenario())


def test_actual_moodle_client_grade_zero_requires_effort_or_explicit_rework():
    from campuspulse_recovery.lms.planning import PlanningState, project_assignments, moodle_resource_id
    from tests.lms.test_api_audit import _payloads, _fetch
    payloads = _payloads()
    payloads['mod_assign_get_submission_status']['lastattempt']['submission']['status'] = 'new'
    grade = payloads['gradereport_user_get_grade_items']['usergrades'][0]['gradeitems'][0]
    grade['graderaw'], grade['gradeformatted'] = 0, '0.00'
    snapshot = _fetch(payloads)
    assert snapshot.tasks[0].submission_status == 'graded'
    resource = moodle_resource_id(snapshot.site_url, snapshot.user_id)
    unknown = project_assignments(snapshot, PlanningState(), resource_id=resource, timezone='Asia/Qyzylorda')
    assert len(unknown) == 1 and unknown[0].remaining_minutes is None
    state = PlanningState(estimates=({'task_id': 'assign:1', 'remaining_minutes': 60},))
    work = project_assignments(snapshot, state, resource_id=resource, timezone='Asia/Qyzylorda')
    assert len(work) == 1 and work[0].remaining_minutes == 60


@pytest.mark.parametrize('cause', ['stale', 'disconnected', 'sync_failed', 'future_cache'])
def test_uncertain_moodle_cache_never_becomes_empty_available_work(cause):
    async def scenario():
        from campuspulse_recovery.lms.sync import _Record
        from campuspulse_recovery.storage.plans import _decode
        h, inputs, reader = await setup()
        if cause == 'stale':
            h.clock += timedelta(minutes=31)
        elif cause == 'disconnected':
            await inputs.connection.disconnect()
        else:
            record = _decode(h.documents.data[inputs.connection.path], 'moodle_connection', _Record)
            changes = {'error': 'unavailable'} if cause == 'sync_failed' else {
                'snapshot': record.snapshot.model_copy(update={'fetched_at': NOW + timedelta(minutes=10)})}
            h.documents.data[inputs.connection.path] = _encode('moodle_connection', record.model_copy(update=changes))
        before = set(h.documents.data)
        with h.request('mcp') as ctx, pytest.raises(IncompleteRead):
            await reader.read(ctx, interval(hours=72))
        assert set(h.documents.data) == before
    asyncio.run(scenario())


def test_emulator_persists_planning_revision_and_moodle_snapshot():
    import os
    import re
    from uuid import uuid4
    host = os.environ.get('FIRESTORE_EMULATOR_HOST')
    if not host:
        pytest.skip('Explicit loopback Firestore emulator required')
    assert re.fullmatch(r'(?:127\.0\.0\.1|localhost):[0-9]{1,5}', host)
    from google.auth.credentials import AnonymousCredentials
    from google.cloud.firestore_v1 import AsyncClient
    from campuspulse_recovery.config import AppConfig
    from campuspulse_recovery.storage.plans import FirestoreDocuments
    from tests.storage.test_plans import config

    async def scenario():
        data = config().model_dump()
        project = 'demo-campuspulse-tests'
        data['project_id'] = project
        data['student']['trusted_user_id'] = 'moodle-' + uuid4().hex
        data['identities'] = {key: value.split('@')[0] + '@' + project + '.iam.gserviceaccount.com'
                              for key, value in data['identities'].items()}
        cfg = AppConfig.model_validate(data)
        client = AsyncClient(project=project, credentials=AnonymousCredentials())
        try:
            documents = FirestoreDocuments(client, project_id=project, database_id='(default)')
            h, inputs, reader = await setup(documents=documents, cfg=cfg)
            with h.request() as ctx:
                saved = await inputs.update(ctx, update())
                assert (await inputs.get(ctx)) == saved
                with pytest.raises(BindingConflict, match='planning_revision_conflict'):
                    await inputs.update(ctx, update())
            with h.request('mcp') as ctx:
                snapshot = await reader.read(ctx, interval(hours=72))
                assert snapshot.assignments[0].remaining_minutes == 90
                assert await h.snapshots.get_assignments(ctx, snapshot.source.reference) == snapshot
        finally:
            client.close()
    asyncio.run(scenario())
