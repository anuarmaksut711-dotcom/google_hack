"""Pure deterministic fixtures; prompts are NOT a fake speech-quality eval.

Requests use existing synthetic domain builders, not another solver. Machine
expectations are pinned in JSON, with independent capacity/allocation checks.
"""
import json
from pathlib import Path

import pytest

from campuspulse_recovery.domain.solver import solve_repair
from tests.domain.test_solver import assignment, owned, request, assert_hard_constraints

DATA = json.loads(Path(__file__).with_name("russian_cases.json").read_text(encoding="utf-8"))


def fixture(name):
    if name == "deadlines":
        return request(items=[assignment("a", 30, 60), assignment("b", 60, 180)])
    if name == "hour":
        return request(items=[assignment(work=45, due=60)], blocks=[owned(start=-60, end=0)], windows=((0, 60),))
    if name == "changed":
        return request(items=[assignment(work=60, due=90)], blocks=[owned(start=120, end=180)])
    if name == "unknown": return request(items=[assignment(work=None)])
    if name == "nochange": return request(blocks=[owned()])
    if name == "deficit": return request(items=[assignment(work=90, due=60)], windows=((0, 240),))
    raise AssertionError("unknown fixture")


@pytest.mark.parametrize("case", DATA["cases"], ids=lambda c: c["id"])
def test_russian_machine_expectations_and_repeatability(case):
    before = Path(__file__).with_name("russian_cases.json").read_bytes()
    data = fixture(case["id"])
    expected = case["expected"]
    first = solve_repair(data)
    assert first.model_dump_json() == solve_repair(data).model_dump_json()
    assert data.now.tzinfo is not None
    assert (data.constraints.horizon.end - data.constraints.horizon.start).total_seconds() == 72 * 3600
    assert data.constraints.timezone == DATA["timezone"]
    assert data.now.isoformat().replace("+00:00", "Z") == DATA["synthetic_now"]
    assert first.status == expected["solver_status"]
    assert first.search.proof == expected["proof"]
    assert [a.remaining_minutes for a in data.assignments.assignments] == expected["remaining"]
    assert [a.scheduled_minutes for a in first.allocations] == expected["scheduled"]
    assert [op.kind for op in first.operations] == expected["operation_kinds"]
    assert set(expected.get("findings", [])) <= {f.code for f in first.findings}
    assert not set(case["tools"]) & set(DATA["forbidden_tools"])
    assert case["prompt"] and case["speech_expectation"]
    if first.status in ("feasible", "no_change"):
        assert_hard_constraints(data, first)
    if case["id"] == "deadlines":
        assert [a.assignment_id for a in sorted(data.assignments.assignments, key=lambda a: a.deadline.at)] == expected["deadline_order"]
    if case["id"] == "hour":

        assert first.allocations[0].remaining_minutes == 45
        assert all(b.interval.start >= data.now for b in first.blocks)
    if case["id"] == "deficit":
        f = next(f for f in first.findings if f.code == "deadline_capacity_deficit")
        assert f.deficit_minutes == expected["deficit_minutes"]
        assert f.deadline.isoformat().replace("+00:00", "Z") == expected["deadline"]
        assert data.constraints.working_windows[0].minutes == 240
        assert int((f.deadline - data.now).total_seconds() / 60) == 60
        assert expected["remaining"][0] - 60 == f.deficit_minutes
    assert Path(__file__).with_name("russian_cases.json").read_bytes() == before
