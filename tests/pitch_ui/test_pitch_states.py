"""UI evidence is never promoted from missing or historical verification."""
from datetime import timedelta
from tests.web.test_ui_states import Fixture, render, view
from tests.storage.test_plans import NOW, run
from campuspulse_recovery.storage.plans import plan_reference


def test_four_step_review_includes_independent_verification():
    async def scenario():
        h = Fixture()
        plan = await h.seed_current()
        html = render('applied_unverified', plan)
        assert 'Запись пока не подтверждена' in html
        assert 'Применён, проверка не подтверждена' not in html
        assert 'Независимая проверка' in html
        assert 'aria-current="step"' in html
        assert 'Запись в Google Calendar: пока не подтверждена' in html
        assert 'Расписание проверено' not in html
        assert 'data-action="apply"' not in html
    run(scenario())


def test_proposal_explains_exact_consent_and_journal():
    async def scenario():
        h = Fixture()
        plan = await h.seed_current()
        html = render('proposed', plan)
        assert 'Подтверждение: требуется для этой версии' in html
        assert 'href="#journal-heading"' in html
        assert plan_reference(plan).payload_hash in html
    run(scenario())


def test_recovery_does_not_render_advice_prompt_as_calendar_connection_state():
    from campuspulse_recovery.web.views.app import SafeSources
    html = render('needs_input', moodle_enabled=True, sources=SafeSources(calendar=(
        'Личные свободные часы неизвестны. Уточни доступное время.',
        'План составляется в чате; запись в календарь не подключена.')))
    assert 'запись в календарь не подключена' not in html
    assert 'Уточни доступное время' not in html
    assert 'href="/planning"' in html
    assert 'время, занятое в Google Calendar' in html
