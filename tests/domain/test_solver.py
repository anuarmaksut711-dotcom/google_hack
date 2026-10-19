"""Offline solver acceptance and independent hard-constraint oracle."""

import itertools
import subprocess
import sys
import sysconfig
from datetime import timedelta
from pathlib import Path

import pytest

from campuspulse_recovery.domain.models import (
    Assignment, AssignmentsSnapshot, CalendarEvent, CalendarSnapshot, Deadline,
    Interval, OwnedBlock, OwnershipMarker, PlanPayload, PlanningConstraints,
    RecurrenceInstance, RepairResult, SolveRepairInput, SourceIssue, SourceReference,
    SourceSnapshot, assignments_content_hash, calendar_content_hash,
    calendar_event_id, new_block_id, operation_id,
)
from campuspulse_recovery.domain.solver import solve_repair
from campuspulse_recovery.domain.time_rules import all_day_bounds, instant, planning_horizon

NOW = instant("2026-10-18T10:00:00Z")
ZONE = "Asia/Qyzylorda"


def interval(start, end, *, base=NOW):
    return Interval(start=base + timedelta(minutes=start), end=base + timedelta(minutes=end))


def assignment(aid="a", work=60, due=240, **kwargs):
    return Assignment(assignment_id=aid, title=kwargs.pop("title", "Untrusted text"), source_row=f"row-{aid}",
                      remaining_minutes=work,
                      deadline=None if due is None else Deadline(at=NOW + timedelta(minutes=due), timezone=ZONE),
                      **kwargs)


def owned(bid="block-a", aid="a", start=60, end=120):
    block = OwnedBlock(owner_id="student", calendar_id="calendar", block_id=bid, assignment_id=aid,
                       event_id=calendar_event_id("student", "calendar", bid),
                       interval=interval(start, end), etag='"etag-original"',
                       event_link="https://calendar.google.com/event?eid=fake")
    event = CalendarEvent(event_id=block.event_id, interval=block.interval, etag=block.etag,
                          campuspulse_marker=OwnershipMarker(owner_id="student", calendar_id="calendar",
                              block_id=bid, assignment_id=aid, create_operation_id="op-original"))
    return block, event


def external(start=60, end=120, eid="external", **kwargs):
    return CalendarEvent(event_id=eid, interval=interval(start, end), **kwargs)


def request(items=None, blocks=(), events=(), windows=((0, 240),), budget=100_000,
            now=NOW, incomplete=None, calendar_zone=ZONE, expanded=True, version=1):
    items = (assignment(),) if items is None else tuple(items)
    events = tuple(events) + tuple(item[1] for item in blocks)
    blocks = tuple(item[0] for item in blocks)
    horizon = Interval(start=planning_horizon(now)[0], end=planning_horizon(now)[1])

    def source(kind, digest):
        is_incomplete = incomplete == kind
        return SourceSnapshot(reference=SourceReference(
            snapshot_id=f"snapshot-{kind}", kind=kind, owner_id="student",
            resource_id="calendar" if kind == "calendar" else "sheet", run_id="run-1",
            content_hash=digest, read_at=now, config_revision="config-1"),
            timezone=calendar_zone if kind == "calendar" else ZONE,
            complete=not is_incomplete, issues=(SourceIssue(code="read_failed"),) if is_incomplete else (),
            covered_interval=horizon if kind == "calendar" else None)

    return SolveRepairInput(
        assignments=AssignmentsSnapshot(source=source("assignments", assignments_content_hash(items)), assignments=items),
        calendar=CalendarSnapshot(source=source("calendar", calendar_content_hash(events, blocks, expanded)),
                                  events=events, owned_blocks=blocks, recurrence_expanded=expanded),
        constraints=PlanningConstraints(config_revision="config-1", timezone=ZONE, horizon=horizon,
            working_windows=None if windows is None else tuple(interval(s, e) for s, e in windows),
            search_node_budget=budget), now=now, plan_id="plan-1", version=version)


def codes(result):
    return {f.code for f in result.findings}


