"""Configured Sheets domain reader. No public MCP registration or model authority.

The frozen T03 wire contract is preserved. Course/title/status are reversibly
encoded in Assignment.title, so the authoritative snapshot itself binds them.
Use normalized_assignments on a repository-loaded snapshot for presentation.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from typing import Annotated, Callable, Literal
from uuid import uuid4

from pydantic import StringConstraints, TypeAdapter, ValidationError

from ..domain.models import (
    Assignment, AssignmentsSnapshot, Contract, Deadline, Identifier, Interval, Minutes,
    ReadAssignmentsInput, SourceIssue, SourceReference, SourceSnapshot,
    assignments_content_hash, canonical_json,
)
from ..domain.ports import SnapshotStore, TrustedContext
from ..domain.time_rules import date_deadline, instant, iso_date
from ..security.context import AccessDenied, TrustedRequestContext
from .google_clients import GoogleWorkspaceClients
from .google_credentials import WorkspaceAccessError

HEADERS = ("assignment_id", "course", "title", "deadline", "remaining_minutes", "status")
MAX_ROWS = 500  # Includes header and mandatory empty overflow guard row.
Text = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=256)]
_IDENTIFIER = TypeAdapter(Identifier)


class NormalizedAssignment(Contract):
    assignment_id: Identifier
    course: Text
    title: Text
    deadline: Deadline | None
    remaining_minutes: Minutes | None
    status: Literal["todo", "in_progress", "done"]
    source_row: Identifier

    def project(self) -> Assignment:
        return Assignment(
            assignment_id=self.assignment_id, source_row=self.source_row,
            deadline=self.deadline, remaining_minutes=self.remaining_minutes,
            title=canonical_json({"format": "campuspulse-sheet-v1", "course": self.course,
                                  "title": self.title, "status": self.status}),
        )


def normalized_assignments(snapshot: AssignmentsSnapshot) -> tuple[NormalizedAssignment, ...]:
    """Decode saved T08 values; never treat a caller's snapshot as authoritative.

    Backend callers first load through SnapshotStore.get_assignments. Round-trip
    equality rejects noncanonical/ambiguous envelopes and unknown fields.
    """
    snapshot = AssignmentsSnapshot.model_validate(snapshot)
    result = []
    for item in snapshot.assignments:
        payload = json.loads(item.title)
        if not isinstance(payload, dict) or payload.pop("format", None) != "campuspulse-sheet-v1":
            raise ValueError("assignment_projection_format_invalid")
        row = NormalizedAssignment(
            **payload, assignment_id=item.assignment_id, source_row=item.source_row,
            deadline=item.deadline, remaining_minutes=item.remaining_minutes,
        )
        if row.project() != item:
            raise ValueError("assignment_projection_not_canonical")
        result.append(row)
    return tuple(result)


class AssignmentReadResult(Contract):
    """Local typed view, fully derived from the saved T03 snapshot.

    Not a new public tool schema. No duplicated authoritative metadata. Backend
    obtains snapshot from its repository before constructing this view.
    """

    snapshot: AssignmentsSnapshot

    @property
    def assignments(self) -> tuple[NormalizedAssignment, ...]:
        return normalized_assignments(self.snapshot)

    @property
    def status(self) -> Literal["complete", "needs_input", "error"]:
        if self.snapshot.source.complete:
            return "complete"
        if all(issue.code.startswith("needs_input_") for issue in self.snapshot.source.issues):
            return "needs_input"
        return "error"


def _bounds(area: str) -> tuple[int, int]:
    rectangle = area.rsplit("!", 1)[1]
    match = re.fullmatch(r"([A-Z]+)([0-9]+):([A-Z]+)([0-9]+)", rectangle)
    if match is None:
        raise ValueError("assignment_range_invalid")
    left, first, right, last = match.groups()

    def column(label):
        value = 0
        for character in label:
            value = value * 26 + ord(character) - 64
        return value

    if column(right) - column(left) != 5 or not 3 <= int(last) - int(first) + 1 <= MAX_ROWS:
        raise ValueError("assignment_range_requires_six_columns_and_3_to_500_rows")
    return int(first), int(last)


def _deadline(value: object, timezone: str) -> Deadline | None:
    if value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("deadline_invalid")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        day = iso_date(value)
        return Deadline(at=date_deadline(day, timezone), timezone=timezone,
                        precision="date", source_date=day)
    return Deadline(at=instant(value), timezone=timezone)


def _minutes(value: object) -> int | None:
    if value == "":
        return None
    if type(value) is int and 0 <= value <= 1_000_000:
        return value
    if isinstance(value, str) and re.fullmatch(r"(?:0|[1-9][0-9]{0,6})", value):
        number = int(value)
        if number <= 1_000_000:
            return number
    raise ValueError("remaining_minutes_invalid")


def normalize_values(data: dict, *, first: int, last: int, timezone: str
                     ) -> tuple[tuple[Assignment, ...], tuple[SourceIssue, ...]]:
    """Pure normalization of one complete ValueRange (not paginated prose).

    Only configured rectangle is the dataset. Its last row is an overflow guard;
    nonblank guard means possibly truncated, even when valid rows were read.
    """
    issues: list[SourceIssue] = []
    rows = data.get("values", [])
    if data.get("majorDimension") != "ROWS" or not isinstance(rows, list):
        return (), (SourceIssue(code="sheet_response_invalid"),)
    if len(rows) > last - first + 1:
        return (), (SourceIssue(code="sheet_response_out_of_bounds"),)
    if not rows or rows[0] != list(HEADERS):
        return (), (SourceIssue(code="sheet_headers_invalid", item_ref=f"row-{first}"),)
    seen: set[str] = set()
    duplicates: set[str] = set()
    assignments: dict[str, Assignment] = {}
    for offset, raw in enumerate(rows[1:], 1):
        row_ref = f"row-{first + offset}"
        # Register a valid stable ID before any other row rejection, including
        # formulas, extra cells and overflow. Never evaluate the other cells.
        if isinstance(raw, list) and raw:
            try:
                stable_id = _IDENTIFIER.validate_python(raw[0])
            except ValidationError:
                pass
            else:
                if stable_id in seen:
                    duplicates.add(stable_id)
                    issues.append(SourceIssue(code="duplicate_assignment_id", item_ref=row_ref))
                seen.add(stable_id)
        if not isinstance(raw, list) or len(raw) > 6:
            issues.append(SourceIssue(code="sheet_row_shape_invalid", item_ref=row_ref))
            continue
        if not raw or all(cell == "" for cell in raw):
            continue
        if first + offset == last:
            issues.append(SourceIssue(code="sheet_range_truncated", item_ref=row_ref))
            continue
        if any(isinstance(cell, str) and cell.lstrip().startswith("=") for cell in raw):
            issues.append(SourceIssue(code="sheet_formula_forbidden", item_ref=row_ref))
            continue
        values = raw + [""] * (6 - len(raw))
        identity, course, title, deadline, effort, status = values
        try:
            due = _deadline(deadline, timezone)
        except ValueError:
            issues.append(SourceIssue(code="deadline_invalid", item_ref=row_ref))
            due = None
        try:
            remaining = _minutes(effort)
        except ValueError:
            issues.append(SourceIssue(code="remaining_minutes_invalid", item_ref=row_ref))
            remaining = None
        try:
            normalized = NormalizedAssignment(assignment_id=identity, course=course, title=title,
                deadline=due, remaining_minutes=remaining, status=status, source_row=row_ref)
            projected = normalized.project()  # Includes T03 encoded-title size bound.
        except ValidationError:
            issues.append(SourceIssue(code="assignment_fields_invalid", item_ref=row_ref))
            continue
        if due is None and deadline == "":
            issues.append(SourceIssue(code="needs_input_deadline", item_ref=row_ref))
        if remaining is None and effort == "":
            issues.append(SourceIssue(code="needs_input_remaining_minutes", item_ref=row_ref))
        if status == "done" and remaining != 0:
            issues.append(SourceIssue(code="done_requires_explicit_zero", item_ref=row_ref))
        assignments[identity] = projected
    for identity in duplicates:
        assignments.pop(identity, None)
    return tuple(assignments[key] for key in sorted(assignments)), tuple(issues)


class AssignmentsReader:
    """Server-created domain service; T10 wraps read_assignments with T03 DTOs.

    Clock is a server dependency, not an argument to the public tool. No Sheet
    or user IDs can be supplied by the model. Synchronous SDK work runs outside
    the async event loop, and before any repository transaction begins.
    """

    def __init__(self, clients: GoogleWorkspaceClients, snapshots: SnapshotStore, *,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC),
                 request_context: Callable[[], TrustedRequestContext] | None = None) -> None:
        self.clients = clients
        self.snapshots = snapshots
        self.clock = clock
        self._request_context = request_context
        self.first, self.last = _bounds(clients.policy.config.student.assignments_a1_range)

    async def read(self, context: TrustedContext | TrustedRequestContext,
                   horizon: Interval) -> AssignmentsSnapshot:
        """T03 port bridge using the same current-scope identity rule as T05.

        Startup may inject a server-only resolver. A DTO is accepted only when
        it IS the resolved active request's DTO, never an equal parsed/copy.
        No default resolver or caller-provided resolver is part of tool input.
        """
        if isinstance(context, TrustedRequestContext):
            self.clients.policy.require_context(context)
            request = context
        else:
            if not isinstance(context, TrustedContext) or self._request_context is None:
                raise AccessDenied("server_request_context_required")
            request = self._request_context()
            self.clients.policy.require_context(request)
            if context is not request.as_port_context():
                raise AccessDenied("foreign_port_context")
        # read_assignments independently checks the MCP boundary before I/O.
        return await self.read_assignments(request, ReadAssignmentsInput(horizon=horizon))

    async def read_assignments(self, context: TrustedRequestContext,
                               request: ReadAssignmentsInput) -> AssignmentsSnapshot:
        ReadAssignmentsInput.model_validate(request)
        trusted = self.clients.policy.require_context(context)
        if context.boundary != "mcp":
            raise AccessDenied("mcp_boundary_required")
        try:
            data = await asyncio.to_thread(self.clients.sheet_read, context,
                spreadsheet_id=trusted.sheet_id, a1_range=trusted.sheet_range,
                request_factory=lambda service, sheet, area: service.spreadsheets().values().get(
                    spreadsheetId=sheet, range=area, majorDimension="ROWS",
                    valueRenderOption="FORMULA", dateTimeRenderOption="SERIAL_NUMBER"))
            assignments, issues = normalize_values(data, first=self.first, last=self.last,
                                                    timezone=trusted.timezone)
        except WorkspaceAccessError as error:
            assignments, issues = (), (SourceIssue(code=error.code),)
        snapshot = AssignmentsSnapshot(assignments=assignments, source=SourceSnapshot(
            reference=SourceReference(snapshot_id="as_" + uuid4().hex, kind="assignments",
                owner_id=trusted.owner_id, resource_id=trusted.sheet_id, run_id=trusted.run_id,
                config_revision=trusted.config_revision, read_at=instant(self.clock()),
                content_hash=assignments_content_hash(assignments)),
            timezone=trusted.timezone, complete=not issues, issues=issues,
        ))
        # Fail closed on expired authority or persistence failure; never return
        # an apparently authoritative reference before create-only commit.
        await self.snapshots.put_assignments(context, snapshot)
        return snapshot
