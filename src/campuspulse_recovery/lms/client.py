"""Bounded Moodle REST reads using a student's server-held service token.

API shapes follow Moodle 4.5's published externallib/classes/external sources.
No login scraping, mutation functions, file downloads, or automatic retries.
"""

from __future__ import annotations

import asyncio
import json
import math
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any, Literal

import httpx
from pydantic import ValidationError

from .models import MoodleCourse, MoodleSnapshot, MoodleTask, normalize_site_url

READ_FUNCTIONS = frozenset({
    "core_webservice_get_site_info", "core_enrol_get_users_courses",
    "core_course_get_contents", "mod_assign_get_assignments",
    "mod_assign_get_submission_status", "gradereport_user_get_grade_items",
    "mod_quiz_get_quizzes_by_courses", "mod_quiz_get_user_attempts",
})
MAX_RESPONSE_BYTES = 2_000_000
MAX_FETCH_SECONDS = 90
MAX_COURSES = 100
MAX_TASKS = 500
ErrorCode = Literal["auth_failed", "identity_mismatch", "unavailable", "invalid_response", "unsupported"]


class MoodleError(RuntimeError):
    """Safe error code only; never preserve upstream body, token or request."""

    def __init__(self, code: ErrorCode):
        if code not in {"auth_failed", "identity_mismatch", "unavailable", "invalid_response", "unsupported"}:
            raise ValueError("invalid Moodle error code")
        self.code = code
        super().__init__(code)


def _obj(value: Any) -> dict:
    if not isinstance(value, dict):
        raise MoodleError("invalid_response")
    return value


def _list(value: Any, limit: int = 2000) -> list:
    if not isinstance(value, list) or len(value) > limit:
        raise MoodleError("invalid_response")
    return value


def _id(value: Any) -> int:
    if type(value) is not int or value < 1:
        raise MoodleError("invalid_response")
    return value


def _flag(value: Any) -> bool:
    if type(value) is bool:
        return value
    if type(value) is int and value in (0, 1):
        return bool(value)
    raise MoodleError("invalid_response")


def _number(value: Any) -> float | None:
    if value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value):
        raise MoodleError("invalid_response")
    return float(value)


def _timestamp(value: Any) -> datetime | None:
    if type(value) is not int or value < 0:
        raise MoodleError("invalid_response")
    if not value:
        return None
    try:
        return datetime.fromtimestamp(value, UTC)
    except (ValueError, OverflowError, OSError):
        raise MoodleError("invalid_response") from None


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.suppressed: list[str] = []

    def handle_starttag(self, tag: str, attrs):
        if tag in ("script", "style", "template", "iframe", "object"):
            self.suppressed.append(tag)
        if not self.suppressed and tag in ("p", "br", "div", "li", "tr", "h1", "h2", "h3"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str):
        if self.suppressed and tag == self.suppressed[-1]:
            self.suppressed.pop()
        if not self.suppressed and tag in ("p", "div", "li", "tr"):
            self.parts.append("\n")

    def handle_data(self, data: str):
        if not self.suppressed:
            self.parts.append(data)


def _text(value: Any, *, max_length: int = 512, required: bool = False) -> str:
    if not isinstance(value, str) or len(value) > 200_000:
        raise MoodleError("invalid_response")
    parser = _PlainText()
    parser.feed(value)
    parser.close()
    text = "\n".join(" ".join(line.split()) for line in "".join(parser.parts).splitlines()).strip()
    text = "\n".join(line for line in text.splitlines() if line)
    if required and not text:
        raise MoodleError("invalid_response")
    if len(text) > max_length:
        text = text[:max_length - 1] + "…"
    return text


def _grade(item: dict | None, maximum: Any = None) -> dict:
    result = {"grade_display": None, "grade_value": None, "grade_max": None}
    if maximum is not None:
        maximum = _number(maximum)
        result["grade_max"] = maximum if maximum is not None and maximum >= 0 else None
    if item is None:
        return result
    if "grademax" in item:
        maximum = _number(item["grademax"])
        result["grade_max"] = maximum if maximum is not None and maximum >= 0 else None
    # Hidden/recalculating grades are not published, even if raw values were returned.
    if any(_flag(item.get(key, False)) for key in ("gradeishidden", "gradehiddenbydate", "gradeneedsupdate")):
        return result
    raw = _number(item.get("graderaw"))
    display = _text(item.get("gradeformatted", ""))
    if display in ("", "-", "–", "—"):
        display = None
    result.update(grade_value=raw, grade_display=display)
    return result


