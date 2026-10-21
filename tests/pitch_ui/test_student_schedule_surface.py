"""Student scheduling exposes the enabled writer through one review surface."""
from pathlib import Path
import shutil
import subprocess

from campuspulse_recovery.web.google_calendar import CALENDAR_SECTION
from campuspulse_recovery.web.views.app import SafeSources
from campuspulse_recovery.web.views.render import render_panel
from tests.storage.test_plans import NOW, run
from tests.web.test_ui_states import Fixture, Markup, render


class StudentText(Markup):
    def __init__(self, html):
        self.visible = []
        super().__init__(html)

    def handle_data(self, value):
        if not any(node['tag'] == 'details' for node in self.stack):
            self.visible.append(value)


def test_connected_writer_uses_the_existing_review_panel_without_false_unavailability():
    html = render_panel(run=None, plan=None, approval=None, current=None,
        sources=SafeSources(), timezone='Asia/Almaty', now=NOW, runtime_available=False,
        moodle_enabled=True, calendar_plan_available=True)
    assert 'id="calendar-plans"' in html
    assert 'data-action="compose-schedule"' in html
    assert 'Составить расписание' in html
    assert 'сейчас недоступно' not in html and 'исполнитель' not in html
    assert 'data-action="approve"' not in html and 'data-action="apply"' not in html


def test_recovery_writer_keeps_its_existing_review_flow_when_both_capabilities_exist():
    html = render_panel(run=None, plan=None, approval=None, current=None,
        sources=SafeSources(), timezone='Asia/Almaty', now=NOW, runtime_available=True,
        moodle_enabled=True, calendar_plan_available=True)
    assert 'data-action="start"' in html
    assert 'id="calendar-plans"' not in html


def test_calendar_agenda_does_not_contain_another_plan_review_screen():
    assert 'id="calendar-events"' in CALENDAR_SECTION
    assert 'id="calendar-plans"' not in CALENDAR_SECTION


def test_saved_plan_metadata_is_collapsed_and_student_copy_avoids_transport_terms():
    async def scenario():
        fixture = Fixture()
        plan = await fixture.seed_current()
        html = render('proposed', plan)
        public_text = ' '.join(StudentText(html).visible)
        assert 'SHA-256' not in public_text and 'Run:' not in public_text
        assert plan.payload_hash not in public_text and 'run_fake' not in public_text
        assert '<p>Согласование относится только к показанной версии, хешу' not in html
        assert '<caption>Точные сохранённые операции</caption>' not in html
    run(scenario())


def test_connected_plan_client_loads_after_panel_replacement_and_keeps_exact_actions():
    node = shutil.which('node')
    assert node, 'Node.js is needed for the Calendar review behavior'
    result = subprocess.run([node, str(Path(__file__).with_suffix('.cjs')),
        str(Path(__file__).resolve().parents[2])], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
