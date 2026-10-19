"""Student-visible Moodle values. These DTOs never contain credentials."""

from __future__ import annotations

import ipaddress
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, StringConstraints, field_validator, model_validator

from ..domain.models import Contract, Instant, PositiveInt

Title = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=512)]
Score = Annotated[float, Field(allow_inf_nan=False)]


def normalize_site_url(value: str) -> str:
    """Only a configured public HTTPS origin; never a user-supplied request URL."""
    if not isinstance(value, str) or value != value.strip() or len(value) > 253:
        raise ValueError("invalid Moodle origin")
    parsed = urlsplit(value)
    hostname = parsed.hostname or ""
    if (parsed.scheme != "https" or not hostname or "." not in hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.path not in ("", "/")
            or parsed.port not in (None, 443) or "\\" in value
            or hostname.endswith((".localhost", ".local", ".internal"))):
        raise ValueError("invalid Moodle origin")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise ValueError("Moodle origin must use a public DNS hostname")
    if not all(part and all(c.isascii() and (c.isalnum() or c == "-") for c in part)
               and not part.startswith("-") and not part.endswith("-") for part in hostname.split(".")):
        raise ValueError("invalid Moodle hostname")
    return "https://" + hostname.lower()


class MoodleCourse(Contract):
    id: PositiveInt
    title: Title
    grade_display: Annotated[str, StringConstraints(max_length=512)] | None = None
    grade_value: Score | None = None
    grade_max: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None


class MoodleTask(Contract):
    id: Annotated[str, StringConstraints(strict=True, pattern=r"^(assign|quiz):[1-9][0-9]*$")]
    course_id: PositiveInt
    course_title: Title
    title: Title
    description: Annotated[str, StringConstraints(strict=True, max_length=12000)] = ""
    due_at: Instant | None = None
    submission_status: Literal["todo", "draft", "submitted", "graded", "unknown"]
    grade_display: Annotated[str, StringConstraints(max_length=512)] | None = None
    grade_value: Score | None = None
    grade_max: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None
    url: Annotated[str, StringConstraints(strict=True, max_length=512)]
    kind: Literal["assignment", "quiz"]


class MoodleSnapshot(Contract):
    site_url: str
    user_id: PositiveInt
    fetched_at: Instant
    courses: Annotated[tuple[MoodleCourse, ...], Field(max_length=100)]
    tasks: Annotated[tuple[MoodleTask, ...], Field(max_length=500)]

    _origin = field_validator("site_url")(normalize_site_url)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        courses = {course.id: course for course in self.courses}
        if len(courses) != len(self.courses) or len({task.id for task in self.tasks}) != len(self.tasks):
            raise ValueError("duplicate Moodle resource")
        for task in self.tasks:
            course = courses.get(task.course_id)
            prefix = "assign" if task.kind == "assignment" else "quiz"
            if course is None or task.course_title != course.title or not task.id.startswith(prefix + ":"):
                raise ValueError("inconsistent Moodle task")
            parsed = urlsplit(task.url)
            if (parsed.scheme + "://" + parsed.netloc != self.site_url
                    or parsed.path != f"/mod/{prefix}/view.php" or parsed.fragment
                    or not parsed.query.startswith("id=") or not parsed.query[3:].isdigit()):
                raise ValueError("invalid Moodle task link")
        return self