class MoodleClient:
    """Construct only from trusted deployment/account config, not browser input."""

    def __init__(self, site_url: str, token: str, expected_user_id: int, *, transport=None):
        self.site_url = normalize_site_url(site_url)
        if not isinstance(token, str) or not token or len(token) > 512 or any(c.isspace() for c in token):
            raise ValueError("invalid Moodle token")
        if type(expected_user_id) is not int or expected_user_id < 1:
            raise ValueError("invalid Moodle account")
        self._token = token
        self.expected_user_id = expected_user_id
        self._transport = transport

    async def fetch(self) -> MoodleSnapshot:
        """Return a complete snapshot or safe failure; close all connections."""
        started = datetime.now(UTC)
        try:
            async with asyncio.timeout(MAX_FETCH_SECONDS):
                async with httpx.AsyncClient(
                    transport=self._transport, timeout=httpx.Timeout(12, connect=5),
                    follow_redirects=False, trust_env=False,
                    limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
                ) as http:
                    return await self._fetch(http, asyncio.Semaphore(4), started)
        except MoodleError:
            raise
        except ExceptionGroup as group:
            failures: list[BaseException] = []

            def collect(error: BaseException):
                if isinstance(error, BaseExceptionGroup):
                    for nested in error.exceptions:
                        collect(nested)
                else:
                    failures.append(error)

            collect(group)
            for failure in failures:
                if isinstance(failure, MoodleError):
                    raise MoodleError(failure.code) from None
            if any(isinstance(failure, (httpx.HTTPError, TimeoutError)) for failure in failures):
                raise MoodleError("unavailable") from None
            raise MoodleError("invalid_response") from None
        except (httpx.HTTPError, TimeoutError):
            raise MoodleError("unavailable") from None
        except (ValueError, TypeError, KeyError, ValidationError, RecursionError):
            raise MoodleError("invalid_response") from None

    async def _call(self, http: httpx.AsyncClient, semaphore: asyncio.Semaphore,
                    function: str, **parameters) -> Any:
        if function not in READ_FUNCTIONS:
            raise MoodleError("unsupported")
        data = {"wstoken": self._token, "wsfunction": function, "moodlewsrestformat": "json"}
        for key, value in parameters.items():
            if isinstance(value, (tuple, list)):
                data.update({f"{key}[{i}]": str(entry) for i, entry in enumerate(value)})
            else:
                data[key] = str(value)
        async with semaphore:
            async with http.stream("POST", self.site_url + "/webservice/rest/server.php", data=data) as response:
                if response.status_code in (401, 403):
                    raise MoodleError("auth_failed")
                if response.status_code != 200:
                    raise MoodleError("unavailable")
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > MAX_RESPONSE_BYTES:
                        raise MoodleError("invalid_response")
        try:
            payload = json.loads(content)
        except (ValueError, UnicodeError):
            raise MoodleError("invalid_response") from None
        if isinstance(payload, dict):
            if payload.get("exception") or payload.get("errorcode"):
                code = payload.get("errorcode")
                if code in {"invalidtoken", "expiredtoken", "invalidlogin", "userdeleted", "usersuspended"}:
                    raise MoodleError("auth_failed")
                if code in {"accessexception", "wsfunctionnotavailable", "servicenotavailable", "webservicedisabled",
                            "nopermissions", "nopermissiontoviewgrades"}:
                    raise MoodleError("unsupported")
                raise MoodleError("unavailable")
            if _list(payload.get("warnings", [])) and function != "mod_assign_get_assignments":
                # Never replace the durable cache with an apparently complete partial read.
                raise MoodleError("unavailable")
        return payload

    async def _fetch(self, http, semaphore, started: datetime) -> MoodleSnapshot:
        async def call(function: str, **parameters):
            return await self._call(http, semaphore, function, **parameters)

        info = _obj(await call("core_webservice_get_site_info"))
        if _id(info.get("userid")) != self.expected_user_id:
            raise MoodleError("identity_mismatch")
        available = {_obj(item).get("name") for item in _list(info.get("functions"))}
        if not READ_FUNCTIONS.issubset(available):
            raise MoodleError("unsupported")
        raw_courses = _list(await call("core_enrol_get_users_courses", userid=self.expected_user_id), MAX_COURSES)
        courses: dict[int, MoodleCourse] = {}
        grades_enabled: set[int] = set()
        for raw in raw_courses:
            raw = _obj(raw)
            cid = _id(raw.get("id"))
            if cid in courses:
                raise MoodleError("invalid_response")
            if "visible" in raw and not _flag(raw["visible"]):
                continue
            courses[cid] = MoodleCourse(id=cid, title=_text(raw.get("fullname"), required=True))
            if _flag(raw.get("showgrades", True)):
                grades_enabled.add(cid)
        if not courses:
            return MoodleSnapshot(site_url=self.site_url, user_id=self.expected_user_id,
                                  fetched_at=started, courses=(), tasks=())

        # Structured concurrency cancels sibling reads when any required read fails.
        async with asyncio.TaskGroup() as group:
            assignment_future = group.create_task(call("mod_assign_get_assignments", courseids=tuple(courses)))
            quiz_future = group.create_task(call("mod_quiz_get_quizzes_by_courses", courseids=tuple(courses)))
            content_futures = {cid: group.create_task(call("core_course_get_contents", courseid=cid,
                **{"options[0][name]": "excludecontents", "options[0][value]": "1",
                   "options[1][name]": "includestealthmodules", "options[1][value]": "1"})) for cid in courses}
            grade_futures = {cid: group.create_task(call("gradereport_user_get_grade_items", courseid=cid,
                userid=self.expected_user_id)) for cid in grades_enabled}
        visible_modules: dict[int, set[int]] = {}
        grade_items: dict[tuple[int, str, int], dict] = {}
        for cid, course in list(courses.items()):
            visible_modules[cid] = set()
            for section in _list(content_futures[cid].result()):
                section = _obj(section)
                section_visible = _flag(section.get("uservisible", section.get("visible", True)))
                for module in _list(section.get("modules")):
                    module = _obj(module)
                    if section_visible and _flag(module.get("uservisible", module.get("visible", True))):
                        visible_modules[cid].add(_id(module.get("id")))
            # A course with grades intentionally disabled has no published scores.
            # Asking its grade API would correctly produce nopermissiontoviewgrades.
            if cid not in grades_enabled:
                continue
            reports = _list(_obj(grade_futures[cid].result()).get("usergrades"), 1)
            if len(reports) != 1:
                raise MoodleError("invalid_response")
            report = _obj(reports[0])
            if report.get("userid") != self.expected_user_id or report.get("courseid") != cid:
                raise MoodleError("identity_mismatch")
            for item in _list(report.get("gradeitems")):
                item = _obj(item)
                if item.get("itemtype") == "course":
                    course = MoodleCourse(id=course.id, title=course.title, **_grade(item))
                elif item.get("itemtype") == "mod" and item.get("itemmodule") in ("assign", "quiz"):
                    # Outcomes/additional quiz grade items must not overwrite the main grade.
                    if item.get("itemnumber", 0) not in (None, 0) or item.get("outcomeid", 0) not in (None, 0):
                        continue
                    key = (cid, item["itemmodule"], _id(item.get("iteminstance")))
                    if key in grade_items:
                        raise MoodleError("invalid_response")
                    grade_items[key] = item
            courses[cid] = course

        pending: list[tuple[dict, str, int, int]] = []
        returned_courses = set()
        assignment_payload = _obj(assignment_future.result())
        visible_ids = set().union(*visible_modules.values())
        for warning in _list(assignment_payload.get("warnings", [])):
            warning = _obj(warning)
            # Moodle reports modules unavailable to this student as warnings.
            # Confirm absence from student-visible course contents; all other
            # warnings remain incomplete reads and must retain the old cache.
            if not (warning.get("item") == "module" and str(warning.get("warningcode")) == "1"
                    and _id(warning.get("itemid")) not in visible_ids):
                raise MoodleError("unavailable")
        for course in _list(assignment_payload.get("courses"), MAX_COURSES):
            course = _obj(course)
            cid = _id(course.get("id"))
            if cid not in courses or cid in returned_courses:
                raise MoodleError("invalid_response")
            returned_courses.add(cid)
            for item in _list(course.get("assignments"), MAX_TASKS):
                item = _obj(item)
                if item.get("course") != cid:
                    raise MoodleError("invalid_response")
                cmid = _id(item.get("cmid"))
                if cmid in visible_modules[cid]:
                    pending.append((item, "assign", cid, cmid))
        if returned_courses != set(courses):
            raise MoodleError("invalid_response")
        for item in _list(_obj(quiz_future.result()).get("quizzes"), MAX_TASKS):
            item = _obj(item)
            cid, cmid = _id(item.get("course")), _id(item.get("coursemodule"))
            if cid not in courses:
                raise MoodleError("invalid_response")
            if cmid in visible_modules[cid]:
                pending.append((item, "quiz", cid, cmid))
        if len(pending) > MAX_TASKS:
            raise MoodleError("invalid_response")
        async with asyncio.TaskGroup() as group:
            status_futures = []
            for item, kind, _, _ in pending:
                task_id = _id(item.get("id"))
                if kind == "assign":
                    request = call("mod_assign_get_submission_status", assignid=task_id, userid=self.expected_user_id)
                else:
                    request = call("mod_quiz_get_user_attempts", quizid=task_id, userid=self.expected_user_id,
                                   status="all", includepreviews=0)
                status_futures.append(group.create_task(request))
        tasks = []
        for (item, kind, cid, cmid), future in zip(pending, status_futures, strict=True):
            task_id = _id(item.get("id"))
            status = _obj(future.result())
            grade = _grade(grade_items.get((cid, kind, task_id)), item.get("grade"))
            due = _timestamp(item.get("duedate" if kind == "assign" else "timeclose"))
            submission = "unknown"
            active_attempt = False
            if kind == "assign" and "lastattempt" in status:
                attempt = _obj(status["lastattempt"])
                extension = _timestamp(attempt.get("extensionduedate", 0))
                if extension is not None and (due is None or extension > due):
                    due = extension
                is_team = _flag(item.get("teamsubmission", 0))
                actual = _obj(attempt.get("teamsubmission" if is_team else "submission", {}))
                valid_users = (0, self.expected_user_id) if is_team else (self.expected_user_id,)
                if actual and actual.get("userid", self.expected_user_id) not in valid_users:
                    raise MoodleError("identity_mismatch")
                submission = {None: "todo", "new": "todo", "reopened": "todo", "draft": "draft",
                              "submitted": "submitted"}.get(actual.get("status"), "unknown")
                active_attempt = actual.get("status") in ("reopened", "draft")
            elif kind == "quiz":
                attempts = [_obj(a) for a in _list(status.get("attempts"))]
                for attempt in attempts:
                    if attempt.get("userid") != self.expected_user_id or attempt.get("quiz") != task_id:
                        raise MoodleError("identity_mismatch")
                states = {a.get("state") for a in attempts}
                submission = ("draft" if states.intersection({"inprogress", "overdue"}) else
                              "submitted" if "finished" in states else "todo" if not states else "unknown")
                active_attempt = submission == "draft"
            if not active_attempt and (grade["grade_value"] is not None or grade["grade_display"] is not None):
                submission = "graded"
            intro = item.get("intro", "")
            activity = item.get("activity", "")
            if not isinstance(intro, str) or not isinstance(activity, str):
                raise MoodleError("invalid_response")
            description = _text(intro + "\n" + activity, max_length=12000)
            tasks.append(MoodleTask(
                id=f"{kind}:{task_id}", course_id=cid, course_title=courses[cid].title,
                title=_text(item.get("name"), required=True), description=description, due_at=due,
                submission_status=submission, url=f"{self.site_url}/mod/{kind}/view.php?id={cmid}",
                kind="assignment" if kind == "assign" else "quiz", **grade,
            ))
        return MoodleSnapshot(site_url=self.site_url, user_id=self.expected_user_id, fetched_at=started,
                              courses=tuple(courses.values()), tasks=tuple(tasks))