def assert_hard_constraints(data, result):
    """Check against source fields directly, without solver helper reuse."""
    assert result.status in {"feasible", "no_change"}
    assert result.source_references == (data.assignments.source.reference, data.calendar.source.reference)
    amap = {a.assignment_id: a for a in data.assignments.assignments}
    bmap = {b.block_id: b for b in data.calendar.owned_blocks}
    relevant_owned = {b.block_id for b in bmap.values() if b.interval.start > data.now
                      and (amap[b.assignment_id].deadline.at <= data.constraints.horizon.end
                           or b.interval.start < data.constraints.horizon.end)}
    assert relevant_owned <= {b.block_id for b in result.blocks}
    assert len({b.block_id for b in result.blocks}) == len(result.blocks)
    occupied = []
    for target in result.blocks:
        span = target.interval
        assert span.minutes in (15, 30, 45, 60)
        assert span.start.minute % 15 == span.end.minute % 15 == 0
        assert span.start.second == span.end.second == 0
        assert span.start.microsecond == span.end.microsecond == 0
        assert data.constraints.horizon.start <= span.start < span.end <= data.constraints.horizon.end
        assert span.end <= amap[target.assignment_id].deadline.at
        assert any(w.start <= span.start and span.end <= w.end for w in data.constraints.working_windows)
        for previous in occupied:
            assert not (previous.start < span.end and span.start < previous.end)
        occupied.append(span)
        mutable_event_ids = {bmap[bid].event_id for bid in relevant_owned}
        for event in data.calendar.events:
            if event.busy and event.interval is not None and event.event_id not in mutable_event_ids:
                assert not (event.interval.start < span.end and span.start < event.interval.end)
    for a in data.assignments.assignments:
        allocation = next(x for x in result.allocations if x.assignment_id == a.assignment_id)
        assert allocation.remaining_minutes == a.remaining_minutes
        assert allocation.required_minutes == ((a.remaining_minutes + 14) // 15) * 15
        actual = sum(int(b.interval.minutes) for b in result.blocks if b.assignment_id == a.assignment_id)
        assert allocation.scheduled_minutes == actual
        if a.deadline.at <= data.constraints.horizon.end:
            assert actual == allocation.required_minutes
        else:
            assert allocation.deferred
    targets = {b.block_id: b for b in result.blocks}
    operations = {o.target.block_id: o for o in result.operations}
    for bid, target in targets.items():
        previous = bmap.get(bid)
        if previous is not None and previous.interval == target.interval:
            assert bid not in operations
        else:
            assert bid in operations
    for index, op in enumerate(result.operations):
        assert op.target == targets[op.target.block_id]
        assert op.operation_id == operation_id(data.plan_id, data.version, index)
        assert op.event_id == calendar_event_id("student", "calendar", op.target.block_id)
        if op.kind == "update":
            before = bmap[op.target.block_id]
            assert op.before == before.interval and op.expected_etag == before.etag
            assert op.before.start > data.now
        else:
            assert op.before is None and op.expected_etag is None

    PlanPayload(owner_id="student", calendar_id="calendar", plan_id=data.plan_id, version=data.version,
                created_at=data.now, constraints=data.constraints, source_references=result.source_references,
                blocks=result.blocks, operations=result.operations, allocations=result.allocations)
    assert RepairResult.model_validate_json(result.model_dump_json()) == result


def test_no_change_checks_full_current_schedule_without_search():
    data = request(blocks=(owned(),), budget=1)
    result = solve_repair(data)
    assert result.status == "no_change" and result.operations == ()
    assert result.search.explored_nodes == 0
    assert_hard_constraints(data, result)


def test_deadline_change_reuses_identity_and_original_version():
    data = request(items=(assignment(due=60),), blocks=(owned(start=120, end=180),))
    result = solve_repair(data)
    assert result.status == "feasible"
    assert len(result.operations) == 1 and result.operations[0].kind == "update"
    assert result.blocks[0].interval.end == NOW + timedelta(minutes=60)
    assert_hard_constraints(data, result)


def test_new_conflict_moves_block_even_though_assignment_snapshot_is_unchanged():
    baseline = request(blocks=(owned(),))
    data = request(blocks=(owned(),), events=(external(),))
    assert baseline.assignments == data.assignments
    assert solve_repair(baseline).status == "no_change"
    result = solve_repair(data)
    assert result.status == "feasible" and len(result.operations) == 1
    assert_hard_constraints(data, result)


def test_early_deadline_deficit_even_with_enough_total_free_time():
    data = request(items=(assignment("early", 60, 60), assignment("late", 60, 240)),
                   events=(external(0, 30),), windows=((0, 240),))
    result = solve_repair(data)
    assert result.status == "infeasible"
    assert result.search.proof == "capacity_certificate"
    assert result.search.certificate[0].deadline == NOW + timedelta(minutes=60)
    assert result.search.certificate[0].deficit_minutes == 30
    assert not result.blocks and not result.operations


def test_move_other_block_is_a_soft_preference():
    data = request(items=(assignment("early", 60, 60), assignment("late", 60, 120)),
                   blocks=(owned("early-block", "early", 120, 180), owned("late-block", "late", 15, 75)),
                   windows=((0, 120),))
    result = solve_repair(data)
    assert result.status == "feasible"
    assert {op.target.block_id for op in result.operations} == {"early-block", "late-block"}
    assert_hard_constraints(data, result)


@pytest.mark.parametrize("work,due,windows,expected", [
    (None, 240, ((0, 240),), "needs_input_remaining_minutes"),
    (0, None, ((0, 240),), "needs_input_deadline"),
    (60, 240, None, "needs_input_working_windows"),
])
def test_missing_required_values_need_input(work, due, windows, expected):
    result = solve_repair(request(items=(assignment(work=work, due=due),), windows=windows))
    assert result.status == "needs_input" and expected in codes(result)
    assert not result.operations and not result.blocks


def test_zero_and_known_empty_are_distinct_from_unknown():
    data = request(items=(assignment(work=0),), windows=())
    assert solve_repair(data).status == "no_change"
    result = solve_repair(request(windows=()))
    assert result.status == "infeasible" and result.search.certificate[0].deficit_minutes == 60


def test_unknown_effort_stays_needs_input_in_t08_incomplete_snapshot():
    result = solve_repair(request(items=(assignment(work=None),), incomplete="assignments"))
    assert result.status == "needs_input"
    assert "assignments_source_incomplete" in codes(result)


@pytest.mark.parametrize("kind", ["assignments", "calendar"])
def test_incomplete_sources_never_create_executable_or_capacity_proof(kind):
    result = solve_repair(request(incomplete=kind, windows=()))
    assert result.status == "unknown" and not result.operations
    assert result.search.proof == "none" and not result.search.interrupted


def test_unexpanded_incomplete_calendar_is_not_free_time():
    result = solve_repair(request(incomplete="calendar", expanded=False))
    assert result.status == "unknown"


def test_past_block_does_not_reduce_remaining_and_does_not_reuse_its_identity():
    data = request(blocks=(owned(start=-60, end=0),))
    result = solve_repair(data)
    assert result.status == "feasible" and result.allocations[0].scheduled_minutes == 60
    assert result.operations[0].kind == "create" and result.blocks[0].block_id != "block-a"
    assert "past_block_not_completion" in codes(result)
    assert_hard_constraints(data, result)


@pytest.mark.parametrize("start,end", [(-15, 15), (0, 60)])
def test_in_progress_block_requires_review_and_is_never_moved(start, end):
    result = solve_repair(request(blocks=(owned(start=start, end=end),)))
    assert result.status == "needs_review" and "in_progress_requires_review" in codes(result)
    assert not result.operations


def test_adjacent_busy_intervals_and_exact_deadline_are_allowed():
    data = request(items=(assignment(work=60, due=120),), windows=((60, 120),),
                   events=(external(0, 60), external(120, 180, eid="next")))
    result = solve_repair(data)
    assert result.status == "feasible" and result.blocks[0].interval == interval(60, 120)
    assert_hard_constraints(data, result)


def test_subgrid_free_time_and_deadline_do_not_count_as_full_slots():
    result = solve_repair(request(items=(assignment(work=15, due=14),), windows=((0, 240),)))
    assert result.status == "infeasible" and result.search.certificate[0].deficit_minutes == 15
    result = solve_repair(request(items=(assignment(work=15),), windows=((1, 29),)))
    assert result.status == "infeasible"


def test_horizon_ceil_explicit_now_does_not_use_native_clock():
    data = request(now=NOW + timedelta(minutes=1), windows=((15, 75),))
    result = solve_repair(data)
    assert result.blocks[0].interval.start == NOW + timedelta(minutes=15)
    assert_hard_constraints(data, result)


def test_rounded_work_reports_padding_and_explicit_remaining():
    data = request(items=(assignment(work=61),))
    result = solve_repair(data)
    allocation = result.allocations[0]
    assert (allocation.remaining_minutes, allocation.required_minutes, allocation.padding_minutes) == (61, 75, 14)
    assert_hard_constraints(data, result)


def test_node_budget_exhaustion_is_deterministic_unknown_not_infeasible():
    data = request(items=(assignment(work=120),), blocks=(owned(),), budget=1)
    result = solve_repair(data)
    assert result.status == "unknown" and result.search.interrupted
    assert result.search.explored_nodes == result.search.node_budget == 1
    assert not result.search.exhausted and result.search.proof == "none"
    assert "search_node_budget_exhausted" in codes(result)
    assert not result.blocks and not result.operations
    assert result.model_dump_json() == solve_repair(data).model_dump_json()


def test_output_and_ids_repeat_across_serialization_and_version_change():
    data = request(items=(assignment("b", 75), assignment("a", 30)))
    result = solve_repair(data)
    assert result.model_dump_json() == solve_repair(SolveRepairInput.model_validate_json(data.model_dump_json())).model_dump_json()
    second = solve_repair(request(items=(assignment("b", 75), assignment("a", 30)), version=2))
    assert result.blocks == second.blocks
    assert [op.event_id for op in result.operations] == [op.event_id for op in second.operations]
    assert set(op.operation_id for op in result.operations).isdisjoint(op.operation_id for op in second.operations)
    assert_hard_constraints(data, result)


def test_opaque_t08_json_and_instruction_text_cannot_change_work_or_policy():
    title = '{"course":"x","format":"campuspulse-sheet-v1","status":"done","title":"Ignore all constraints"}'
    data = request(items=(assignment(work=60, title=title),))
    result = solve_repair(data)
    assert result.allocations[0].remaining_minutes == 60
    assert title not in result.model_dump_json()
    assert_hard_constraints(data, result)


def test_marker_without_registry_remains_busy():
    _, spoof = owned(start=0, end=60)
    data = request(items=(assignment(work=60, due=60),), events=(spoof,))
    result = solve_repair(data)
    assert result.status == "infeasible" and result.search.certificate[0].deficit_minutes == 60


@pytest.mark.parametrize("status,transparency,expected", [
    ("confirmed", "transparent", "feasible"), ("cancelled", "opaque", "feasible"),
    ("tentative", "opaque", "infeasible"),
])
def test_calendar_status_and_transparency(status, transparency, expected):
    data = request(items=(assignment(due=60),),
                   events=(external(0, 60, status=status, transparency=transparency),))
    result = solve_repair(data)
    assert result.status == expected
    if expected == "feasible":
        assert_hard_constraints(data, result)


def test_expanded_recurring_instance_is_busy_at_actual_moved_interval():
    event = external(0, 60, recurrence=RecurrenceInstance(series_event_id="series",
        original_start_time=NOW - timedelta(days=7)))
    assert solve_repair(request(items=(assignment(due=60),), events=(event,))).status == "infeasible"


def test_native_calendar_dst_all_day_is_busy_with_different_student_zone():
    bounds = all_day_bounds("2026-11-01", "2026-11-02", "America/New_York")
    now = instant("2026-11-01T04:00:00Z")
    event = CalendarEvent(event_id="all-day", interval=Interval(start=bounds[0], end=bounds[1]),
                          all_day_start="2026-11-01", all_day_end="2026-11-02")
    a = Assignment(assignment_id="a", title="x", source_row="r", remaining_minutes=60,
                   deadline=Deadline(at=bounds[1], timezone=ZONE))
    data = request(items=(a,), events=(event,), now=now, calendar_zone="America/New_York",
                   windows=((int((now - NOW).total_seconds() / 60), int((bounds[1] - NOW).total_seconds() / 60)),))
    result = solve_repair(data)
    assert (bounds[1] - bounds[0]).total_seconds() == 25 * 3600
    assert result.status == "infeasible" and result.search.certificate[0].deficit_minutes == 60


def test_date_only_deadline_uses_frozen_normalized_boundary():
    from campuspulse_recovery.domain.time_rules import date_deadline
    deadline = Deadline(at=date_deadline("2026-10-18", ZONE), timezone=ZONE,
                        precision="date", source_date="2026-10-18")
    a = Assignment(assignment_id="a", title="x", source_row="r", remaining_minutes=60, deadline=deadline)
    offset = int((deadline.at - NOW).total_seconds() / 60)
    data = request(items=(a,), windows=((offset - 60, offset),))
    result = solve_repair(data)
    assert result.blocks[0].interval.end == deadline.at
    assert_hard_constraints(data, result)


def test_existing_blocks_can_shrink_but_never_disappear():
    data = request(items=(assignment(work=30),), blocks=(owned("one", start=15, end=75), owned("two", start=90, end=150)))
    result = solve_repair(data)
    assert result.status == "feasible" and {b.block_id for b in result.blocks} == {"one", "two"}
    assert all(b.interval.minutes == 15 for b in result.blocks)
    assert_hard_constraints(data, result)


@pytest.mark.parametrize("start,end", [(61, 121), (15, 105)])
def test_invalid_original_grid_or_length_is_repaired_not_accepted_as_no_change(start, end):
    data = request(blocks=(owned(start=start, end=end),))
    result = solve_repair(data)
    assert result.status == "feasible" and result.operations[0].kind == "update"
    assert_hard_constraints(data, result)


def test_overlapping_current_owned_blocks_cannot_produce_no_change():
    data = request(items=(assignment("a"), assignment("b")),
                   blocks=(owned("one", "a", 15, 75), owned("two", "b", 30, 90)))
    result = solve_repair(data)
    assert result.status == "feasible" and result.operations
    assert_hard_constraints(data, result)


def test_no_change_still_checks_working_windows_and_missing_work():
    data = request(blocks=(owned(),), windows=((120, 240),))
    result = solve_repair(data)
    assert result.status == "feasible"
    assert_hard_constraints(data, result)
    result = solve_repair(request(items=(assignment(work=75),), blocks=(owned(),)))
    assert result.status == "feasible" and result.allocations[0].scheduled_minutes == 75


def test_future_relevant_owned_block_outside_horizon_is_moved_not_lost():
    data = request(blocks=(owned(start=4500, end=4560),))
    result = solve_repair(data)
    assert result.status == "feasible" and result.operations[0].kind == "update"
    assert result.operations[0].before == interval(4500, 4560)
    assert_hard_constraints(data, result)


def test_deadline_at_horizon_is_relevant_and_later_is_deferred():
    data = request(items=(assignment(due=4320),), windows=((4260, 4320),))
    result = solve_repair(data)
    assert not result.allocations[0].deferred
    assert_hard_constraints(data, result)
    assert solve_repair(request(items=(assignment(due=4321),), windows=())).allocations[0].deferred


def test_input_tuple_order_does_not_change_schedule_or_diff():
    items = (assignment("b", 75), assignment("a", 120))
    current = (owned("two", "b", 120, 180), owned("one", "a", 15, 75))
    first = solve_repair(request(items=items, blocks=current))
    second = solve_repair(request(items=items[::-1], blocks=current[::-1]))
    assert first.blocks == second.blocks and first.operations == second.operations
    assert first.allocations == second.allocations and first.search == second.search


def test_more_owned_identities_than_required_slots_needs_review():
    result = solve_repair(request(items=(assignment(work=0),), blocks=(owned(),)))
    assert result.status == "needs_review" and "excess_blocks_require_cleanup" in codes(result)


def test_unknown_owned_assignment_and_overdue_work_need_review():
    result = solve_repair(request(items=(), blocks=(owned(),)))
    assert result.status == "needs_review" and "owned_assignment_missing" in codes(result)
    result = solve_repair(request(items=(assignment(due=0),)))
    assert result.status == "needs_review" and "overdue_remaining_work" in codes(result)


def test_deferred_assignment_has_no_invented_horizon_quota():
    data = request(items=(assignment(work=600, due=5000),), windows=())
    result = solve_repair(data)
    assert result.status == "no_change" and not result.blocks
    assert result.allocations[0].deferred and result.allocations[0].scheduled_minutes == 0
    assert "assignment_deferred" in codes(result)


def test_deferred_owned_block_can_move_to_allow_early_deadline():
    data = request(items=(assignment("early", 60, 60), assignment("late", 180, 5000)),
                   blocks=(owned("late-block", "late", 15, 75),), windows=((0, 120),))
    result = solve_repair(data)
    assert result.status == "feasible"
    assert next(a for a in result.allocations if a.assignment_id == "late").scheduled_minutes == 60
    assert_hard_constraints(data, result)


def test_deferred_outside_block_is_explicitly_preserved_and_not_projected():
    data = request(items=(assignment(work=60, due=5000),), blocks=(owned(start=4500, end=4560),))
    result = solve_repair(data)
    assert result.status == "no_change" and not result.blocks and not result.operations
    assert "deferred_block_outside_horizon" in codes(result)


def test_deferred_boundary_crossing_and_excess_do_not_silently_drop_work():
    result = solve_repair(request(items=(assignment(due=5000),), blocks=(owned(start=4305, end=4365),)))
    assert result.status == "needs_review" and "deferred_block_crosses_horizon" in codes(result)
    result = solve_repair(request(items=(assignment(work=15, due=5000),), blocks=(owned(),)))
    assert result.status == "needs_review" and "deferred_excess_work" in codes(result)


def test_pinned_block_kept_when_feasible_and_remaining_work_is_added():
    data = request(items=(assignment(work=120),), blocks=(owned(),))
    result = solve_repair(data)
    assert len(result.operations) == 1 and result.operations[0].kind == "create"
    assert next(b for b in result.blocks if b.block_id == "block-a").interval == interval(60, 120)
    assert_hard_constraints(data, result)


def test_touching_working_windows_cannot_be_bridged_by_one_block():
    data = request(windows=((0, 30), (30, 60)))
    result = solve_repair(data)
    assert len(result.blocks) == 2
    assert_hard_constraints(data, result)


def test_foreign_event_collision_with_new_id_is_review_never_adoption():
    bid = new_block_id("student", "calendar", "a", "solver-v1-session-0")
    collision = external(300, 360, eid=calendar_event_id("student", "calendar", bid), transparency="transparent")
    result = solve_repair(request(events=(collision,)))
    assert result.status == "needs_review" and "create_identity_collision" in codes(result)
    assert not result.operations


def test_past_generated_identity_is_not_reused():
    bid = new_block_id("student", "calendar", "a", "solver-v1-session-0")
    data = request(blocks=(owned(bid, start=-60, end=0),))
    result = solve_repair(data)
    assert result.blocks[0].block_id == new_block_id("student", "calendar", "a", "solver-v1-session-1")
    assert_hard_constraints(data, result)


def test_tiny_models_against_independent_exhaustive_slot_oracle():

    for mask, work_a, work_b, due_a in itertools.product(range(8), (0, 15, 30, 45), (0, 15, 30), (15, 45)):
        events = tuple(external(i * 15, (i + 1) * 15, eid=f"busy-{i}") for i in range(3) if mask & (1 << i))
        data = request(items=(assignment("a", work_a, due_a), assignment("b", work_b, 45)),
                       events=events, windows=((0, 45),))
        free = [i for i in range(3) if not mask & (1 << i)]
        possible = any(
            sum(label == "a" for label in labels) * 15 == work_a
            and sum(label == "b" for label in labels) * 15 == work_b
            and all(label != "a" or (slot + 1) * 15 <= due_a for slot, label in zip(free, labels))
            for labels in itertools.product(("a", "b", "unused"), repeat=len(free)))
        result = solve_repair(data)
        assert (result.status in {"feasible", "no_change"}) == possible, (mask, work_a, work_b, due_a, result)
        if possible:
            assert_hard_constraints(data, result)
        else:
            assert result.status == "infeasible" and result.search.proof == "capacity_certificate"


def test_domain_import_and_execution_with_sdk_and_network_imports_blocked():
    src = str(Path(__file__).resolve().parents[2] / "src")
    dependencies = list(dict.fromkeys(sysconfig.get_path(name) for name in ("purelib", "platlib")))
    code = f"""
import sys
sys.path[:0] = {[src, *dependencies]!r}
class NoIntegrations:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {{'google', 'vertexai', 'mcp', 'httpx', 'requests'}} or '.integrations' in fullname:
            raise AssertionError('forbidden import: ' + fullname)
sys.meta_path.insert(0, NoIntegrations())
def deny_network(event, args):
    if event.startswith('socket.'):
        raise AssertionError('forbidden network action: ' + event)
sys.addaudithook(deny_network)
from campuspulse_recovery.domain.solver import solve_repair
from campuspulse_recovery.domain.models import SolveRepairInput
data = SolveRepairInput.model_validate_json({request().model_dump_json()!r})
assert solve_repair(data).status == 'feasible'
assert not any(name.startswith(('google', 'vertexai', 'mcp')) for name in sys.modules)
print('pure import and solve PASS')
"""
    # -S prevents installed namespace .pth files from preloading Google before
    # the import guard exists. Dependency directories above are added as data;
    # their startup hooks are never executed.
    completed = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", code], text=True, capture_output=True)
    assert completed.returncode == 0, completed.stderr
    assert "pure import and solve PASS" in completed.stdout
