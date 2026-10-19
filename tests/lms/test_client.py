"""Offline API fixtures; never use a real account, secret or Moodle server."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import ValidationError

from campuspulse_recovery.lms.client import MoodleClient, MoodleError, READ_FUNCTIONS
from campuspulse_recovery.lms.models import MoodleSnapshot

SITE = "https://lms.astanait.edu.kz"
TOKEN = "fixture_token_never_a_real_secret"
USER = 123


def fixtures():
    return {
        "core_webservice_get_site_info": {"userid": USER, "functions": [{"name": n} for n in READ_FUNCTIONS]},
        "core_enrol_get_users_courses": [{"id": 10, "fullname": "<b>Algorithms</b>", "visible": 1}],
        "core_course_get_contents": [{"id": 1, "uservisible": True, "modules": [
            {"id": 101, "uservisible": True}, {"id": 102, "uservisible": True},
            {"id": 103, "uservisible": False}, {"id": 104, "uservisible": True},
        ]}],
        "mod_assign_get_assignments": {"courses": [{"id": 10, "assignments": [
            {"id": 1, "cmid": 101, "course": 10, "name": "Trees &amp; graphs", "intro": "<p>Implement DFS.</p>",
             "duedate": 1792500000, "grade": 100, "teamsubmission": 0},
            {"id": 2, "cmid": 102, "course": 10, "name": "No deadline", "duedate": 0, "grade": 10},
            {"id": 3, "cmid": 103, "course": 10, "name": "Hidden", "duedate": 0, "grade": 10},
        ]}], "warnings": []},
        "mod_assign_get_submission_status": {"lastattempt": {"submission": {"userid": USER, "status": "submitted"},
            "extensionduedate": 1792600000}, "warnings": []},
        "mod_quiz_get_quizzes_by_courses": {"quizzes": [{"id": 4, "coursemodule": 104, "course": 10,
            "name": "Quiz one", "timeclose": 1792700000, "grade": 20}], "warnings": []},
        "mod_quiz_get_user_attempts": {"attempts": [{"id": 12, "quiz": 4, "userid": USER, "state": "finished"}], "warnings": []},
        "gradereport_user_get_grade_items": {"usergrades": [{"userid": USER, "courseid": 10, "gradeitems": [
            {"itemtype": "course", "graderaw": 65.5, "gradeformatted": "65.50", "grademax": 100},
            {"itemtype": "mod", "itemmodule": "assign", "iteminstance": 1, "itemnumber": 0,
             "graderaw": 0, "gradeformatted": "0.00", "grademax": 100},
            {"itemtype": "mod", "itemmodule": "assign", "iteminstance": 2, "itemnumber": 0,
             "graderaw": None, "gradeformatted": "-", "grademax": 10},
            {"itemtype": "mod", "itemmodule": "quiz", "iteminstance": 4, "itemnumber": 0,
             "graderaw": 15, "gradeformatted": "15.00", "grademax": 20, "gradeishidden": True},
        ]}], "warnings": []},
    }


def fetch(payloads=None, *, response_override=None, token=TOKEN):
    data = fixtures() if payloads is None else payloads
    requests = []

    def handler(request):
        body = parse_qs(request.content.decode())
        requests.append((request, body))
        function = body["wsfunction"][0]
        if response_override is not None:
            override = response_override(function, request, body)
            if override is not None:
                return override
        return httpx.Response(200, json=deepcopy(data[function]))

    result = asyncio.run(MoodleClient(SITE, token, USER, transport=httpx.MockTransport(handler)).fetch())
    return result, requests


def test_snapshot_has_actual_tasks_grades_utc_overrides_and_safe_links():
    result, requests = fetch()
    assert result.site_url == SITE
    assert result.user_id == USER
    assert result.fetched_at.tzinfo == UTC
    assert result.courses[0].title == "Algorithms"
    assert result.courses[0].grade_value == 65.5
    assert [task.id for task in result.tasks] == ["assign:1", "assign:2", "quiz:4"]
    task = result.tasks[0]
    assert task.title == "Trees & graphs"
    assert task.description == "Implement DFS."
    assert task.due_at == datetime.fromtimestamp(1792600000, UTC)
    assert task.grade_value == 0 and task.grade_display == "0.00"
    assert task.submission_status == "graded"
    assert task.url == SITE + "/mod/assign/view.php?id=101"
    assert result.tasks[1].grade_value is None
    assert result.tasks[1].grade_display is None
    assert result.tasks[2].grade_value is None
    assert result.tasks[2].submission_status == "submitted"
    for request, body in requests:
        assert request.method == "POST"
        assert not request.url.query
        assert TOKEN not in str(request.url)
        assert body["wstoken"] == [TOKEN]
        assert body["moodlewsrestformat"] == ["json"]
        assert body["wsfunction"][0] in READ_FUNCTIONS
        if body["wsfunction"][0] in ("mod_assign_get_assignments", "mod_quiz_get_quizzes_by_courses"):
            assert body["courseids[0]"] == ["10"]
        if "userid" in body:
            assert body["userid"] == [str(USER)]
    assert TOKEN not in result.model_dump_json()


def test_zero_timestamp_means_no_deadline_and_missing_submission_is_todo():
    payloads = fixtures()
    payloads["mod_assign_get_submission_status"] = {"lastattempt": {"extensionduedate": 0}}
    result, _ = fetch(payloads)
    assert result.tasks[1].due_at is None
    assert result.tasks[1].submission_status == "todo"


def test_plain_text_discards_executable_html_but_preserves_literal_text():
    payloads = fixtures()
    item = payloads["mod_assign_get_assignments"]["courses"][0]["assignments"][0]
    item["intro"] = '<script>stolen()</script><p>Solve &lt;x&gt; &amp; y</p><img src=x onerror=bad()>'
    item["activity"] = "<div>Then submit.</div><style>.hidden{}</style>"
    result, _ = fetch(payloads)
    assert result.tasks[0].description == "Solve <x> & y\nThen submit."
    assert "stolen" not in result.model_dump_json()


@pytest.mark.parametrize("url", ["http://lms.astanait.edu.kz", "https://127.0.0.1", "https://localhost",
    "https://user:password@lms.astanait.edu.kz", SITE + "?wstoken=secret", SITE + "/fake", SITE + "#x"])
def test_rejects_unsafe_or_untrusted_endpoint_shape(url):
    with pytest.raises(ValueError):
        MoodleClient(url, TOKEN, USER)


def test_wrong_token_account_fails_before_any_course_requests():
    payloads = fixtures()
    payloads["core_webservice_get_site_info"]["userid"] = 999
    with pytest.raises(MoodleError, match="identity_mismatch"):
        fetch(payloads)


def test_missing_required_function_fails_explicitly():
    payloads = fixtures()
    payloads["core_webservice_get_site_info"]["functions"] = []
    with pytest.raises(MoodleError, match="unsupported"):
        fetch(payloads)


@pytest.mark.parametrize(("error", "expected"), [("invalidtoken", "auth_failed"), ("accessexception", "unsupported"),
    ("unknownerror", "unavailable")])
def test_errors_are_safe_and_discard_upstream_messages(error, expected):
    payloads = fixtures()
    payloads["mod_assign_get_submission_status"] = {"exception": "moodle_exception", "errorcode": error,
        "message": "Echoed secret: " + TOKEN, "debuginfo": "private account data"}
    with pytest.raises(MoodleError) as failure:
        fetch(payloads)
    assert failure.value.code == expected
    assert str(failure.value) == expected
    assert TOKEN not in repr(failure.value)


def test_redirect_is_not_followed_and_never_forwards_token():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(302, headers={"Location": "https://attacker.example/collect"})

    with pytest.raises(MoodleError, match="unavailable"):
        asyncio.run(MoodleClient(SITE, TOKEN, USER, transport=httpx.MockTransport(handler)).fetch())
    assert len(seen) == 1
    assert seen[0].url.host == "lms.astanait.edu.kz"


@pytest.mark.parametrize("payload", [b"<html>Login required</html>", b"{" , b"[]", b"null"])
def test_non_api_or_invalid_response_is_not_empty_success(payload):
    with pytest.raises(MoodleError, match="invalid_response"):
        fetch(response_override=lambda *_: httpx.Response(200, content=payload))


def test_failure_or_warning_in_one_course_rejects_whole_snapshot():
    payloads = fixtures()
    payloads["gradereport_user_get_grade_items"]["warnings"] = [{"warningcode": "1", "message": "no access"}]
    with pytest.raises(MoodleError, match="unavailable"):
        fetch(payloads)


def test_course_disabling_grades_still_syncs_tasks_without_published_marks():
    payloads = fixtures()
    payloads["core_enrol_get_users_courses"][0]["showgrades"] = False
    result, requests = fetch(payloads)
    assert result.courses[0].grade_value is None
    assert all(task.grade_value is None and task.grade_display is None for task in result.tasks)
    assert "gradereport_user_get_grade_items" not in {body["wsfunction"][0] for _, body in requests}


def test_only_confirmed_invisible_module_warnings_may_be_omitted():
    payloads = fixtures()
    payloads["mod_assign_get_assignments"]["warnings"] = [{"item": "module", "itemid": 103, "warningcode": "1"}]
    result, _ = fetch(payloads)
    assert "assign:3" not in {task.id for task in result.tasks}
    payloads["mod_assign_get_assignments"]["warnings"][0]["itemid"] = 101
    with pytest.raises(MoodleError, match="unavailable"):
        fetch(payloads)


def test_grade_report_cannot_substitute_another_student():
    payloads = fixtures()
    payloads["gradereport_user_get_grade_items"]["usergrades"][0]["userid"] = 999
    with pytest.raises(MoodleError, match="identity_mismatch"):
        fetch(payloads)


def test_quiz_attempt_cannot_substitute_another_student():
    payloads = fixtures()
    payloads["mod_quiz_get_user_attempts"]["attempts"][0]["userid"] = 999
    with pytest.raises(MoodleError, match="identity_mismatch"):
        fetch(payloads)


def test_team_submission_status_uses_group_record():
    payloads = fixtures()
    item = payloads["mod_assign_get_assignments"]["courses"][0]["assignments"][1]
    item["teamsubmission"] = 1
    payloads["mod_assign_get_submission_status"] = {"lastattempt": {
        "submission": {"userid": USER, "status": "new"},
        "teamsubmission": {"userid": 0, "status": "submitted"}, "extensionduedate": 0}}
    result, _ = fetch(payloads)
    assert result.tasks[1].submission_status == "submitted"


def test_empty_enrolment_is_valid_without_fetching_other_students_courses():
    payloads = fixtures()
    payloads["core_enrol_get_users_courses"] = []
    result, requests = fetch(payloads)
    assert not result.tasks and not result.courses
    assert len(requests) == 2


def test_reopened_assignment_retains_old_score_but_still_needs_work():
    payloads = fixtures()
    payloads["mod_assign_get_submission_status"]["lastattempt"]["submission"]["status"] = "reopened"
    result, _ = fetch(payloads)
    assert result.tasks[0].grade_value == 0
    assert result.tasks[0].submission_status == "todo"


def test_new_quiz_attempt_is_not_complete_due_to_old_published_grade():
    payloads = fixtures()
    payloads["gradereport_user_get_grade_items"]["usergrades"][0]["gradeitems"][-1]["gradeishidden"] = False
    payloads["mod_quiz_get_user_attempts"]["attempts"].append({
        "id": 13, "quiz": 4, "userid": USER, "state": "inprogress"})
    result, _ = fetch(payloads)
    assert result.tasks[-1].grade_value == 15
    assert result.tasks[-1].submission_status == "draft"


def test_unbounded_response_is_rejected(monkeypatch):
    import campuspulse_recovery.lms.client as module
    monkeypatch.setattr(module, "MAX_RESPONSE_BYTES", 20)
    with pytest.raises(MoodleError, match="invalid_response"):
        fetch()


def test_timeout_is_safe_failure():
    def response(function, request, body):
        raise httpx.ReadTimeout("do not expose " + TOKEN, request=request)
    with pytest.raises(MoodleError, match="unavailable") as failure:
        fetch(response_override=response)
    assert str(failure.value) == "unavailable"


def test_model_rejects_cached_cross_course_or_external_links():
    result, _ = fetch()
    data = result.model_dump(mode="json")
    data["tasks"][0]["url"] = "https://attacker.example/mod/assign/view.php?id=101"
    with pytest.raises(ValidationError):
        MoodleSnapshot.model_validate(data)
    data = result.model_dump(mode="json")
    data["tasks"][0]["course_id"] = 999
    with pytest.raises(ValidationError):
        MoodleSnapshot.model_validate(data)
