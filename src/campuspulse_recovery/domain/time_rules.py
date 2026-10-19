"""Pure, fail-closed time normalization. No clock, I/O, or implicit local zone."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_RFC3339 = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])$"
)
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def iana_zone(value: str) -> str:
    if not isinstance(value, str) or value != value.strip() or not value:
        raise ValueError("an explicit IANA timezone is required")
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("unknown IANA timezone") from exc
    return value


def instant(value: datetime | str) -> datetime:
    """Accept aware datetime or offset-bearing RFC3339; return UTC."""
    if isinstance(value, str):
        if not _RFC3339.fullmatch(value) or value.endswith("-00:00"):
            raise ValueError("expected RFC3339 with explicit offset and seconds")
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    try:
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("naive/invalid datetime is not an instant")
        # Also reject manufactured nonexistent ZoneInfo local times.
        normalized = value.astimezone(UTC)
        if normalized.astimezone(value.tzinfo).replace(tzinfo=None) != value.replace(tzinfo=None):
            raise ValueError("nonexistent local time")
    except OverflowError as exc:
        raise ValueError("datetime normalization exceeds supported range") from exc
    return normalized


def iso_date(value: date | str) -> date:
    if isinstance(value, datetime):
        raise ValueError("datetime is not a date-only value")
    if isinstance(value, str):
        if not _ISO_DATE.fullmatch(value):
            raise ValueError("expected ISO YYYY-MM-DD")
        value = date.fromisoformat(value)
    if not isinstance(value, date):
        raise ValueError("expected date")
    return value


def local_time(value: datetime, timezone: str) -> datetime:
    """Resolve an unzoned local value only when the zone gives one instant."""
    if not isinstance(value, datetime) or value.tzinfo is not None:
        raise ValueError("expected naive local datetime for explicit resolution")
    zone = ZoneInfo(iana_zone(timezone))
    candidates = set()
    try:
        for fold in (0, 1):
            candidate = value.replace(tzinfo=zone, fold=fold).astimezone(UTC)
            if candidate.astimezone(zone).replace(tzinfo=None) == value:
                candidates.add(candidate)
    except OverflowError as exc:
        raise ValueError("local time normalization exceeds supported range") from exc
    if len(candidates) != 1:
        raise ValueError("ambiguous or nonexistent local time; request explicit offset")
    return candidates.pop()


def date_deadline(value: date | str, timezone: str) -> datetime:
    """Date-only work must finish <= midnight starting the following local day."""
    try:
        next_day = iso_date(value) + timedelta(days=1)
    except OverflowError as exc:
        raise ValueError("date deadline boundary exceeds supported range") from exc
    return local_time(datetime.combine(next_day, time.min), timezone)


def all_day_bounds(start: date | str, end: date | str, timezone: str) -> tuple[datetime, datetime]:
    """Google all-day end.date is exclusive, including across DST transitions."""
    first, last = iso_date(start), iso_date(end)
    if last <= first:
        raise ValueError("all-day end must be strictly after start")
    return (
        local_time(datetime.combine(first, time.min), timezone),
        local_time(datetime.combine(last, time.min), timezone),
    )


def interval_bounds(start: datetime | str, end: datetime | str) -> tuple[datetime, datetime]:
    first, last = instant(start), instant(end)
    if last <= first:
        raise ValueError("interval must have positive elapsed duration")
    return first, last


def overlaps(start: datetime, end: datetime, other_start: datetime, other_end: datetime) -> bool:
    first, last = interval_bounds(start, end)
    other_first, other_last = interval_bounds(other_start, other_end)
    return first < other_last and other_first < last


def clip(start: datetime, end: datetime, lower: datetime, upper: datetime) -> tuple[datetime, datetime] | None:
    first, last = interval_bounds(start, end)
    low, high = interval_bounds(lower, upper)
    return (max(first, low), min(last, high)) if first < high and low < last else None


def planning_horizon(now: datetime | str) -> tuple[datetime, datetime]:
    """Ceil to UTC quarter-hour (exact boundaries unchanged), then 72 elapsed h."""
    current = instant(now)
    floor = current.replace(minute=(current.minute // 15) * 15, second=0, microsecond=0)
    try:
        start = floor if current == floor else floor + timedelta(minutes=15)
        return start, start + timedelta(hours=72)
    except OverflowError as exc:
        raise ValueError("planning horizon exceeds supported range") from exc


def required_minutes(remaining_minutes: int | None) -> int:
    if type(remaining_minutes) is not int or remaining_minutes < 0:
        raise ValueError("explicit nonnegative remaining_minutes required; unknown is not zero")
    return ((remaining_minutes + 14) // 15) * 15


def block_phase(start: datetime, end: datetime, now: datetime) -> str:
    first, last = interval_bounds(start, end)
    current = instant(now)
    return "past" if last <= current else "in_progress" if first <= current else "future"
