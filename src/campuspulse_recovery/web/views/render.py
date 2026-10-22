"""Escaped semantic HTML. Business facts come only from authenticated stores."""
from html import escape
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from ...storage.plans import plan_reference
from ..assets.theme import THEME_BUTTON, THEME_HEAD
from .navigation import render_navigation

STATUS = {
    "proposed": ("Предложен план", "Изучите изменения перед согласованием."),
    "waiting_approval": ("Ожидает согласования", "Календарь ещё не изменён этим действием."),
    "approved": ("Версия согласована", "Применение запускается отдельной кнопкой."),
    "approval_revoked": ("Согласование отозвано", "Повторно согласовать эту версию нельзя."),
    "approval_expired": ("Срок согласования истёк", "Продлить согласование этой версии нельзя."),
    "approval_superseded": ("Согласование заменено", "Для продолжения нужен свежий план."),
    "applying": ("Применяется", "Дождитесь результата. Обновление страницы не запускает запись."),
    "partial": ("Расписание добавлено частично", "Проверьте календарь перед продолжением."),
    "unknown": ("Результат неизвестен", "Обновите состояние. Не запускайте повторную запись вслепую."),
    "applied_unverified": ("Запись пока не подтверждена", "Пока неизвестно, все ли события добавлены. Обновите состояние и проверьте календарь."),
    "verified": ("Расписание проверено", "Свежая проверка подтвердила эту версию расписания."),
    "verification_failed": ("Проверка выявила расхождения", "Нужна сверка календаря и новое расписание."),
    "needs_input": ("Нужны данные", "Уточните недостающие данные или настройку подключения."),
    "insufficient_capacity": ("Не хватает свободного времени", "Проверьте сроки, оставшуюся работу и часы для учёбы."),
    "no_change": ("Изменения не предложены", "Расписание не требует изменений."),
    "ready": ("Запуск подготовлен", "Итоговый план пока не получен."),
    "stale": ("План устарел", "Получите свежие данные и согласуйте новую версию."),
    "unavailable": ("Сервис недоступен", "Обновите состояние, чтобы узнать сохранённый результат."),
    "needs_review": ("Нужна проверка данных", "Расписание нельзя безопасно применить в текущем виде."),
}


def e(value):
    return escape(str(value), quote=True)


def safe_calendar_link(value):
    if not isinstance(value, str) or len(value) > 4096 or any(ord(c) <= 32 or ord(c) == 127 for c in value):
        return None
    try:
        parts = urlsplit(value)
        if (parts.scheme != "https" or parts.hostname not in {"calendar.google.com", "www.google.com"}
                or parts.username or parts.password or parts.port not in (None, 443)
                or "\\" in value or not (parts.path == "/calendar" or parts.path.startswith("/calendar/"))):
            return None
    except ValueError:
        return None
    return value


def stamp(value, timezone):
    return e(value.astimezone(ZoneInfo(timezone)).strftime("%d.%m.%Y %H:%M %z"))


def interval(value, timezone):
    if value is None:
        return "Нет блока"
    return f"{stamp(value.start, timezone)} → {stamp(value.end, timezone)}"


