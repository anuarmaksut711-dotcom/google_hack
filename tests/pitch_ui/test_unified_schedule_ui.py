"""The schedule flow stays discoverable without implying Calendar execution."""
from pathlib import Path
import shutil
import subprocess

import pytest

from campuspulse_recovery.web.views.render import render_panel, render_shell
from tests.storage.test_plans import NOW, run
from tests.web.test_approval import action, login
from tests.web.test_ui_states import Fixture, Markup, render
from campuspulse_recovery.storage.plans import plan_reference
from campuspulse_recovery.web.views.app import SafeSources


@pytest.mark.parametrize("mode", ["recovery", "calendar_advice"])
def test_proposed_schedule_is_a_direct_navigation_destination_in_each_chat_configuration(mode):
    html = render_shell("Asia/Almaty", moodle_enabled=True, chat_mode=mode,
                        planning_url="/planning", google_calendar_enabled=True)
    markup = Markup(html)
    links = [node for node in markup.nodes if node["tag"] == "a"
             and node["attrs"].get("data-view") == "plan"]
    assert len(links) == 1
    assert links[0]["attrs"]["href"] == "#panel"
    assert all(not any(parent["tag"] == "details" for parent in link["parents"]) for link in links)
    assert any(link["attrs"].get("aria-label") == "Предложенное расписание" for link in links)
    assert all("hidden" not in parent["attrs"] for link in links for parent in link["parents"])
    assert any(node["tag"] == "span" and node["attrs"].get("class") == "nav-label"
               and links[0] in node["parents"] for node in markup.nodes)
    assert '<span class="nav-label">Мой план</span>' in html
    assert "План изменений" not in html
    assert "Мои задания и баллы" not in html


@pytest.mark.parametrize("mode", ["recovery", "calendar_advice"])
def test_shared_navigation_connects_moodle_preparation_review_and_calendar(mode):
    html = render_shell("Asia/Almaty", moodle_enabled=True, chat_mode=mode,
                        planning_url="/planning", google_calendar_enabled=True,
                        calendar_plan_available=True)
    markup = Markup(html)
    links = {node["attrs"].get("data-view"): node for node in markup.nodes
             if node["tag"] == "a" and any(parent["attrs"].get("id") == "sidebar"
                                           for parent in node["parents"])}
    steps = [links[view] for view in ("tasks", "planning", "plan", "calendar")]
    assert [step["attrs"]["href"] for step in steps] == ["#moodle", "/planning", "#panel", "#calendar"]
    assert all(not any(parent["tag"] == "details" or "hidden" in parent["attrs"]
                       for parent in step["parents"]) for step in steps)
    assert "Пары и время для учёбы" in html
    assert "Составить расписание" in html
    start = markup.by_id("start")
    assert start["attrs"]["type"] == "button"
    assert start["attrs"]["data-action"] == "compose-schedule"
    assert "hidden" not in start["attrs"]
    assert any(parent["tag"] == "details" and parent["attrs"].get("class") == "composer-options"
               for parent in start["parents"])
    assert any(parent["attrs"].get("id") == "chat-form" for parent in start["parents"])
    # The accepted home keeps actions in the sidebar/composer, with no duplicate
    # stepper or deadline/three-day shortcuts below the welcome heading.
    assert not any(node["attrs"].get("id") == "schedule-steps" for node in markup.nodes)
    assert not any(node["tag"] in {"a", "button"} and
                   any("intro" in parent["attrs"].get("class", "").split() for parent in node["parents"])
                   for node in markup.nodes)


def test_missing_executor_offers_preparation_without_a_chat_only_mode_or_write_controls():
    html = render_panel(run=None, plan=None, approval=None, current=None,
                        sources=SafeSources(), timezone="Asia/Almaty", now=NOW,
                        runtime_available=False, moodle_enabled=True)
    assert 'data-runtime="false"' in html
    assert 'href="/planning"' in html
    assert "Составление расписания и применение в Google Calendar пока недоступны" in html
    assert "в этом режиме" not in html and "расписание в чате" not in html
    assert 'data-action="approve"' not in html and 'data-action="apply"' not in html


def test_exact_saved_schedule_remains_reviewable_when_executor_is_unavailable():
    async def scenario():
        fixture = Fixture()
        plan = await fixture.seed_current()
        html = render("proposed", plan, runtime_available=False, moodle_enabled=True)
        assert 'id="plan-reference"' in html
        reference = Markup(html).by_id("plan-reference")
        assert reference["attrs"]["data-hash"] == plan.payload_hash
        assert reference["attrs"]["data-version"] == "1"
        assert all(operation.operation_id in html for operation in plan.payload.operations)
        assert 'data-action="approve"' not in html and 'data-action="apply"' not in html
        assert "Расписание проверено" not in html
    run(scenario())


def test_confirmation_and_calendar_application_remain_separate_exact_version_actions():
    async def scenario():
        fixture = Fixture()
        plan = await fixture.seed_current()
        proposed = render("proposed", plan)
        assert 'data-action="approve">Подтвердить расписание</button>' in proposed
        assert 'data-action="apply"' not in proposed
        async with fixture.client() as browser:
            headers = await login(browser)
            response = await browser.post("/api/approval", headers=headers, json=action(plan))
            assert response.status_code == 200
            with fixture.request() as context:
                consent = await fixture.approvals.get(context, plan_reference(plan))
        approved = render("proposed", plan, approval=consent)
        assert 'data-action="apply">Применить в Google Calendar</button>' in approved
        assert 'data-action="approve"' not in approved
        assert plan.payload_hash in approved
        assert 'data-action="apply"' not in render("proposed", plan, approval=consent, current=None)
    run(scenario())


def test_navigation_uses_the_same_proposed_schedule_title_after_client_routing():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for client navigation regressions")
    result = subprocess.run([node, str(Path(__file__).with_suffix(".cjs")),
                             str(Path(__file__).resolve().parents[2])],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('runtime,sources_available,expected', [(True, True, True), (False, True, False), (True, False, False)])
def test_prepared_student_can_start_from_schedule_without_searching_the_chat(runtime, sources_available, expected):
    html = render_panel(run=None, plan=None, approval=None, current=None,
                        sources=SafeSources(), timezone='Asia/Almaty', now=NOW,
                        runtime_available=runtime, sources_available=sources_available, moodle_enabled=True)
    assert ('data-action="start"' in html) is expected


def test_review_displays_saved_assignment_titles_and_escapes_them():
    async def scenario():
        h = Fixture()
        plan = await h.seed_current()
        title = '<img src=x onerror=alert(1)> Homework'
        html = render_panel(run=None, plan=plan, approval=None, current=plan_reference(plan),
            sources=SafeSources(), timezone='Asia/Almaty', now=NOW, runtime_available=True,
            assignment_titles={plan.payload.allocations[0].assignment_id: title})
        assert '&lt;img' in html and '<img src=x' not in html
        assert 'Homework' in html
        assert plan.payload.operations[0].target.assignment_id in html
    run(scenario())
