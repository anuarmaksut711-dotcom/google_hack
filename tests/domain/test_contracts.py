"""Pure shared-contract/time checks. No LLM, SDK clients, network or cloud I/O."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from campuspulse_recovery.domain.models import (
    TOOL_CONTRACTS, ApplyPlanInput, ApplyResult, Approval, Assignment,
    AssignmentsSnapshot, BlockTarget, CalendarEvent, CalendarSnapshot, Deadline,
    ExecutionState, ExecutionStatus, Finding, Interval, Operation, OperationState,
    OperationStatus, OwnedBlock, OwnedBlockRecord, OwnershipMarker, PlanPayload, PlanReference,
    PlanVersion, PlanningConstraints, ReadCalendarInput, RecurrenceInstance,
    RepairResult, RepairStatus, RunEvent, SearchEvidence, SolveRepairInput,
    SourceIssue, SourceKind, SourceReference, SourceSnapshot, VerificationResult,
    VerifyPlanInput, WorkAllocation, assignments_content_hash, calendar_content_hash,
    calendar_event_id, canonical_hash, canonical_json, new_block_id, operation_id,
    validate_execution_transition, validate_operation_transition,
)
from campuspulse_recovery.domain.ports import CalendarLease, TrustedContext
from campuspulse_recovery.domain.time_rules import (
    all_day_bounds, block_phase, clip, date_deadline, iana_zone, instant, iso_date,
    local_time, overlaps, planning_horizon, required_minutes,
)

NOW = instant("2026-10-17T10:00:00Z")
ZONE = "Asia/Qyzylorda"


def interval(start=NOW, minutes=60):
    return Interval(start=start, end=start + timedelta(minutes=minutes))


def constraints(**overrides):
    args = dict(config_revision="config-1", timezone=ZONE,
                horizon=interval(minutes=72 * 60), working_windows=(interval(minutes=120),))
    return PlanningConstraints(**(args | overrides))


def source(kind, digest, *, owner="student-1", calendar="calendar-1", complete=True,
           issues=(), read_at=NOW, run="run-1", timezone=ZONE):
    return SourceSnapshot(
        reference=SourceReference(snapshot_id=f"snapshot-{kind}", kind=kind,
            owner_id=owner, resource_id=calendar if kind == "calendar" else "sheet-1",
            run_id=run, content_hash=digest, read_at=read_at, config_revision="config-1"),
        timezone=timezone, complete=complete, issues=issues,
        covered_interval=interval(minutes=72 * 60) if kind == "calendar" else None,
    )


def assignment(**overrides):
    args = dict(assignment_id="assignment-1", title="Untrusted cell text", source_row="row-2",
                deadline=Deadline(at=NOW + timedelta(hours=3), timezone=ZONE), remaining_minutes=60)
    return Assignment(**(args | overrides))


def assignment_snapshot(items=None, **source_args):
    items = (assignment(),) if items is None else items
    return AssignmentsSnapshot(source=source("assignments", assignments_content_hash(items), **source_args), assignments=items)


def calendar_snapshot(events=(), owned=(), expanded=True, **source_args):
    return CalendarSnapshot(
        source=source("calendar", calendar_content_hash(events, owned, expanded), **source_args),
        events=events, owned_blocks=owned, recurrence_expanded=expanded,
    )


def plan(*, owner="student-1", calendar="calendar-1", version=1, kind="create", start=NOW):
    target = BlockTarget(block_id="block-1", assignment_id="assignment-1", interval=interval(start))
    op = Operation(operation_id=operation_id("plan-1", version, 0), kind=kind,
                   event_id=calendar_event_id(owner, calendar, target.block_id), target=target,
                   expected_etag='"etag-old"' if kind == "update" else None,
                   before=interval(start + timedelta(hours=2)) if kind == "update" else None)
    sources = (assignment_snapshot(owner=owner).source.reference,
               calendar_snapshot(owner=owner, calendar=calendar).source.reference)
    payload = PlanPayload(owner_id=owner, calendar_id=calendar, plan_id="plan-1", version=version,
                          created_at=NOW, source_references=sources, constraints=constraints(),
                          operations=(op,), blocks=(target,), allocations=(allocation(),))
    return PlanVersion.seal(payload)


def allocation():
    return WorkAllocation(assignment_id="assignment-1", remaining_minutes=60,
                          required_minutes=60, padding_minutes=0, scheduled_minutes=60)


def ref():
    value = plan()
    return PlanReference(plan_id=value.payload.plan_id, version=value.payload.version, payload_hash=value.payload_hash)


def observed():
    return CalendarEvent(event_id=calendar_event_id("student-1", "calendar-1", "block-1"),
                         etag='"etag-new"', interval=interval(), event_link="https://calendar.google.com/event?eid=demo",
                         campuspulse_marker=OwnershipMarker(owner_id="student-1", calendar_id="calendar-1",
                             block_id="block-1", assignment_id="assignment-1", create_operation_id=operation_id("plan-1", 1, 0)))


def op_state(status=OperationStatus.COMPLETED, **overrides):
    args = dict(operation_id=operation_id("plan-1", 1, 0), status=status, attempts=1,
                updated_at=NOW, observed_event=observed() if status == OperationStatus.COMPLETED else None)
    return OperationState(**(args | overrides))


def execution(status=ExecutionStatus.APPLIED_UNVERIFIED, operations=None):
    return ExecutionState(owner_id="student-1", calendar_id="calendar-1", plan=ref(),
        request_key="request-1", run_id="run-1", status=status,
        operations=(op_state(),) if operations is None else operations, updated_at=NOW)


@pytest.mark.parametrize("value", ["2026-10-17T15:00:00+05:00", "2026-10-17T10:00:00Z", NOW])
def test_aware_instants_normalize_to_utc(value):
    assert instant(value) == NOW
    assert instant(value).tzinfo == UTC


@pytest.mark.parametrize("value", [datetime(2026, 10, 17), "2026-10-17T10:00:00", "17/10/26", 0, True, "2026-10-17", "2026-10-17T10:00:00-00:00extra", "2026-10-17T10:00:00-00:00"])
def test_invalid_time_is_rejected(value):
    with pytest.raises((ValueError, ValidationError)):
        Interval(start=value, end=NOW + timedelta(hours=1))


@pytest.mark.parametrize("offset", ["+05:60", "+00:99", "-00:60", "+24:00", "-24:00", "+99:59"])
def test_malformed_rfc3339_offsets_fail_in_helper_and_dtos(offset):
    value = "2026-10-17T10:00:00" + offset
    with pytest.raises(ValueError, match="RFC3339"):
        instant(value)
    with pytest.raises(ValidationError) as interval_error:
        Interval(start=value, end=NOW + timedelta(hours=1))
    assert interval_error.value.errors()[0]["loc"] == ("start",)
    with pytest.raises(ValidationError) as deadline_error:
        Deadline(at=value, timezone=ZONE)
    assert deadline_error.value.errors()[0]["loc"] == ("at",)


@pytest.mark.parametrize("value, expected", [
    ("2026-10-17T10:00:00+23:59", "2026-10-16T10:01:00Z"),
    ("2026-10-17T10:00:00-23:59", "2026-10-18T09:59:00Z"),
    ("2026-10-17T10:00:00+00:59", "2026-10-17T09:01:00Z"),
    ("2026-10-17T10:00:00-00:59", "2026-10-17T10:59:00Z"),
])
def test_valid_offset_component_boundaries(value, expected):
    assert instant(value) == instant(expected)


@pytest.mark.parametrize("value", [
    "0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00",
    datetime.min.replace(tzinfo=ZoneInfo("Etc/GMT-1")),
    datetime.max.replace(tzinfo=ZoneInfo("Etc/GMT+1")),
])
def test_out_of_range_instants_become_value_or_structured_validation_error(value):
    with pytest.raises(ValueError, match="supported range"):
        instant(value)
    for model, fields in ((Interval, dict(start=value, end=NOW)), (Deadline, dict(at=value, timezone=ZONE))):
        with pytest.raises(ValidationError) as error:
            model(**fields)
        assert error.value.errors()[0]["type"] == "value_error"


@pytest.mark.parametrize("helper", [
    pytest.param(lambda: date_deadline("9999-12-31", "UTC"), id="next-midnight-overflow"),
    pytest.param(lambda: planning_horizon("9999-12-31T23:59:59Z"), id="grid-ceil-overflow"),
    pytest.param(lambda: planning_horizon("9999-12-30T00:00:00Z"), id="72-hour-overflow"),
    pytest.param(lambda: local_time(datetime.min, "Etc/GMT-1"), id="local-underflow"),
    pytest.param(lambda: local_time(datetime.max, "Etc/GMT+1"), id="local-overflow"),
    pytest.param(lambda: all_day_bounds("0001-01-01", "0001-01-02", "Etc/GMT-1"), id="all-day-underflow"),
])
def test_calendar_arithmetic_out_of_range_helpers_fail_closed(helper):
    with pytest.raises(ValueError, match="supported range"):
        helper()


def test_calendar_arithmetic_overflows_are_structured_dto_errors():
    with pytest.raises(ValidationError, match="supported range"):
        Deadline(at="9999-12-31T00:00:00Z", timezone="UTC", precision="date", source_date="9999-12-31")
    event = CalendarEvent(event_id="boundary-event", interval=Interval(
        start="0001-01-01T00:00:00Z", end="0001-01-02T00:00:00Z"),
        all_day_start="0001-01-01", all_day_end="0001-01-02")
    with pytest.raises(ValidationError, match="supported range"):
        calendar_snapshot(events=(event,), timezone="Etc/GMT-1")
    with pytest.raises(ValidationError, match="supported range"):
        SolveRepairInput(assignments=assignment_snapshot(), calendar=calendar_snapshot(),
            constraints=constraints(), now="9999-12-31T23:59:59Z", plan_id="plan-1", version=1)


def test_representable_datetime_boundaries_remain_valid():
    assert instant("0001-01-01T00:00:00Z") == datetime.min.replace(tzinfo=UTC)
    assert instant("9999-12-31T23:59:59.999999Z") == datetime.max.replace(tzinfo=UTC)
    assert date_deadline("9999-12-30", "UTC") == instant("9999-12-31T00:00:00Z")
    assert planning_horizon("9999-12-28T00:00:00Z")[1] == instant("9999-12-31T00:00:00Z")
    assert all_day_bounds("0001-01-01", "0001-01-02", "UTC")[0] == datetime.min.replace(tzinfo=UTC)


def test_invalid_zone_is_not_defaulted():
    for value in ("", "Mars/Student", " Asia/Qyzylorda", "+05:00"):
        with pytest.raises(ValueError):
            iana_zone(value)


def test_dst_ambiguous_and_nonexistent_local_time_require_input():
    for value in (datetime(2026, 3, 8, 2, 30), datetime(2026, 11, 1, 1, 30)):
        with pytest.raises(ValueError, match="ambiguous or nonexistent"):
            local_time(value, "America/New_York")
    assert local_time(datetime(2026, 3, 8, 3, 30), "America/New_York") == instant("2026-03-08T07:30:00Z")
    with pytest.raises(ValueError, match="nonexistent"):
        instant(datetime(2026, 3, 8, 2, 30, tzinfo=ZoneInfo("America/New_York")))
    assert instant("2026-11-01T01:30:00-04:00") != instant("2026-11-01T01:30:00-05:00")


def test_date_deadline_is_next_local_midnight_exclusive_boundary():
    boundary = date_deadline("2026-10-20", ZONE)
    assert boundary == instant("2026-10-20T19:00:00Z")
    assert Deadline(at=boundary, timezone=ZONE, precision="date", source_date="2026-10-20").at == boundary
    with pytest.raises(ValidationError):
        Deadline(at="2026-10-20T23:59:59+05:00", timezone=ZONE, precision="date", source_date="2026-10-20")


@pytest.mark.parametrize("value", ["20.10.2026", "2026-02-30", datetime(2026, 10, 20), True])
def test_invalid_date_only_is_not_guessed(value):
    with pytest.raises(ValueError):
        iso_date(value)


def test_all_day_exclusive_end_and_dst_elapsed_duration():
    first, last = all_day_bounds("2026-03-08", "2026-03-09", "America/New_York")
    assert (last - first).total_seconds() == 23 * 3600
    assert all_day_bounds("2026-10-20", "2026-10-21", ZONE) == (
        instant("2026-10-19T19:00:00Z"), instant("2026-10-20T19:00:00Z"))
    with pytest.raises(ValueError):
        all_day_bounds("2026-10-20", "2026-10-20", ZONE)


def test_half_open_adjacency_is_not_overlap_and_clip_is_positive():
    left, right = interval(), interval(NOW + timedelta(hours=1))
    assert not overlaps(left.start, left.end, right.start, right.end)
    assert overlaps(left.start, left.end, NOW + timedelta(minutes=59), right.end)
    assert clip(left.start, left.end, right.start, right.end) is None
    assert clip(left.start, left.end, NOW + timedelta(minutes=30), right.end) == (NOW + timedelta(minutes=30), left.end)
    for args in ((NOW, NOW), (NOW + timedelta(minutes=1), NOW)):
        with pytest.raises(ValidationError):
            Interval(start=args[0], end=args[1])


def test_horizon_ceil_and_elapsed_hours_across_dst():
    assert planning_horizon(NOW)[0] == NOW
    assert planning_horizon(NOW + timedelta(microseconds=1))[0] == NOW + timedelta(minutes=15)
    first, last = planning_horizon("2026-03-07T10:01:00-05:00")
    assert first == instant("2026-03-07T15:15:00Z")
    assert last - first == timedelta(hours=72)


@pytest.mark.parametrize("value, expected", [(0, 0), (1, 15), (15, 15), (16, 30), (61, 75)])
def test_explicit_work_and_padding(value, expected):
    assert required_minutes(value) == expected
    assert assignment(remaining_minutes=value).required_minutes == expected


@pytest.mark.parametrize("value", [None, -1, True, 1.5, "30"])
def test_unknown_invalid_work_is_not_zero(value):
    with pytest.raises(ValueError):
        required_minutes(value)
    if value is None:
        item = assignment(remaining_minutes=None)
        with pytest.raises(ValueError):
            _ = item.required_minutes
    else:
        with pytest.raises(ValidationError):
            assignment(remaining_minutes=value)


def test_past_blocks_do_not_decrease_remaining_work():
    work = assignment(remaining_minutes=60)
    assert block_phase(NOW - timedelta(hours=2), NOW, NOW) == "past"
    assert block_phase(NOW, NOW + timedelta(hours=1), NOW) == "in_progress"
    assert block_phase(NOW + timedelta(seconds=1), NOW + timedelta(hours=1), NOW) == "future"
    assert work.remaining_minutes == work.required_minutes == 60


def test_unknown_work_and_windows_survive_solver_input_for_needs_input():
    data = SolveRepairInput(assignments=assignment_snapshot((assignment(remaining_minutes=None, deadline=None),)),
        calendar=calendar_snapshot(), constraints=constraints(working_windows=None), now=NOW, plan_id="plan-1", version=1)
    assert data.assignments.assignments[0].remaining_minutes is None
    assert data.assignments.assignments[0].deadline is None
    assert data.constraints.working_windows is None
    assert constraints(working_windows=()).working_windows == ()


@pytest.mark.parametrize("calendar_zone, expected_start, expected_end", [
    ("Asia/Almaty", "2026-10-16T19:00:00Z", "2026-10-17T19:00:00Z"),
    ("America/New_York", "2026-10-17T04:00:00Z", "2026-10-18T04:00:00Z"),
])
def test_solver_preserves_native_calendar_zone_distinct_from_student(calendar_zone, expected_start, expected_end):
    start, end = all_day_bounds("2026-10-17", "2026-10-18", calendar_zone)
    event = CalendarEvent(event_id="native-all-day", interval=Interval(start=start, end=end),
        all_day_start="2026-10-17", all_day_end="2026-10-18")
    data = SolveRepairInput(assignments=assignment_snapshot(),
        calendar=calendar_snapshot(events=(event,), timezone=calendar_zone),
        constraints=constraints(), now=NOW, plan_id="plan-1", version=1)
    assert data.constraints.timezone == data.assignments.source.timezone == ZONE
    assert data.calendar.source.timezone == calendar_zone != ZONE
    assert data.calendar.events[0].interval == Interval(start=expected_start, end=expected_end)


@pytest.mark.parametrize("student_zone, assignment_zone", [
    (ZONE, "America/New_York"), ("America/New_York", ZONE),
])
def test_solver_rejects_mismatched_student_and_assignment_zone(student_zone, assignment_zone):
    with pytest.raises(ValidationError, match="student zone differs"):
        SolveRepairInput(assignments=assignment_snapshot(timezone=assignment_zone),
            calendar=calendar_snapshot(timezone="America/New_York"),
            constraints=constraints(timezone=student_zone), now=NOW, plan_id="plan-1", version=1)


def test_calendar_all_day_bounds_cannot_be_replaced_by_student_zone():
    start, end = all_day_bounds("2026-10-17", "2026-10-18", ZONE)
    event = CalendarEvent(event_id="wrong-all-day-zone", interval=Interval(start=start, end=end),
        all_day_start="2026-10-17", all_day_end="2026-10-18")
    with pytest.raises(ValidationError, match="exclusive dates/calendar zone"):
        calendar_snapshot(events=(event,), timezone="America/New_York")


@pytest.mark.parametrize("binding", ["owner", "run", "config", "coverage"])
def test_distinct_calendar_zone_does_not_relax_source_bindings(binding):
    calendar = calendar_snapshot(timezone="America/New_York").model_dump(mode="json")
    if binding == "coverage":
        calendar["source"]["covered_interval"] = interval(minutes=60).model_dump(mode="json")
    else:
        field = {"owner": "owner_id", "run": "run_id", "config": "config_revision"}[binding]
        calendar["source"]["reference"][field] = "other-binding"
    with pytest.raises(ValidationError):
        SolveRepairInput(assignments=assignment_snapshot(), calendar=CalendarSnapshot.model_validate(calendar),
            constraints=constraints(), now=NOW, plan_id="plan-1", version=1)


@pytest.mark.parametrize("overrides", [dict(horizon=interval(minutes=60)), dict(working_windows=(interval(NOW - timedelta(minutes=15)),)), dict(working_windows=(interval(), interval(NOW + timedelta(minutes=30))))])
def test_invalid_planning_constraints(overrides):
    with pytest.raises(ValidationError):
        constraints(**overrides)


def test_sources_are_immutable_bound_and_content_hash_checked():
    data = assignment_snapshot()
    assert AssignmentsSnapshot.model_validate_json(data.model_dump_json()) == data
    with pytest.raises(ValidationError):
        AssignmentsSnapshot(source=data.source, assignments=(assignment(remaining_minutes=30),))
    with pytest.raises(ValidationError):
        data.assignments[0].remaining_minutes = 0
    with pytest.raises(ValidationError):
        SolveRepairInput(assignments=data, calendar=calendar_snapshot(owner="other"), constraints=constraints(), now=NOW, plan_id="plan-1", version=1)


def test_partial_read_is_not_complete_availability():
    issues = (SourceIssue(code="pagination_failed"),)
    data = calendar_snapshot(expanded=False, complete=False, issues=issues)
    assert not data.source.complete
    with pytest.raises(ValidationError):
        calendar_snapshot(expanded=False)
    with pytest.raises(ValidationError):
        calendar_snapshot(issues=issues)
    with pytest.raises(ValidationError):
        calendar_snapshot(complete=False)


def test_all_day_snapshot_requires_calendar_zone_boundaries():
    start, end = all_day_bounds(date(2026, 10, 17), date(2026, 10, 18), ZONE)
    event = CalendarEvent(event_id="external-event", interval=Interval(start=start, end=end), all_day_start="2026-10-17", all_day_end="2026-10-18")
    assert calendar_snapshot(events=(event,)).events[0].busy
    wrong = CalendarEvent(event_id="external-event", interval=interval(), all_day_start="2026-10-17", all_day_end="2026-10-18")
    with pytest.raises(ValidationError):
        calendar_snapshot(events=(wrong,))


def test_recurrence_original_identity_exceptions_and_busy_semantics():
    occurrence = RecurrenceInstance(series_event_id="series-1", original_start_time=NOW)
    moved = CalendarEvent(event_id="series-1_instance", interval=interval(NOW + timedelta(hours=1)), recurrence=occurrence)
    cancelled = CalendarEvent(event_id="series-1_exception", interval=None, status="cancelled", recurrence=occurrence)
    assert moved.busy and not cancelled.busy
    assert CalendarEvent(event_id="tentative", interval=interval(), status="tentative").busy
    assert not CalendarEvent(event_id="free", interval=interval(), transparency="transparent").busy
    assert calendar_snapshot(events=(moved, cancelled)).recurrence_expanded
    for fields in ({}, dict(original_start_time=NOW, original_start_date="2026-10-17")):
        with pytest.raises(ValidationError):
            RecurrenceInstance(series_event_id="series-1", **fields)
    with pytest.raises(ValidationError):
        CalendarEvent(event_id="bad", interval=None)


def test_owned_blocks_require_matching_observation_and_stable_identity():
    block = OwnedBlock(owner_id="student-1", calendar_id="calendar-1", block_id="block-1",
        assignment_id="assignment-1", event_id=observed().event_id, interval=interval(), etag='"etag-new"')
    assert calendar_snapshot(events=(observed(),), owned=(block,)).owned_blocks == (block,)
    with pytest.raises(ValidationError):
        calendar_snapshot(owned=(block,))
    with pytest.raises(ValidationError):
        OwnedBlock.model_validate(block.model_dump() | {"event_id": "abcde"})
    forged = CalendarEvent.model_validate(observed().model_dump() | {"campuspulse_marker": None})
    with pytest.raises(ValidationError):
        calendar_snapshot(events=(forged,), owned=(block,))
    # A marker on its own remains an external busy event; it does not add a registry record.
    assert calendar_snapshot(events=(observed(),)).owned_blocks == ()


def test_plan_hash_stable_under_json_round_trip_key_order_and_equivalent_offsets():
    value = plan()
    wire = value.model_dump(mode="json")
    assert PlanVersion.model_validate_json(value.model_dump_json()) == value
    reordered = dict(reversed(list(wire["payload"].items())))
    reordered["created_at"] = "2026-10-17T15:00:00+05:00"
    assert PlanVersion(payload=reordered, payload_hash=value.payload_hash) == value
    assert canonical_json({"z": 1, "a": 2}) == '{"a":2,"z":1}'
    assert canonical_hash(value.payload) == value.payload_hash


@pytest.mark.parametrize("other", [dict(owner="other-student"), dict(calendar="other-calendar"), dict(version=2), dict(kind="update"), dict(start=NOW + timedelta(minutes=15))])
def test_exact_plan_hash_changes_for_identity_version_or_operations(other):
    assert plan(**other).payload_hash != plan().payload_hash


def test_hash_includes_source_refs_constraints_and_exact_operations():
    value = plan()
    payload = value.payload.model_dump(mode="json")
    payload["source_references"][0]["content_hash"] = "a" * 64
    with pytest.raises(ValidationError, match="hash mismatch"):
        PlanVersion(payload=payload, payload_hash=value.payload_hash)
    payload = value.payload.model_dump(mode="json")
    payload["operations"][0]["operation_id"] = "spoof"
    with pytest.raises(ValidationError, match="operation identity"):
        PlanVersion.seal(PlanPayload.model_validate(payload))
    with pytest.raises(ValidationError):
        value.payload.operations = ()


def test_deterministic_ids_use_unambiguous_encoding_and_valid_calendar_alphabet():
    event_id = calendar_event_id("a", "b", "c")
    assert event_id == calendar_event_id("a", "b", "c")
    assert len(event_id) == 66 and set(event_id) <= set("0123456789abcdefghijklmnopqrstuv")
    assert calendar_event_id("a:b", "c", "d") != calendar_event_id("a", "b:c", "d")
    assert operation_id("plan-1", 1, 0) != operation_id("plan-1", 2, 0)
    assert operation_id("plan-1", 1, 0) != operation_id("plan-1", 1, 1)
    block = new_block_id("a", "b", "c", "allocation-1")
    assert block == new_block_id("a", "b", "c", "allocation-1")
    assert block != new_block_id("a", "b", "c", "allocation-2")


@pytest.mark.parametrize("case", ["overlap", "missing_work", "unknown_windows", "outside_windows", "in_progress_update"])
def test_immutable_plan_rejects_invalid_projected_schedule(case):
    payload = plan(kind="update").payload.model_dump(mode="json")
    if case == "overlap":
        payload["blocks"].append(payload["blocks"][0] | {"block_id": "block-2"})
    elif case == "missing_work":
        payload["allocations"] = []
    elif case == "unknown_windows":
        payload["constraints"]["working_windows"] = None
    elif case == "outside_windows":
        payload["constraints"]["working_windows"] = []
    else:
        payload["operations"][0]["before"] = interval(NOW - timedelta(minutes=15)).model_dump(mode="json")
    with pytest.raises(ValidationError):
        PlanPayload.model_validate(payload)


def test_conditional_operations_and_only_create_update():
    op = plan().payload.operations[0]
    for fields in (dict(kind="delete"), dict(kind="update"), dict(expected_etag='"unexpected"')):
        with pytest.raises(ValidationError):
            Operation.model_validate(op.model_dump() | fields)
    assert plan(kind="update").payload.operations[0].expected_etag
    with pytest.raises(ValidationError):
        BlockTarget(block_id="block-1", assignment_id="assignment-1", interval=interval(minutes=0))
    for duration in (5, 61, 90):
        with pytest.raises(ValidationError):
            BlockTarget(block_id="block-1", assignment_id="assignment-1", interval=interval(minutes=duration))


def test_approval_exact_version_backend_record_expiry_and_revocation():
    approval = Approval(owner_id="student-1", calendar_id="calendar-1", plan=ref(), approved_by="ui-principal",
        approved_at=NOW, expires_at=NOW + timedelta(minutes=15))
    assert approval.active_at(NOW)
    assert not approval.active_at(NOW + timedelta(minutes=15))
    assert not approval.active_at(NOW - timedelta(seconds=1))
    revoked = Approval.model_validate(approval.model_dump() | {"status": "revoked", "invalidated_at": NOW})
    assert not revoked.active_at(NOW)
    for fields in (dict(expires_at=NOW), dict(expires_at=NOW + timedelta(minutes=16)), dict(status="revoked")):
        with pytest.raises(ValidationError):
            Approval.model_validate(approval.model_dump() | fields)


def test_registry_reserve_confirm_is_not_external_marker_ownership():
    args = dict(owner_id="student-1", calendar_id="calendar-1", block_id="block-1", assignment_id="assignment-1",
        event_id=observed().event_id, intent_plan=ref(), intent_operation_id=operation_id("plan-1", 1, 0), reserved_at=NOW)
    reserved = OwnedBlockRecord(**args, state="reserved")
    confirmed = OwnedBlockRecord(**args, state="confirmed", confirmed_at=NOW, confirmed_etag='"etag-new"')
    assert reserved.state != confirmed.state
    for fields in (dict(state="confirmed"), dict(state="reserved", confirmed_at=NOW), dict(state="reserved", event_id="abcde")):
        with pytest.raises(ValidationError):
            OwnedBlockRecord(**(args | fields))


def test_operation_unknown_requires_reconciliation_completed_never_resets():
    validate_operation_transition(OperationStatus.PENDING, OperationStatus.IN_FLIGHT)
    validate_operation_transition(OperationStatus.IN_FLIGHT, OperationStatus.UNKNOWN)
    with pytest.raises(ValueError, match="reconciliation"):
        validate_operation_transition(OperationStatus.UNKNOWN, OperationStatus.PENDING)
    validate_operation_transition(OperationStatus.UNKNOWN, OperationStatus.COMPLETED, reconciled=True)
    validate_operation_transition(OperationStatus.COMPLETED, OperationStatus.COMPLETED)
    with pytest.raises(ValueError):
        validate_operation_transition(OperationStatus.COMPLETED, OperationStatus.PENDING)
    with pytest.raises(ValidationError):
        op_state(observed_event=None)


def test_execution_transitions_and_partial_preserve_completed_operations():
    validate_execution_transition(ExecutionStatus.APPROVED, ExecutionStatus.APPLYING)
    validate_execution_transition(ExecutionStatus.APPLYING, ExecutionStatus.PARTIAL)
    validate_execution_transition(ExecutionStatus.PARTIAL, ExecutionStatus.APPLYING)
    for target in (ExecutionStatus.VERIFIED, ExecutionStatus.PROPOSED):
        with pytest.raises(ValueError):
            validate_execution_transition(ExecutionStatus.PARTIAL, target)
    unfinished = op_state(OperationStatus.UNKNOWN, operation_id=operation_id("plan-1", 1, 1))
    partial = execution(ExecutionStatus.PARTIAL, operations=(op_state(), unfinished))
    assert partial.operations[0].status == OperationStatus.COMPLETED
    for status in (ExecutionStatus.APPLIED_UNVERIFIED, ExecutionStatus.VERIFIED, ExecutionStatus.REJECTED, ExecutionStatus.FAILED, ExecutionStatus.UNKNOWN):
        with pytest.raises(ValidationError):
            execution(status, operations=partial.operations)


def test_verified_requires_fresh_complete_actual_read_and_matching_plan():
    args = dict(plan=ref(), run_id="run-1", status="verified", attempted_at=NOW,
                verification_at=NOW + timedelta(seconds=2),
                calendar_source=calendar_snapshot(read_at=NOW + timedelta(seconds=1)).source,
                observed_events=(observed(),), checked_operations=(operation_id("plan-1", 1, 0),))
    verified = VerificationResult(**args)
    assert ApplyResult(execution=execution(ExecutionStatus.VERIFIED), verification=verified).verification.verification_at
    with pytest.raises(ValidationError):
        ApplyResult(execution=execution(ExecutionStatus.VERIFIED))
    for fields in (dict(calendar_source=None), dict(verification_at=None),
                   dict(calendar_source=calendar_snapshot(read_at=NOW - timedelta(seconds=1)).source),
                   dict(calendar_source=calendar_snapshot(complete=False, issues=(SourceIssue(code="read_failed"),)).source),
                   dict(findings=(Finding(code="new_conflict"),)), dict(run_id="other-run")):
        with pytest.raises(ValidationError):
            VerificationResult(**(args | fields))


def test_failed_verification_and_unavailable_read_never_report_success():
    failed = VerificationResult(plan=ref(), run_id="run-1", status="verification_failed", attempted_at=NOW,
        verification_at=NOW, calendar_source=calendar_snapshot().source, findings=(Finding(code="target_mismatch"),))
    assert failed.status == "verification_failed"
    unavailable = VerificationResult(plan=ref(), run_id="run-1", status="applied_unverified", attempted_at=NOW,
        findings=(Finding(code="calendar_read_unavailable"),))
    assert unavailable.verification_at is None
    with pytest.raises(ValidationError):
        VerificationResult(plan=ref(), run_id="run-1", status="verification_failed", attempted_at=NOW)


def test_planning_status_claims_require_proof_and_no_change_has_empty_diff():
    args = dict(constraints=constraints(), source_references=plan().payload.source_references,
                search=SearchEvidence(explored_nodes=1, node_budget=100_000))
    assert RepairResult(status="feasible", **args).status == RepairStatus.FEASIBLE
    assert RepairResult(status="no_change", **args).operations == ()
    assert RepairResult(status="unknown", **args).status == RepairStatus.UNKNOWN
    for status in ("optimal", "infeasible"):
        with pytest.raises(ValidationError, match="proof"):
            RepairResult(status=status, **args)
    for status in ("unknown", "needs_input", "needs_review", "no_change"):
        with pytest.raises(ValidationError):
            RepairResult(status=status, operations=plan().payload.operations, **args)
    proof = SearchEvidence(explored_nodes=20, node_budget=100_000, exhausted=True, proof="exhaustive_search")
    assert RepairResult(status="optimal", **(args | {"search": proof})).status == RepairStatus.OPTIMAL
    with pytest.raises(ValidationError):
        SearchEvidence(explored_nodes=20, node_budget=10)


def test_capacity_certificate_is_deadline_specific_and_separate_from_budget_exhaustion():
    finding = Finding(code="deadline_capacity_deficit", deadline=NOW + timedelta(hours=1), deficit_minutes=15)
    proof = SearchEvidence(explored_nodes=1, node_budget=100_000,
                           proof="capacity_certificate", certificate=(finding,))
    result = RepairResult(status="infeasible", constraints=constraints(),
        source_references=plan().payload.source_references, findings=(finding,), search=proof)
    assert result.status == RepairStatus.INFEASIBLE
    for invalid in (Finding(code="unproven"), Finding(code="deficit", deadline=NOW, deficit_minutes=0)):
        with pytest.raises(ValidationError):
            SearchEvidence(explored_nodes=1, node_budget=100_000, proof="capacity_certificate", certificate=(invalid,))
    limited = SearchEvidence(explored_nodes=100_000, node_budget=100_000)
    assert not limited.exhausted and limited.proof == "none"


def test_public_tool_schema_surface_excludes_model_authorization_fields():
    assert set(TOOL_CONTRACTS) == {"read_assignments", "read_calendar", "solve_repair", "apply_plan", "verify_plan"}
    forbidden = {"owner_id", "calendar_id", "sheet_id", "approved", "approval", "principal_id", "operations", "session_id"}
    for name, (input_type, output_type) in TOOL_CONTRACTS.items():
        input_schema, output_schema = input_type.model_json_schema(), output_type.model_json_schema()
        assert input_schema["additionalProperties"] is False
        assert output_schema["additionalProperties"] is False
        assert not forbidden.intersection(input_schema["properties"]), name
    for field in forbidden:
        with pytest.raises(ValidationError):
            ApplyPlanInput(plan_id="plan-1", version=1, request_key="request-1", **{field: "spoof"})
    assert set(ApplyPlanInput.model_fields) == {"schema_version", "plan_id", "version", "request_key"}
    assert set(VerifyPlanInput.model_fields) == {"schema_version", "plan_id", "version"}


def test_versioning_strict_numbers_bounds_and_untrusted_trace_payload():
    for fields in (dict(version=True), dict(version=0), dict(schema_version="2.0")):
        with pytest.raises(ValidationError):
            ApplyPlanInput.model_validate(dict(plan_id="plan-1", version=1, request_key="request-1") | fields)
    with pytest.raises(ValidationError):
        ReadCalendarInput(horizon=interval(minutes=72 * 60 + 1))
    with pytest.raises(ValidationError):
        RunEvent(event_id="trace-1", run_id="run-1", sequence=0, occurred_at=NOW,
            tool="apply_plan", phase="result", status="unknown", raw_payload={"Authorization": "secret"})


def test_ports_require_explicit_context_and_valid_lease():
    context = TrustedContext(owner_id="student-1", principal_id="trusted-principal", project_id="explicit-project",
        region="explicit-region", sheet_id="sheet-1", sheet_range="'Fall Tasks'!A:F", calendar_id="calendar-1",
        timezone=ZONE, config_revision="config-1", run_id="run-1")
    assert context.calendar_id == "calendar-1"
    with pytest.raises(ValidationError):
        CalendarLease(owner_id="student-1", calendar_id="calendar-1", holder_id="worker-1", generation=1,
                      acquired_at=NOW, expires_at=NOW, state="active")