def render_shell(timezone, *, demo_mode=False, live_chat=False, signin_url=None, moodle_enabled=False, chat_mode='recovery', account_url=None, planning_url=None, google_calendar_enabled=False, calendar_plan_available=False):
    notice = ''
    if demo_mode:
        from ...demo import NOTICE
        notice = '<details class="demo-notice"><summary>Тестовый режим · без подключения к Google</summary><p>' + e(NOTICE) + ' <a href="/demo">Тестовый вход</a></p></details>'
        if live_chat:
            notice = '<details class="demo-notice"><summary>Тестовые источники · чат Gemini</summary><p>Чат настроен на Gemini. Задания и календарь тестовые; история хранится до перезапуска сервера. Восстановление работает на тестовых данных. <a href="/demo">Тестовый вход</a></p></details>'
    moodle = '''<section id="moodle" class="moodle" aria-labelledby="moodle-heading" aria-busy="true">
<div class="moodle-heading-row"><div><h1 id="moodle-heading">Мои задания</h1>
<p class="moodle-intro">Задания и ближайшие сроки</p></div>
<button id="moodle-refresh" type="button" disabled>Обновить задания</button></div>
<div class="moodle-toolbar"><div class="moodle-filters" role="group" aria-label="Группы заданий">
<button id="moodle-filter-upcoming" type="button" data-moodle-group="upcoming" aria-pressed="true" aria-controls="moodle-group-upcoming">Ближайшие</button>
<button id="moodle-filter-past" type="button" data-moodle-group="past" aria-pressed="false" aria-controls="moodle-group-past">Прошедшие</button>
<button id="moodle-filter-completed" type="button" data-moodle-group="completed" aria-pressed="false" aria-controls="moodle-group-completed">Выполнено вами</button>
</div></div>
<p id="moodle-status" role="status" aria-live="polite">Загружаем задания из Moodle…</p>
<div id="moodle-courses"></div><div id="moodle-tasks"></div>
<details class="moodle-source-details" hidden><summary>О данных Moodle</summary><p id="moodle-source-status"></p></details></section>''' if moodle_enabled else ''
    if moodle_enabled and account_url:
        moodle = moodle.replace('</div></div>\n<p id="moodle-status"',
            f'</div><a class="moodle-connection" href="{e(account_url)}">Моё подключение Moodle</a></div>\n<p id="moodle-status"')
    advice = chat_mode == 'calendar_advice'
    greeting = 'Над чем поработаем?'
    introduction = 'Помогу разобраться с заданиями и найти время на учёбу.'
    prompts = ('По будням свободен с 18:00 до 21:00. Помоги распределить задания.',
               'Помоги оценить, сколько времени нужно на каждое задание.',
               'Помоги расставить приоритеты по ближайшим дедлайнам.') if advice else (
               'Объясни, как составить расписание по заданиям Moodle.',
               'Как проверить предложенное расписание перед подтверждением?',
               'Что происходит после применения расписания в Google Calendar?')
    prompt_labels = ('Распланировать неделю', 'Оценить время', 'С чего начать?') if advice else (
        'Как это работает?', 'Как проверить расписание?', 'После применения')
    suggestions = ''.join(f'<button type="button" class="prompt-chip" data-prompt="{e(prompt)}">{e(label)}<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 17 17 7M7 7h10v10"/></svg></button>' for prompt, label in zip(prompts, prompt_labels))
    navigation = render_navigation(home_url='', planning_url=planning_url,
        account_url=account_url, active='assistant', moodle_enabled=moodle_enabled,
        google_calendar_enabled=google_calendar_enabled)
    planning_action = (f'<button id="start" class="composer-plan-action" type="button"'
        + (' data-action="compose-schedule"' if calendar_plan_available else '')
        + (' hidden' if advice and not calendar_plan_available else '')
        + ' disabled>Составить расписание</button>')
    calendar_section = calendar_assets = ''
    if google_calendar_enabled:
        from ..google_calendar import CALENDAR_SECTION
        calendar_section = CALENDAR_SECTION
        calendar_assets = '<link rel="stylesheet" href="/assets/google-calendar.css"><script src="/assets/google-calendar.js" defer></script>'
    return f'''<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Google AI · Учебное расписание</title>
<meta name="color-scheme" content="dark"><link rel="stylesheet" href="/assets/campuspulse.css">{THEME_HEAD}<script src="/assets/campuspulse.js" defer></script>
<script src="/assets/sidebar-head.js"></script>
{calendar_assets}
<script src="/assets/logo/vendor/gsap.min.js" defer></script>
<script type="module" src="/assets/logo/welcome.js"></script></head>
<body data-view="assistant" data-chat-mode="{e(chat_mode)}" data-calendar-plan-available="{str(calendar_plan_available).lower()}" data-signin-url="{e(signin_url or '')}" data-moodle-enabled="{str(moodle_enabled).lower()}" data-timezone="{e(timezone)}"><a class="skip" href="#workspace">К содержимому</a>
{navigation}
<div class="app-content"><header class="topbar"><button id="mobile-nav-toggle" type="button" aria-label="Открыть навигацию" aria-controls="sidebar" aria-expanded="false"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6h16M4 12h16M4 18h16"/></svg></button><div class="view-heading"><span id="view-title">Ассистент</span><span id="view-subtitle">Ваши задания и расписание</span></div><div class="topbar-actions">{THEME_BUTTON}<button id="reload" type="button" title="Обновить сохранённое состояние"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 7v5h-5M4 17v-5h5M6 7a7 7 0 0 1 12-1l2 6M4 12l2 6a7 7 0 0 0 12-1"/></svg><span>Обновить состояние</span></button></div></header>
<main id="workspace">{calendar_section}{moodle}<section class="recovery-home" aria-labelledby="home-heading"><div id="conversation-scroll" class="conversation-scroll" tabindex="0" aria-label="История диалога">{notice}<div class="intro">
<div id="welcome-logo" class="welcome-logo" data-state="loading" aria-hidden="true">
<canvas id="welcome-fallback" width="409" height="413" hidden></canvas>
<canvas id="welcome-canvas"></canvas></div><h1 id="home-heading">{greeting}</h1><p>{introduction}</p></div>
<div id="chat-history" class="chat-history" role="log" aria-label="Диалог с Google AI" aria-live="polite" aria-relevant="additions" hidden></div>
</div><div class="composer-dock"><div class="command-bar">
<div id="chat-limit" hidden role="status"><p>Этот диалог заполнен. Начните новый чат — история останется доступна.</p><button id="chat-limit-new" type="button">Начать новый диалог</button></div>
<form id="chat-form" class="command-body"><label class="message-label" for="recovery-message">Сообщение Google AI</label>
<textarea id="recovery-message" name="message" rows="2" maxlength="4000" placeholder="Что нужно успеть?" aria-describedby="chat-status"></textarea>
<div class="chat-submit"><details class="composer-options"><summary aria-label="Примеры вопросов" title="Примеры вопросов"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v16M4 12h16"/></svg></summary><div class="prompt-suggestions" aria-label="Примеры вопросов">{suggestions}{planning_action}</div></details><details class="chat-help"><summary>Об ассистенте<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m8 10 4 4 4-4"/></svg></summary><p id="chat-status" role="status">Проверяем подключение чата…</p></details>
<button id="voice-toggle" type="button" disabled aria-pressed="false" aria-label="Голосовой ввод" title="Голосовой ввод" aria-describedby="voice-status"><svg class="voice-mic-icon" viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="2" width="6" height="13" rx="3"/><path d="M5 10v2a7 7 0 0 0 14 0v-2M12 19v3m-4 0h8"/></svg><svg class="voice-stop-icon" viewBox="0 0 24 24" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2"/></svg><span id="voice-toggle-label" class="message-label">Голосовой ввод</span></button>
<button id="chat-send" type="submit" disabled aria-label="Отправить сообщение" title="Отправить сообщение"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5m-6 6 6-6 6 6"/></svg></button></div>
<div class="voice-input">
<div id="voice-level-wrap" class="voice-recorder" hidden role="group" aria-label="Запись голосового сообщения">
<button id="voice-cancel" type="button" aria-label="Отменить запись" title="Отменить запись"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"/></svg></button>
<div id="voice-waveform" aria-hidden="true">{''.join('<span></span>' for _ in range(104))}</div>
<button id="voice-stop" type="button" aria-label="Остановить запись" title="Остановить запись"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="5" y="5" width="14" height="14" rx="1.5"/></svg></button>
</div>
<p id="voice-status" role="status" aria-live="polite"></p></div>
</form>
</div>
</div></section>
<noscript>Для действий включите JavaScript. Загрузка страницы не подтверждает и не применяет расписание.</noscript>
<div id="panel" tabindex="-1" aria-busy="true"><p>Загружаем предложенное расписание…</p></div>
<section class="journal" aria-labelledby="journal-heading"><div class="section-heading"><h2 id="journal-heading">Журнал запуска</h2>
<div><button id="trace" type="button" disabled>Перечитать журнал</button> <button id="clear-run" type="button" disabled>Снять выбор запуска</button></div></div>
<p id="trace-health" role="status">Выберите сохранённый запуск, чтобы прочитать журнал.</p>
<ol id="trace-records"></ol><button id="trace-more" type="button" hidden>Следующая страница</button></section>
<p id="feedback" role="status" aria-live="polite">Загружаем сохранённое состояние…</p>
</main></div></body></html>'''


