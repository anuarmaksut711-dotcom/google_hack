"""T27 independent constraint oracle; pure solver, no LLM/network."""
from datetime import timedelta

import pytest

from campuspulse_recovery.domain.solver import solve_repair
from tests.domain.test_solver import assignment, external, owned, request, NOW
from tests.faults.support import install_network_guard


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    install_network_guard(monkeypatch)


def assert_schedule(inputs, result, expected_minutes):
    assert result.status in {"feasible", "no_change"}
    assignments = {a.assignment_id: a for a in inputs.assignments.assignments}
    mutable = {b.event_id for b in inputs.calendar.owned_blocks if b.interval.start > inputs.now}
    for aid, minutes in expected_minutes.items():
        blocks = [b for b in result.blocks if b.assignment_id == aid]
        assert sum((b.interval.end - b.interval.start).total_seconds() / 60 for b in blocks) == minutes
        for block in blocks:
            span = block.interval
            assert inputs.now <= span.start < span.end <= assignments[aid].deadline.at
            assert any(w.start <= span.start and span.end <= w.end for w in inputs.constraints.working_windows)
            assert span.start.tzinfo is not None
            for event in inputs.calendar.events:
                if event.busy and event.interval and event.event_id not in mutable:
                    assert not (event.interval.start < span.end and span.start < event.interval.end)
    for i, a in enumerate(result.blocks):
        for b in result.blocks[i + 1:]:
            assert a.interval.end <= b.interval.start or b.interval.end <= a.interval.start


def test_no_change_empty_diff():
    inputs = request(items=(assignment(work=45, due=180),), blocks=(owned(start=45, end=90),))
    result = solve_repair(inputs)
    assert result.status == "no_change" and result.operations == ()
    assert [(b.interval.start, b.interval.end) for b in result.blocks] == [(NOW + timedelta(minutes=45), NOW + timedelta(minutes=90))]
    assert_schedule(inputs, result, {"a": 45})


def test_changed_deadline_moves_same_owned_identity_before_new_due():
    inputs = request(items=(assignment(work=60, due=90),), blocks=(owned(start=150, end=210),), windows=((15, 90),))
    result = solve_repair(inputs)
    assert_schedule(inputs, result, {"a": 60})
    assert len(result.operations) == 1 and result.operations[0].kind == "update"
    assert result.operations[0].target.block_id == "block-a"
    assert result.operations[0].expected_etag == '"etag-original"'


def test_new_conflict_replans_with_unchanged_assignment():
    inputs = request(items=(assignment(work=60, due=180),), blocks=(owned(start=30, end=90),),
                     events=(external(30, 90),), windows=((30, 150),))
    result = solve_repair(inputs)
    assert_schedule(inputs, result, {"a": 60})
    assert len(result.operations) == 1
    assert result.blocks[0].interval.start == NOW + timedelta(minutes=90)
    assert result.blocks[0].interval.end == NOW + timedelta(minutes=150)


def test_deadline_specific_deficit_later_capacity_does_not_count():
    inputs = request(items=(assignment("early", 90, 90), assignment("late", 60, 300)),
                     events=(external(0, 45),), windows=((0, 300),))
    result = solve_repair(inputs)
    assert 300 - 45 >= 90 + 60  # Sufficient total, only 45 minutes before early due.
    assert result.status == "infeasible" and not result.operations and not result.blocks
    certificates = [c for c in result.search.certificate if c.deadline == NOW + timedelta(minutes=90)]
    assert len(certificates) == 1 and certificates[0].deficit_minutes == 45
    assert result.search.proof == "capacity_certificate"


def test_move_other_block_is_required_for_feasibility():
    inputs = request(items=(assignment("urgent", 60, 75), assignment("later", 60, 135)),
        blocks=(owned("urgent-block", "urgent", 150, 210), owned("later-block", "later", 15, 75)),
        windows=((15, 135),))
    result = solve_repair(inputs)
    assert_schedule(inputs, result, {"urgent": 60, "later": 60})
    targets = {b.assignment_id: b.interval for b in result.blocks}
    assert (targets["urgent"].start, targets["urgent"].end) == (NOW + timedelta(minutes=15), NOW + timedelta(minutes=75))
    assert (targets["later"].start, targets["later"].end) == (NOW + timedelta(minutes=75), NOW + timedelta(minutes=135))
    assert {op.target.block_id for op in result.operations} == {"urgent-block", "later-block"}


def test_past_block_is_not_completion_and_unknown_effort_needs_input():
    inputs = request(items=(assignment(work=60, due=180),), blocks=(owned(start=-60, end=0),), windows=((15, 75),))
    result = solve_repair(inputs)
    assert_schedule(inputs, result, {"a": 60})
    assert result.allocations[0].remaining_minutes == 60
    assert result.operations[0].kind == "create" and result.blocks[0].block_id != "block-a"
    unknown = solve_repair(request(items=(assignment(work=None),)))
    assert unknown.status == "needs_input" and not unknown.operations


def test_half_open_adjacency_and_bounded_unknown():
    inputs = request(items=(assignment(work=60, due=135),), events=(external(0, 75), external(135, 195, eid="next")), windows=((75, 135),))
    result = solve_repair(inputs)
    assert_schedule(inputs, result, {"a": 60})
    assert result.blocks[0].interval.start == NOW + timedelta(minutes=75)
    limited = solve_repair(request(items=(assignment(work=120, due=240),), blocks=(owned(),), budget=1))
    assert limited.status == "unknown" and not limited.operations
    assert limited.search.interrupted and limited.search.proof == "none"
