"""Regression fixtures checked against Moodle 4.5's official PHP sources.

These tests use no university account or external network connection.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from urllib.parse import parse_qs

import httpx

from campuspulse_recovery.lms.client import MoodleClient, READ_FUNCTIONS


def _payloads():
    return {
        "core_webservice_get_site_info": {
            "userid": 123, "functions": [{"name": name} for name in READ_FUNCTIONS],
        },
        "core_enrol_get_users_courses": [{
            "id": 10, "fullname": "Algorithms", "visible": 1, "showgrades": True,
        }],
        "core_course_get_contents": [{
            "id": 1, "uservisible": True,
            "modules": [{"id": 101, "uservisible": True}],
        }],
        "mod_assign_get_assignments": {"courses": [{"id": 10, "assignments": [{
            "id": 1, "cmid": 101, "course": 10, "name": "Trees",
            "duedate": 0, "grade": 100, "teamsubmission": 0,
        }]}], "warnings": []},
        "mod_assign_get_submission_status": {"lastattempt": {
            "submission": {"userid": 123, "status": "submitted"}, "extensionduedate": 0,
        }, "warnings": []},
        "gradereport_user_get_grade_items": {"usergrades": [{
            "userid": 123, "courseid": 10, "gradeitems": [{
                "itemtype": "mod", "itemmodule": "assign", "iteminstance": 1,
                "itemnumber": 0, "outcomeid": None,
                "graderaw": 76, "gradeformatted": "76.00", "grademax": 100,
            }],
        }], "warnings": []},
        "mod_quiz_get_quizzes_by_courses": {"quizzes": [], "warnings": []},
    }


def _fetch(payloads, *, stealth=False):
    def handler(request):
        body = parse_qs(request.content.decode())
        function = body["wsfunction"][0]
        result = deepcopy(payloads[function])
        if stealth and function == "core_course_get_contents":
            # Moodle moves accessible stealth activities out of hidden sections
            # only when includestealthmodules is explicitly requested.
            options = {
                body[f"options[{i}][name]"][0]: body[f"options[{i}][value]"][0]
                for i in range(8) if f"options[{i}][name]" in body
            }
            result = [{"id": 1, "uservisible": False, "modules": []}]
            if options.get("includestealthmodules") == "1":
                result.append({"id": -1, "modules": [{"id": 101, "uservisible": True}]})
        return httpx.Response(200, json=result)

    return asyncio.run(MoodleClient(
        "https://lms.astanait.edu.kz", "offline_fixture_token", 123,
        transport=httpx.MockTransport(handler),
    ).fetch())


def test_main_grade_with_nullable_outcomeid_remains_published():
    # grade_items.outcomeid is NULL for a regular grade. The user report and
    # external_api::clean_returnvalue preserve it; NULL does not mean outcome.
    result = _fetch(_payloads())
    assert result.tasks[0].grade_value == 76
    assert result.tasks[0].grade_display == "76.00"
    assert result.tasks[0].submission_status == "graded"


def test_course_with_no_assignments_has_valid_empty_course_entry():
    # mod_assign_external::get_assignments always appends requested courses,
    # including those whose assignmentarray is empty.
    payloads = _payloads()
    payloads["mod_assign_get_assignments"]["courses"][0]["assignments"] = []
    payloads["core_course_get_contents"] = [{"id": 1, "modules": []}]
    result = _fetch(payloads)
    assert len(result.courses) == 1
    assert not result.tasks


def test_accessible_stealth_assignment_is_included_from_hidden_section():
    result = _fetch(_payloads(), stealth=True)
    assert [task.id for task in result.tasks] == ["assign:1"]
