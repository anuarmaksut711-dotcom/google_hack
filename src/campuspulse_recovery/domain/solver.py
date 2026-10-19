"""Bounded, deterministic recovery calculation. No clock, I/O, SDK or LLM.

See docs/solver.md for the searched model and the limits of its quality claim.
Snapshots are data, not capabilities; the backend must reload trusted sources.
"""

from __future__ import annotations

from datetime import timedelta

from pydantic import ValidationError

from .models import (
    BlockTarget, Finding, Interval, Operation, RepairResult, RepairStatus,
    SearchEvidence, SolveRepairInput, WorkAllocation, calendar_event_id,
    new_block_id, operation_id, validate_projected_schedule,
)

_STEP = timedelta(minutes=15)


def _overlap(left: Interval, right: Interval) -> bool:
    return left.start < right.end and right.start < left.end


def _inside(interval: Interval, windows: tuple[Interval, ...]) -> bool:
    return any(w.start <= interval.start and interval.end <= w.end for w in windows)


def solve_repair(request: SolveRepairInput) -> RepairResult:
    """Calculate a typed repair from explicit snapshots, constraints and now.

    Never infer effort/completion from titles, Calendar history or elapsed time.
    Only the registry-joined owned_blocks collection defines mutable identities.
    """
    request = SolveRepairInput.model_validate(request)
    constraints = request.constraints
    horizon = constraints.horizon
    assignments = tuple(sorted(request.assignments.assignments, key=lambda a: a.assignment_id))
    assignment_map = {a.assignment_id: a for a in assignments}
    owned = tuple(sorted(request.calendar.owned_blocks, key=lambda b: b.block_id))
    findings: list[Finding] = []

    def result(status, *, blocks=(), operations=(), allocations=(), extra=(), nodes=0,
               interrupted=False, certificate=()):
        return RepairResult(
            status=status, constraints=constraints,
            source_references=(request.assignments.source.reference, request.calendar.source.reference),
            blocks=blocks, operations=operations, allocations=allocations,
            findings=tuple(findings) + tuple(extra),
            search=SearchEvidence(explored_nodes=nodes, node_budget=constraints.search_node_budget,
                                  interrupted=interrupted,
                                  proof="capacity_certificate" if certificate else "none",
                                  certificate=certificate),
        )

    missing = []
    if constraints.working_windows is None:
        missing.append(Finding(code="needs_input_working_windows"))
    for a in assignments:
        if a.remaining_minutes is None:
            missing.append(Finding(code="needs_input_remaining_minutes", assignment_id=a.assignment_id))
        if a.deadline is None:
            missing.append(Finding(code="needs_input_deadline", assignment_id=a.assignment_id))
    incomplete = []
    if not request.assignments.source.complete:
        incomplete.append(Finding(code="assignments_source_incomplete"))
    if not request.calendar.source.complete or not request.calendar.recurrence_expanded:
        incomplete.append(Finding(code="calendar_source_incomplete"))
    if missing:
        return result(RepairStatus.NEEDS_INPUT, extra=missing + incomplete)
    if incomplete:
        return result(RepairStatus.UNKNOWN, extra=incomplete)

    windows = constraints.working_windows
    review = []
    mutable = []
    for block in owned:
        if block.interval.end <= request.now:
            findings.append(Finding(code="past_block_not_completion", block_id=block.block_id,
                                    assignment_id=block.assignment_id))
            continue
        if block.interval.start <= request.now:
            review.append(Finding(code="in_progress_requires_review", block_id=block.block_id,
                                  assignment_id=block.assignment_id))
            continue
        a = assignment_map.get(block.assignment_id)
        if a is None:
            review.append(Finding(code="owned_assignment_missing", block_id=block.block_id))
            continue
        event = next(e for e in request.calendar.events if e.event_id == block.event_id)
        if event.all_day_start is not None or event.status != "confirmed":
            review.append(Finding(code="unsupported_owned_event", block_id=block.block_id))
            continue
        if a.deadline.at > horizon.end and block.interval.start >= horizon.end:
            findings.append(Finding(code="deferred_block_outside_horizon", block_id=block.block_id,
                                    assignment_id=a.assignment_id))
            continue
        if a.deadline.at > horizon.end and block.interval.end > horizon.end:
            review.append(Finding(code="deferred_block_crosses_horizon", block_id=block.block_id))
            continue
        mutable.append(block)

    demands = {}
    deadlines = {}
    deferred = {}
    for a in assignments:
        aid = a.assignment_id
        deferred[aid] = a.deadline.at > horizon.end
        deadlines[aid] = min(a.deadline.at, horizon.end)
        current = tuple(b for b in mutable if b.assignment_id == aid)
        if a.required_minutes and a.deadline.at <= request.now:
            review.append(Finding(code="overdue_remaining_work", assignment_id=aid, deadline=a.deadline.at))
        if deferred[aid]:
            findings.append(Finding(code="assignment_deferred", assignment_id=aid, deadline=a.deadline.at))

            demands[aid] = sum(int(b.interval.minutes) for b in current)
            outside = sum(b.interval.minutes for b in owned if b.assignment_id == aid
                          and b.interval.start >= horizon.end)
            if demands[aid] + outside > a.required_minutes:
                review.append(Finding(code="deferred_excess_work", assignment_id=aid))
            for b in current:
                try:
                    BlockTarget(block_id=b.block_id, assignment_id=aid, interval=b.interval)
                except ValidationError:
                    review.append(Finding(code="unsupported_deferred_block", block_id=b.block_id))
        else:
            demands[aid] = a.required_minutes
        if len(current) * 15 > demands[aid]:
            review.append(Finding(code="excess_blocks_require_cleanup", assignment_id=aid))
    if review:
        return result(RepairStatus.NEEDS_REVIEW, extra=review)

    mutable = tuple(sorted(mutable, key=lambda b: b.block_id))
    mutable_map = {b.block_id: b for b in mutable}

    movable_events = {b.event_id for b in mutable}
    busy = tuple(e.interval for e in request.calendar.events
                 if e.busy and e.interval is not None and e.event_id not in movable_events)
    slots = tuple(Interval(start=horizon.start + i * _STEP, end=horizon.start + (i + 1) * _STEP)
                  for i in range(288))
    available = tuple(i for i, slot in enumerate(slots)
                      if _inside(slot, windows) and not any(_overlap(slot, b) for b in busy))
    available_set = frozenset(available)



    certificates = []
    for deadline in sorted({deadlines[a] for a in demands if demands[a]}):
        required = sum(amount for aid, amount in demands.items() if deadlines[aid] <= deadline)
        capacity = 15 * sum(slots[i].end <= deadline for i in available)
        if required > capacity:
            certificates.append(Finding(code="deadline_capacity_deficit", deadline=deadline,
                                        deficit_minutes=required - capacity))
    if certificates:
        return result(RepairStatus.INFEASIBLE, extra=certificates, certificate=tuple(certificates))

    def allocations_for(blocks):
        return tuple(WorkAllocation(
            assignment_id=a.assignment_id, remaining_minutes=a.remaining_minutes,
            required_minutes=a.required_minutes, padding_minutes=a.required_minutes - a.remaining_minutes,
            scheduled_minutes=sum(int(b.interval.minutes) for b in blocks if b.assignment_id == a.assignment_id),
            deferred=deferred[a.assignment_id],
        ) for a in assignments)

    def valid(blocks):
        """Independent hard-constraint check, also used before no_change."""
        allocations = allocations_for(blocks)
        try:

            blocks = tuple(BlockTarget.model_validate(b) for b in blocks)
            validate_projected_schedule(blocks, allocations, constraints)
        except (ValidationError, ValueError):
            return False
        targets = {b.block_id: b for b in blocks}
        if not set(mutable_map).issubset(targets):
            return False
        for b in blocks:
            a = assignment_map.get(b.assignment_id)
            if a is None or b.interval.end > a.deadline.at:
                return False
            if any(_overlap(b.interval, item) for item in busy):
                return False
            old = mutable_map.get(b.block_id)
            if old is not None and old.assignment_id != b.assignment_id:
                return False
        return all(item.scheduled_minutes == demands[item.assignment_id] for item in allocations)

    original_targets = []
    for block in mutable:
        try:
            original_targets.append(BlockTarget(block_id=block.block_id, assignment_id=block.assignment_id,
                                                interval=block.interval))
        except ValidationError:
            break
    else:
        current = tuple(sorted(original_targets, key=lambda b: (b.interval.start, b.block_id)))
        if valid(current):
            return result(RepairStatus.NO_CHANGE, blocks=current, allocations=allocations_for(current))

    def slot_indices(interval):
        return frozenset(i for i, slot in enumerate(slots) if _overlap(slot, interval))

    candidates = []
    for block in mutable:
        try:
            target = BlockTarget(block_id=block.block_id, assignment_id=block.assignment_id,
                                 interval=block.interval)
        except ValidationError:
            continue
        indices = slot_indices(block.interval)
        if (horizon.start <= block.interval.start and block.interval.end <= deadlines[block.assignment_id]
                and indices.issubset(available_set) and _inside(block.interval, windows)):
            candidates.append((target, indices))

    def construct(pinned, occupied):
        blocks = list(pinned)
        free = [i for i in available if i not in occupied]
        for aid in sorted(demands, key=lambda aid: (deadlines[aid], aid)):
            amount = demands[aid] - sum(int(b.interval.minutes) for b in pinned if b.assignment_id == aid)
            old = [b for b in mutable if b.assignment_id == aid
                   and all(b.block_id != p.block_id for p in pinned)]
            if amount < len(old) * 15:
                return None
            selected = [i for i in free if slots[i].end <= deadlines[aid]][:amount // 15]
            if len(selected) * 15 != amount:
                return None
            used = frozenset(selected)
            free = [i for i in free if i not in used]
            groups = []
            for i in selected:
                if (groups and len(groups[-1]) < 4 and groups[-1][-1] + 1 == i
                        and _inside(Interval(start=slots[groups[-1][0]].start, end=slots[i].end), windows)):
                    groups[-1].append(i)
                else:
                    groups.append([i])

            while len(groups) < len(old):
                position = next((j for j, g in enumerate(groups) if len(g) > 1), None)
                if position is None:
                    return None
                group = groups[position]
                groups[position:position + 1] = [group[:-1], group[-1:]]
            intervals = [Interval(start=slots[g[0]].start, end=slots[g[-1]].end) for g in groups]
            for previous in sorted(old, key=lambda b: (b.interval.start, b.block_id)):
                chosen = min(intervals, key=lambda value: (
                    abs((value.start - previous.interval.start).total_seconds())
                    + abs((value.end - previous.interval.end).total_seconds()), value.start, value.end))
                intervals.remove(chosen)
                blocks.append(BlockTarget(block_id=previous.block_id, assignment_id=aid, interval=chosen))

            ordinal = 0
            known_ids = {b.block_id for b in owned} | {b.block_id for b in blocks}
            ref = request.calendar.source.reference
            for interval in sorted(intervals, key=lambda v: v.start):
                while True:
                    block_id = new_block_id(ref.owner_id, ref.resource_id, aid, f"solver-v1-session-{ordinal}")
                    ordinal += 1
                    if block_id not in known_ids:
                        break
                known_ids.add(block_id)
                blocks.append(BlockTarget(block_id=block_id, assignment_id=aid, interval=interval))
        return tuple(sorted(blocks, key=lambda b: (b.interval.start, b.block_id)))



    stack = [(0, (), frozenset())]
    nodes = 0
    while stack and nodes < constraints.search_node_budget:
        position, pinned, occupied = stack.pop()
        nodes += 1
        if position < len(candidates):
            target, indices = candidates[position]
            stack.append((position + 1, pinned, occupied))
            total = int(target.interval.minutes) + sum(int(b.interval.minutes) for b in pinned
                                                      if b.assignment_id == target.assignment_id)
            unpinned_count = sum(b.assignment_id == target.assignment_id for b in mutable) - 1 - sum(
                b.assignment_id == target.assignment_id for b in pinned)
            if not occupied.intersection(indices) and total + unpinned_count * 15 <= demands[target.assignment_id]:
                stack.append((position + 1, pinned + (target,), occupied | indices))
            continue
        blocks = construct(pinned, occupied)
        if blocks is None or not valid(blocks):
            continue
        operations = []
        observed_ids = {e.event_id for e in request.calendar.events}
        ref = request.calendar.source.reference
        for target in sorted(blocks, key=lambda b: b.block_id):
            old = mutable_map.get(target.block_id)
            if old is not None and old.interval == target.interval:
                continue
            event_id = calendar_event_id(ref.owner_id, ref.resource_id, target.block_id)
            if old is None and event_id in observed_ids:
                return result(RepairStatus.NEEDS_REVIEW, nodes=nodes,
                              extra=(Finding(code="create_identity_collision", block_id=target.block_id),))
            operations.append(Operation(
                operation_id=operation_id(request.plan_id, request.version, len(operations)),
                kind="update" if old else "create", event_id=event_id, target=target,
                expected_etag=old.etag if old else None, before=old.interval if old else None,
            ))
        return result(RepairStatus.FEASIBLE if operations else RepairStatus.NO_CHANGE,
                      blocks=blocks, operations=tuple(operations), allocations=allocations_for(blocks), nodes=nodes,
                      extra=(Finding(code="bounded_preservation_edf"),))
    return result(RepairStatus.UNKNOWN, nodes=nodes, interrupted=bool(stack),
                  extra=(Finding(code="search_node_budget_exhausted" if stack else "heuristic_not_found"),))
