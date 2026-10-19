"""Student planning input and a domain projection of the durable Moodle cache.

No LMS, Google or model requests occur here. Explicit student effort overrides
submission/grade status; a grade alone never proves that work is finished.
"""
from __future__ import annotations

from typing import Annotated
from uuid import uuid4

from pydantic import Field, StrictInt, model_validator

from ..domain.models import (
    Assignment, AssignmentsSnapshot, Contract, Deadline, Instant, Interval,
    PlanningConstraints, ReadAssignmentsInput, SourceReference, SourceSnapshot,
    assignments_content_hash, canonical_hash,
)
from ..domain.ports import BindingConflict, IncompleteRead
from ..domain.time_rules import instant, planning_horizon
from ..security.context import AccessDenied
from ..integrations.assignments import AssignmentsReader
from ..storage.plans import _Repository, _decode, _encode, _key
from .models import normalize_site_url
from .sync import utcnow


class EffortEstimate(Contract):
    task_id: Annotated[str, Field(strict=True, pattern=r'^(assign|quiz):[1-9][0-9]*$')]
    remaining_minutes: Annotated[StrictInt, Field(ge=0, le=100_000)] | None


class PlanningValues(Contract):
    working_windows: Annotated[tuple[Interval, ...], Field(max_length=32)] | None = None
    estimates: Annotated[tuple[EffortEstimate, ...], Field(max_length=500)] = ()

    @model_validator(mode='after')
    def valid_input(self):
        if len({e.task_id for e in self.estimates}) != len(self.estimates):
            raise ValueError('duplicate_task_estimate')
        if self.working_windows is not None:
            windows = sorted(self.working_windows, key=lambda w: w.start)
            for window in windows:
                if any(p.minute % 15 or p.second or p.microsecond for p in (window.start, window.end)):
                    raise ValueError('quarter_hour_windows_required')
            if any(a.end > b.start for a, b in zip(windows, windows[1:])):
                raise ValueError('overlapping_working_windows')
        return self


class PlanningState(PlanningValues):
    revision: Annotated[StrictInt, Field(ge=0)] = 0
    updated_at: Instant | None = None


class PlanningUpdate(PlanningValues):
    # Partial updates preserve omitted windows and estimates for other tasks.
    # A bounded batch fits the existing 16 KiB authenticated JSON reader.
    expected_revision: Annotated[StrictInt, Field(ge=0)]
    estimates: Annotated[tuple[EffortEstimate, ...], Field(max_length=100)] = ()


class PlanningRecord(Contract):
    owner_id: str
    resource_id: str
    state: PlanningState


def moodle_resource_id(site_url, user_id):
    site_url = normalize_site_url(site_url)
    if type(user_id) is not int or user_id < 1:
        raise ValueError('explicit_moodle_user_required')
    return 'moodle:' + canonical_hash([site_url, user_id])


class MoodlePlanningStore(_Repository):
    def __init__(self, documents, policy, *, connection, clock=utcnow):
        super().__init__(documents, policy)
        if connection.documents is not documents or connection.owner_id != policy.config.student.trusted_user_id:
            raise AccessDenied('moodle_planning_binding_mismatch')
        self.connection, self.clock = connection, clock
        self.resource_id = moodle_resource_id(connection.site_url, connection.expected_user_id)
        self.path = 'cp_moodle_planning/' + _key(connection.owner_id, self.resource_id)

    def _load(self, raw):
        if raw is None:
            return PlanningState()
        record = _decode(raw, 'moodle_planning', PlanningRecord)
        if (record.owner_id, record.resource_id) != (self.connection.owner_id, self.resource_id):
            raise AccessDenied('moodle_planning_binding_mismatch')
        return record.state

    async def get(self, context, *, reader=None):
        context = self._request(context)
        state = self._load(await (reader or self.documents).get(self.path))
        self.policy.require_context(context)
        return state

    async def cached(self, context, *, reader=None):
        context = self._request(context)
        record = self.connection._load(await (reader or self.documents).get(self.connection.path))
        source = self.connection._state(record)
        self.policy.require_owner(context, source.owner_id)
        return source

    async def update(self, context, request):
        context = self._request(context)
        if context.boundary != 'backend':
            raise AccessDenied('planning_backend_required')
        request = PlanningUpdate.model_validate(request)
        source = await self.cached(context)
        self.policy.require_owner(context, source.owner_id)
        if not source.connected or source.snapshot is None:
            raise BindingConflict('moodle_snapshot_required')
        known = {task.id for task in source.snapshot.tasks}
        if any(e.task_id not in known for e in request.estimates):
            raise BindingConflict('moodle_task_not_found')

        async def save(tx):
            old = self._load(await tx.get(self.path))
            if old.revision != request.expected_revision:
                raise BindingConflict('planning_revision_conflict')
            estimates = {e.task_id: e for e in old.estimates if e.task_id in known}
            estimates.update({e.task_id: e for e in request.estimates})
            windows = request.working_windows if 'working_windows' in request.model_fields_set else old.working_windows
            state = PlanningState(revision=old.revision + 1, working_windows=windows,
                estimates=tuple(estimates[key] for key in sorted(estimates)), updated_at=self.clock())
            self.policy.require_context(context)
            tx.set(self.path, _encode('moodle_planning', PlanningRecord(
                owner_id=self.connection.owner_id, resource_id=self.resource_id, state=state)))
            return state
        return await self.documents.transact(save)

    def constraints(self, state, *, now):
        state = PlanningState.model_validate(state)
        start, end = planning_horizon(now)
        windows = None
        if state.working_windows is not None:
            windows = tuple(Interval(start=max(w.start, start), end=min(w.end, end))
                for w in sorted(state.working_windows, key=lambda w: w.start)
                if w.start < end and w.end > start)
        return PlanningConstraints(config_revision=self.policy.config.config_revision,
            timezone=self.policy.config.student.timezone, horizon=Interval(start=start, end=end),
            working_windows=windows)


