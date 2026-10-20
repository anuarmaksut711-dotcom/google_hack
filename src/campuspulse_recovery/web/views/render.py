"""Escaped semantic HTML. Business facts come only from authenticated stores."""
from html import escape
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from ...storage.plans import plan_reference

STATUS = {
    "proposed": ("Предложен план", "Изучите изменения перед согласованием."),
    "waiting_approval": ("Ожидает согласования", "Календарь ещё не изменён этим действием."),
    "approved": ("Версия согласована", "Применение запускается отдельной кнопкой."),
    "approval_revoked": ("Согласование отозвано", "Повторно согласовать эту версию нельзя."),
    "approval_expired": ("Срок согласования истёк", "Продлить согласование этой версии нельзя."),
    "approval_superseded": ("Согласование заменено", "Для продолжения нужен свежий план."),
    "applying": ("Применяется", "Дождитесь результата. Обновление страницы не запускает запись."),
    "partial": ("Применён частично", "Часть операций завершена. Нужна сверка оставшихся операций."),
    "unknown": ("Результат неизвестен", "Обновите состояние. Не запускайте повторную запись вслепую."),
    "applied_unverified": ("Применён, проверка не подтверждена", "Фактическое расписание ещё не подтверждено свежим чтением."),
    "verified": ("Расписание проверено", "Свежая проверка подтвердила эту версию расписания."),
    "verification_failed": ("Проверка выявила расхождения", "Нужна сверка календаря и новый план изменений."),
    "needs_input": ("Нужны данные", "Уточните недостающие данные или настройку подключения."),
    "insufficient_capacity": ("Недостаточно времени в горизонте поиска", "Пересмотрите сроки, объём оставшейся работы или доступные окна."),
    "no_change": ("Изменения не предложены", "Операций для записи нет."),
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


def render_shell(timezone, *, demo_mode=False, live_chat=False, signin_url=None, moodle_enabled=False, chat_mode='recovery', account_url=None, planning_url=None):
    notice = ''
    if demo_mode:
        from ...demo import NOTICE
        notice = '<details class="demo-notice"><summary>Тестовый режим · без подключения к Google</summary><p>' + e(NOTICE) + ' <a href="/demo">Тестовый вход</a></p></details>'
        if live_chat:
            notice = '<details class="demo-notice"><summary>Тестовые источники · чат Gemini</summary><p>Чат настроен на Gemini. Задания и календарь тестовые; история хранится до перезапуска сервера. Восстановление работает на тестовых данных. <a href="/demo">Тестовый вход</a></p></details>'
    moodle = '''<section id="moodle" class="moodle" aria-labelledby="moodle-heading" aria-busy="true">
<div class="section-heading"><h2 id="moodle-heading">Мои задания и баллы</h2>
<button id="moodle-refresh" type="button" disabled>Обновить задания</button></div>
<p id="moodle-status" role="status" aria-live="polite">Загружаем задания из Moodle…</p>
<div id="moodle-courses"></div><div id="moodle-tasks"></div></section>''' if moodle_enabled else ''
    if moodle_enabled and account_url:
        moodle = moodle.replace('<div id="moodle-courses">',
            f'<p><a href="{e(account_url)}">Моё подключение Moodle</a></p><div id="moodle-courses">')
    sources_target = '#moodle' if moodle_enabled else '#sources'
    if planning_url:
        moodle += (f'<p><a href="{e(planning_url)}">Настроить время на задания и свободные часы</a></p>'
                   '<p>План применяется к подключённому Google Calendar только после вашего согласования.</p>')
    sources_title = 'Задания и баллы' if moodle_enabled else 'Задания и календарь'
    return f'''<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>CampusPulse · Восстановление плана</title>
<link rel="stylesheet" href="/assets/campuspulse.css"><script src="/assets/campuspulse.js" defer></script>
<script src="/assets/logo/vendor/gsap.min.js" defer></script>
<script type="module" src="/assets/logo/welcome.js"></script></head>
<body data-chat-mode="{e(chat_mode)}" data-signin-url="{e(signin_url or '')}" data-moodle-enabled="{str(moodle_enabled).lower()}" data-timezone="{e(timezone)}"><a class="skip" href="#workspace">К плану</a>
<aside class="sidebar" aria-label="Навигация CampusPulse">
<a class="brand" href="#workspace" aria-label="CampusPulse — к плану" title="CampusPulse"><svg class="brand-mark" viewBox="0 0 32 32" aria-hidden="true"><path d="M4 17h6l4-9 5 17 4-8h5"/></svg></a>
<nav aria-label="Разделы плана">
<a class="nav-link current" href="#workspace" aria-label="Восстановление плана" aria-current="location"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="4" y="4" width="16" height="16" rx="3"/><path d="m8 12 3 3 5-6"/></svg><span class="nav-label">Восстановление плана</span><span class="nav-short" aria-hidden="true">План</span></a>
<a class="nav-link" href="{sources_target}" aria-label="{sources_title}"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 5H6a2 2 0 0 0-2 2v12h16V7a2 2 0 0 0-2-2h-3"/><rect x="9" y="3" width="6" height="4" rx="1"/><path d="M8 11h8M8 15h5"/></svg><span class="nav-label">{sources_title}</span><span class="nav-short" aria-hidden="true">Источники</span></a>
<a class="nav-link" href="#diff-heading" aria-label="Изменения"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 7h14m-4-4 4 4-4 4M19 17H5m4-4-4 4 4 4"/></svg><span class="nav-label">Изменения</span><span class="nav-short" aria-hidden="true">Изменения</span></a>
<a class="nav-link" href="#journal-heading" aria-label="Журнал запуска"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 6h12M8 12h12M8 18h12M4 6h.01M4 12h.01M4 18h.01"/></svg><span class="nav-label">Журнал запуска</span><span class="nav-short" aria-hidden="true">Журнал</span></a>
</nav>
<div class="sidebar-bottom"><button id="signout" type="button" hidden aria-label="Выйти" title="Выйти"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 5H5v14h4M10 12h10m-4-4 4 4-4 4"/></svg></button></div>
</aside>
<div class="app-content"><header class="topbar"><a class="wordmark" href="#workspace">CampusPulse</a><span class="topbar-divider" aria-hidden="true">/</span><span>Учебное расписание</span><a class="topbar-results" href="#panel">К плану <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 17 17 7M7 7h10v10"/></svg></a></header>
<main id="workspace">{moodle}<section class="recovery-home" aria-labelledby="home-heading">{notice}<div class="intro">
<h1 id="home-heading" class="sr-only">Восстановление учебного плана</h1>
<div id="welcome-logo" class="welcome-logo" data-state="loading" aria-hidden="true">
<img id="welcome-fallback" src="/assets/logo/assets/g-reference.png" alt="" width="409" height="413" hidden>
<canvas id="welcome-canvas"></canvas></div></div>
<div id="chat-history" class="chat-history" role="log" aria-label="Диалог с CampusPulse" aria-live="polite" aria-relevant="additions" hidden></div>
<div class="command-bar">
<div class="context-strip"><a href="{sources_target}"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="5" y="3" width="14" height="18" rx="2"/><path d="M9 8h6M9 12h6M9 16h3"/></svg>{sources_title}</a><span>{e(timezone)}</span></div>
<form id="chat-form" class="command-body"><label class="message-label" for="recovery-message">Сообщение CampusPulse</label>
<textarea id="recovery-message" name="message" rows="2" maxlength="4000" placeholder="Что изменилось в расписании?" aria-describedby="message-hint"></textarea>
<div class="chat-submit"><p id="message-hint">Enter — отправить · Shift+Enter — новая строка</p><button id="chat-send" type="submit" disabled aria-label="Отправить сообщение" title="Отправить сообщение"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5m-6 6 6-6 6 6"/></svg></button></div>
<div class="voice-input"><button id="voice-toggle" type="button" disabled aria-pressed="false" aria-describedby="voice-status">Голосовой ввод</button>
<label class="voice-language" for="voice-language">Язык <select id="voice-language"><option value="ru-RU">Русский</option><option value="kk-KZ">Қазақша</option><option value="auto">Авто</option></select></label>
<label id="voice-level-wrap" hidden for="voice-level">Уровень микрофона <meter id="voice-level" min="0" max="100" value="0"></meter></label>
<p id="voice-status" role="status" aria-live="polite">Запись до 1 минуты. Аудио отправляется в Gemini для распознавания.</p></div>
<p id="chat-status" role="status">Проверяем подключение чата…</p>
<div class="toolbar"><button id="reload" type="button"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 7v5h-5M4 17v-5h5M6 7a7 7 0 0 1 12-1l2 6M4 12l2 6a7 7 0 0 0 12-1"/></svg>Обновить состояние</button>
<button id="start" type="button" disabled><span>Проверить изменения и восстановить план</span><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5m-6 6 6-6 6 6"/></svg></button></div></form>
<p id="feedback" role="status" aria-live="polite">Загружаем сохранённое состояние…</p>
</div></section>
<noscript>Для действий включите JavaScript. Загрузка страницы не согласует и не применяет план.</noscript>
<div id="panel" tabindex="-1" aria-busy="true"><p>Загрузка…</p></div>
<section class="journal" aria-labelledby="journal-heading"><div class="section-heading"><h2 id="journal-heading">Журнал запуска</h2>
<button id="trace" type="button" disabled>Загрузить журнал</button></div>
<p id="trace-health" role="status">Выберите сохранённый запуск, чтобы прочитать журнал.</p>
<ol id="trace-records"></ol><button id="trace-more" type="button" hidden>Следующая страница</button></section>
<footer>Часовой пояс: {e(timezone)}. Интервалы [начало, конец): конец не включён.
Запланированное время не означает, что задание выполнено.</footer></main></div></body></html>'''


def verified_evidence(run, plan, sources, now):
    evidence = sources.verification
    return (run is not None and plan is not None and run.status == "verified" and run.verified_success
        and run.verification_status == "verified" and run.verification_run_id == run.run_id
        and evidence is not None and evidence.status == "verified"
        and evidence.plan == plan_reference(plan) == run.plan and evidence.run_id == run.run_id
        and evidence.verification_at == run.verification_at and evidence.verification_at <= now
        and evidence.checked_operations == tuple(op.operation_id for op in plan.payload.operations))


def render_panel(*, run, plan, approval, current, sources, timezone, now, runtime_available, moodle_enabled=False):
    if moodle_enabled and not runtime_available:
        return ('<section class="outcome" data-status="needs_input" data-runtime="false">'
            '<h2>Планирование заданий</h2><p>Данные из Moodle доступны выше. '
            'Для составления плана ещё нужны оценки времени на задания.</p></section>')
    status = "needs_input" if not runtime_available else "ready"
    if run is not None:
        status = "insufficient_capacity" if run.status == "infeasible" else run.status
    proven = verified_evidence(run, plan, sources, now)
    if status == "verified" and not proven:
        status = "applied_unverified"
    ref = None if plan is None else plan_reference(plan)
    active = approval is not None and approval.plan == ref and approval.active_at(now)
    is_current = ref is not None and current == ref
    terminal_consent = None if approval is None or active else (
        approval.status if approval.status != "active" else "expired")
    needs_new_version = plan is not None and (not is_current or terminal_consent is not None)
    can_approve = (runtime_available and is_current and not active and approval is None
                   and (run is None or run.status in {"proposed", "ready"}))
    can_apply = (runtime_available and is_current and active and
                 (run is None or run.status in {"proposed", "ready"}))
    if plan is not None and (run is None or run.status in {"proposed", "ready"}):
        if not is_current:
            status = "stale"
        elif terminal_consent is not None:
            status = "approval_" + terminal_consent
        else:
            status = "approved" if active else "waiting_approval"
    title, description = STATUS.get(status, STATUS["unknown"])
    html = [f'<section class="outcome" data-status="{e(status)}" data-runtime="{str(runtime_available).lower()}">',
        f'<h2>{title}</h2><p>{description}</p>']
    if not runtime_available:
        html.append('<p>Облачный исполнитель не настроен. Запуск восстановления недоступен.</p>')
    if needs_new_version:
        html.append('<p class="next-step">Нажмите «Проверить изменения и восстановить план», чтобы получить свежий план и новую версию. '
            'Изучите её изменения и явно согласуйте новую версию. Повторное согласование этой версии недоступно.</p>')
    if run is not None:
        html.append(f'<p class="technical">Run: <code>{e(run.run_id)}</code> · Intent: <code>{e(run.intent_id)}</code></p>')
        if not run.observation_available:
            html.append('<p>Журнал недоступен или неполон. Это не подтверждение результата.</p>')
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
        if items is None:
            html.append('<p>Безопасная сводка источника не подключена.</p>')
        elif not items:
            html.append('<p>В полученной сводке нет записей.</p>')
        else:
            html.append('<ul>' + ''.join(f'<li>{e(item)}</li>' for item in items) + '</ul>')
        html.append('</section>')
    html.append('</div><section aria-labelledby="diff-heading"><h2 id="diff-heading">Изменения в расписании</h2>')
    if plan is None:
        html.append('<p>Сохранённая версия плана ещё не получена.</p></section>')
        return ''.join(html)
    payload = plan.payload
    html.append(f'<dl class="reference" id="plan-reference" data-plan-id="{e(ref.plan_id)}" data-version="{ref.version}" data-hash="{e(ref.payload_hash)}">'
        f'<dt>План</dt><dd>{e(ref.plan_id)}</dd><dt>Версия</dt><dd>{ref.version}</dd>'
        f'<dt>SHA-256</dt><dd><code>{e(ref.payload_hash)}</code></dd></dl>')
    html.append(f'<p>Горизонт поиска: {interval(payload.constraints.horizon, timezone)}. {e(timezone)}.</p>')
    if payload.allocations:
        html.append('<ul>' + ''.join(f'<li>Задание {e(a.assignment_id)}: осталось {a.remaining_minutes} мин.; '
            f'запланировано {a.scheduled_minutes} мин.</li>' for a in payload.allocations) + '</ul>')
    if not is_current:
        html.append('<p role="alert">Эта версия больше не текущая. Получите свежий план перед согласованием.</p>')
    if not payload.operations:
        html.append('<p>Нет изменений для записи в календарь.</p>')
    else:
        html.append('<div class="table-scroll" tabindex="0" role="region" aria-label="Изменения до и после"><table><caption>Точные сохранённые операции</caption>'
            '<thead><tr><th scope="col">Операция / блок</th><th scope="col">До</th><th scope="col">После</th></tr></thead><tbody>')
        for op in payload.operations:
            html.append(f'<tr><th scope="row"><strong>{"Создать" if op.kind == "create" else "Перенести"}</strong><br>Задание: {e(op.target.assignment_id)}'
                f'<details class="operation-details"><summary>Сведения об операции</summary><code>{e(op.operation_id)}</code>'
                f'<br>Блок: {e(op.target.block_id)}<br>Событие: <code>{e(op.event_id)}</code>'
                f'<br>Версия события: {e(op.expected_etag or "—")}</details></th>'
                f'<td>{interval(op.before, timezone)}</td><td>{interval(op.target.interval, timezone)}</td></tr>')
        html.append('</tbody></table></div>')
    html.append('<p>Согласование относится только к показанной версии, хешу и операциям. Изменённый план требует нового согласования.</p>')
    if approval is not None:
        labels = {"active": "Действует", "revoked": "Отозвано", "expired": "Истекло", "superseded": "Заменено новой версией"}
        html.append(f'<p id="approval-state">Согласование: {labels.get(approval.status, "Недействительно") if active or approval.status != "active" else "Истекло"}. '
            f'Версия {approval.plan.version}, хеш <code>{e(approval.plan.payload_hash)}</code>.</p>')
    html.append('<div class="actions">')
    if can_approve and payload.operations:
        html.append('<button type="button" data-action="approve">Согласовать эту версию</button>')
    if can_apply and payload.operations:
        html.append('<button type="button" data-action="apply">Применить согласованную версию</button>')
    if active:
        html.append('<button type="button" data-action="revoke">Отозвать согласование</button>')
    html.append('</div></section>')
    return ''.join(html)