def verified_evidence(run, plan, sources, now):
    evidence = sources.verification
    return (run is not None and plan is not None and run.status == "verified" and run.verified_success
        and run.verification_status == "verified" and run.verification_run_id == run.run_id
        and evidence is not None and evidence.status == "verified"
        and evidence.plan == plan_reference(plan) == run.plan and evidence.run_id == run.run_id
        and evidence.verification_at == run.verification_at and evidence.verification_at <= now
        and evidence.checked_operations == tuple(op.operation_id for op in plan.payload.operations))


def render_panel(*, run, plan, approval, current, sources, timezone, now, runtime_available, moodle_enabled=False,
                 sources_available=True, assignment_titles=None, calendar_plan_available=False):
    if calendar_plan_available and not runtime_available and plan is None:
        from ..calendar_plans import MARKUP
        return MARKUP
    if moodle_enabled and not runtime_available and plan is None:
        return ('<section class="outcome" data-status="needs_input" data-runtime="false">'
            '<h2>Подготовьте данные для расписания</h2><p>Дедлайны находятся в разделе «Мои задания». '
            'Укажите пары, свободные часы и сколько времени нужно на задания.</p>'
            '<p><a href="/planning">Пары и время для учёбы</a></p>'
            '<p>Составление расписания и применение в Google Calendar пока недоступны для этого аккаунта. '
            'Пары можно сохранить заранее, а задания обсудить с ассистентом.</p>'
            '<p><a href="#workspace">Перейти к ассистенту</a></p></section>')
    status = "needs_input" if not runtime_available else "ready"
    if run is not None:
        status = "insufficient_capacity" if run.status == "infeasible" else run.status
    proven = sources_available and verified_evidence(run, plan, sources, now)
    if status == "verified" and not proven:
        status = "applied_unverified"
    ref = None if plan is None else plan_reference(plan)
    active = approval is not None and approval.plan == ref and approval.active_at(now)
    is_current = ref is not None and current == ref
    terminal_consent = None if approval is None or active else (
        approval.status if approval.status != "active" else "expired")
    needs_new_version = plan is not None and (not is_current or terminal_consent is not None)
    can_approve = (runtime_available and sources_available and is_current and not active and approval is None
                   and (run is None or run.status in {"proposed", "ready"}))
    can_apply = (runtime_available and sources_available and is_current and active and
                 (run is None or run.status in {"proposed", "ready"}))
    if plan is not None and (run is None or run.status in {"proposed", "ready"}):
        if not is_current:
            status = "stale"
        elif terminal_consent is not None:
            status = "approval_" + terminal_consent
        else:
            status = "approved" if active else "waiting_approval"
    title, description = STATUS.get(status, STATUS["unknown"])
    if run is None and plan is None and runtime_available:
        title = 'Предложенное расписание появится после расчёта'
        description = 'Проверьте пары и время для учёбы, затем нажмите «Составить расписание». Агент прочитает задания и календарь и предложит изменения.'
    html = [f'<section class="outcome" data-status="{e(status)}" data-runtime="{str(runtime_available).lower()}">',
        f'<h2>{title}</h2><p>{description}</p>']
    if runtime_available and sources_available and run is None and plan is None:
        html.append('<button type="button" data-action="start">Составить расписание</button>')
    if not sources_available:
        saved_notice = 'Показана сохранённая версия плана. ' if plan is not None else ''
        html.append('<p role="alert">Не удалось загрузить данные для расписания и результат проверки. '
            + saved_notice + 'Действия с планом временно недоступны. '
            'Нажмите «Обновить состояние», чтобы перечитать результат.</p>')
    if not runtime_available:
        html.append('<p>Составление расписания и применение в Google Calendar пока недоступны для этого аккаунта. Сохранённое расписание можно просмотреть.</p>')
    if needs_new_version:
        html.append('<p class="next-step">Нажмите «Составить расписание» на вкладке «Ассистент», чтобы получить свежий план и новую версию. '
            'Изучите её изменения и явно согласуйте новую версию. Повторное согласование этой версии недоступно.</p>')
    if run is not None:
        html.append(f'<details class="plan-details"><summary>Сведения о запросе</summary><p class="technical">Запуск: <code>{e(run.run_id)}</code> · Запрос: <code>{e(run.intent_id)}</code></p></details>')
        if not run.observation_available:
            html.append('<p>Журнал недоступен или неполон. Это не подтверждение результата.</p>')
    if run is not None:
        html.append('<p><a href="#journal-heading">Посмотреть действия ассистента</a></p>')
    if plan is not None:
        consent = ('эта версия согласована' if active else 'требуется для этой версии'
            if is_current and approval is None else 'недоступно для этой версии')
        html.append(f'<p>Подтверждение: {consent}.</p>')
    if plan is not None:
        html.append('<p>Запись в Google Calendar: ' + ('подтверждена.' if proven else 'пока не подтверждена.') + '</p>')
    if proven:
        html.append(f'<p>Проверено: <time datetime="{e(run.verification_at.isoformat())}">{stamp(run.verification_at, timezone)}</time></p>')
        links = [safe_calendar_link(event.event_link) for event in sources.verification.observed_events]
        links = [link for link in links if link]
        html.extend(f'<p><a href="{e(link)}" target="_blank" rel="noopener noreferrer">Событие в Google Calendar</a></p>' for link in links)
        if not links:
            html.append('<p>Ссылки на события не предоставлены.</p>')
    html.append('</section><div class="sources" id="sources">')
    for heading, items in (("Задания", sources.assignments), ("Календарь", sources.calendar)):
        html.append(f'<section><h2>{heading}</h2>')
        if not sources_available:
            html.append('<p>Сводку не удалось загрузить. Перечитайте состояние.</p>')
        elif heading == 'Календарь' and moodle_enabled:
            html.append('<p>При составлении расписания учитывается время, занятое в Google Calendar.</p>'
                '<p><a href="/planning">Пары и время для учёбы</a></p>')
        elif items is None:
            html.append('<p>Данные пока не загружены.</p>')
        elif not items:
            html.append('<p>В полученной сводке нет записей.</p>')
        else:
            html.append('<ul>' + ''.join(f'<li>{e(item)}</li>' for item in items) + '</ul>')
        html.append('</section>')
    html.append('</div><section aria-labelledby="diff-heading"><h2 id="diff-heading">Предложенное расписание</h2>')
    if plan is None:
        html.append('<p>Сохранённая версия расписания ещё не получена.</p></section>')
        return ''.join(html)
    payload = plan.payload
    assignment_titles = assignment_titles or {}
    step = (4 if status in {'verified', 'applied_unverified', 'verification_failed'} else
            3 if active or status in {'applying', 'partial', 'unknown'} else 2)
    html.append('<ol class="review-flow" aria-label="Подтверждение и проверка расписания">' + ''.join(
        f'<li{' aria-current="step"' if number == step else ""}><b>{number}</b>{label}</li>'
        for number, label in ((1, 'Предложение'), (2, 'Подтверждение'), (3, 'Google Calendar'), (4, 'Независимая проверка'))) + '</ol>')
    html.append('<p class="plan-summary">Проверьте задания и время в расписании перед подтверждением.</p>')
    html.append(f'<details class="plan-details"><summary>Точная версия и идентификаторы плана</summary><dl class="reference" id="plan-reference" data-plan-id="{e(ref.plan_id)}" data-version="{ref.version}" data-hash="{e(ref.payload_hash)}">'
        f'<dt>План</dt><dd>{e(ref.plan_id)}</dd><dt>Версия</dt><dd>{ref.version}</dd>'
        f'<dt>SHA-256</dt><dd><code>{e(ref.payload_hash)}</code></dd></dl></details>')
    html.append(f'<p>Расписание на период: {interval(payload.constraints.horizon, timezone)}. {e(timezone)}.</p>')
    if payload.allocations:
        html.append('<ul>' + ''.join(f'<li>Задание {e(assignment_titles.get(a.assignment_id, "Учебное задание"))}: осталось {a.remaining_minutes} мин.; '
            f'запланировано {a.scheduled_minutes} мин.</li>' for a in payload.allocations) + '</ul>')
    if not is_current:
        html.append('<p role="alert">Эта версия больше не текущая. Получите свежий план перед согласованием.</p>')
    if not payload.operations:
        html.append('<p>Нет изменений для записи в календарь.</p>')
    else:
        html.append('<div class="table-scroll" tabindex="0" role="region" aria-label="Изменения до и после"><table><caption>Что изменится в Google Calendar</caption>'
            '<thead><tr><th scope="col">Задание</th><th scope="col">Сейчас</th><th scope="col">В расписании</th></tr></thead><tbody>')
        for op in payload.operations:
            html.append(f'<tr><th scope="row"><strong>{"Добавить" if op.kind == "create" else "Перенести"}</strong><br>Задание: {e(assignment_titles.get(op.target.assignment_id, "Учебное задание"))}'
                f'<details class="operation-details"><summary>Сведения об операции</summary><code>{e(op.operation_id)}</code>'
                f'<br>Задание ID: {e(op.target.assignment_id)}<br>Блок: {e(op.target.block_id)}<br>Событие: <code>{e(op.event_id)}</code>'
                f'<br>Версия события: {e(op.expected_etag or "—")}</details></th>'
                f'<td>{interval(op.before, timezone)}</td><td>{interval(op.target.interval, timezone)}</td></tr>')
        html.append('</tbody></table></div>')
    html.append('<p>Вы подтверждаете только показанное расписание. После изменений потребуется новое подтверждение. '
        'Затем нажмите «Применить в Google Calendar», чтобы добавить события.</p>')
    if approval is not None:
        labels = {"active": "Действует", "revoked": "Отозвано", "expired": "Истекло", "superseded": "Заменено новой версией"}
        html.append(f'<p id="approval-state">Согласование: {labels.get(approval.status, "Недействительно") if active or approval.status != "active" else "Истекло"}. '
            '</p>')
        html.append(f'<details class="plan-details"><summary>Сведения о подтверждении</summary><p>Версия {approval.plan.version}, хеш <code>{e(approval.plan.payload_hash)}</code>.</p></details>')
    html.append('<div class="actions">')
    if can_approve and payload.operations:
        html.append('<button type="button" data-action="approve">Подтвердить расписание</button>')
    if can_apply and payload.operations:
        html.append('<button type="button" data-action="apply">Применить в Google Calendar</button>')
    if active and sources_available:
        html.append('<button type="button" data-action="revoke">Отозвать согласование</button>')
    html.append('</div></section>')
    return ''.join(html)