def current_cache(cached, now):
    return bool(cached.connected and cached.snapshot is not None and not cached.stale
                and not cached.error and cached.snapshot.fetched_at <= instant(now))


def project_assignments(snapshot, state, *, resource_id, timezone):
    estimates = {e.task_id: e.remaining_minutes for e in state.estimates}
    tasks = []
    for task in sorted(snapshot.tasks, key=lambda t: t.id):
        remaining = estimates.get(task.id)
        # Moodle may report graded merely because a grade (including zero)
        # exists. Positive student effort also covers rework after submission.
        if remaining == 0 or task.submission_status == 'submitted' and task.id not in estimates:
            continue
        tasks.append(Assignment(assignment_id='moodle_' + canonical_hash([resource_id, task.id]),
            source_row=task.id, title=task.title,
            deadline=None if task.due_at is None else Deadline(at=task.due_at, timezone=timezone),
            remaining_minutes=remaining))
    return tuple(tasks)


def input_version(state):
    return 'moodle_' + canonical_hash(state) + '_'


class MoodleAssignmentsReader(AssignmentsReader):
    """The existing assignment-read port, backed by authenticated cached data."""
    def __init__(self, clients, snapshots, *, inputs, clock=utcnow, request_context=None):
        # The inherited read bridge needs these port attributes, but this source
        # does not parse or validate the unused Google Sheet range.
        self.clients, self.snapshots, self.clock = clients, snapshots, clock
        self._request_context = request_context
        self.policy = clients.policy
        if inputs.policy is not self.policy or snapshots.policy is not self.policy:
            raise AccessDenied('moodle_reader_authority_mismatch')
        self.clients, self.snapshots, self.inputs, self.clock = clients, snapshots, inputs, clock

    async def read_assignments(self, context, request):
        ReadAssignmentsInput.model_validate(request)
        trusted = self.policy.require_context(context)
        if context.boundary != 'mcp' or self.policy.assignments_resource_id != self.inputs.resource_id:
            raise AccessDenied('moodle_assignment_source_required')
        cached = await self.inputs.cached(context)
        now = instant(self.clock())
        if not current_cache(cached, now):
            raise IncompleteRead('moodle_snapshot_not_current')
        state = await self.inputs.get(context)
        assignments = project_assignments(cached.snapshot, state, resource_id=self.inputs.resource_id,
                                          timezone=trusted.timezone)
        source = SourceSnapshot(reference=SourceReference(snapshot_id='moodle_' + uuid4().hex,
            kind='assignments', owner_id=trusted.owner_id, resource_id=self.inputs.resource_id,
            run_id=trusted.run_id, content_hash=assignments_content_hash(assignments),
            read_at=now, config_revision=trusted.config_revision), timezone=trusted.timezone, complete=True,
            source_version=input_version(state) + canonical_hash(cached.snapshot))
        result = AssignmentsSnapshot(source=source, assignments=assignments)
        await self.snapshots.put_assignments(context, result)
        return result
