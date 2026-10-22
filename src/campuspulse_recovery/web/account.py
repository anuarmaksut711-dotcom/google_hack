"""First Moodle connection and optional settings; saved feed URLs stay server-side."""
from .assets.theme import THEME_BUTTON, THEME_HEAD
from .telegram_settings import SECTION as TELEGRAM_SECTION


def render_moodle_settings(*, connected, classes_html, google_calendar_html='', shared_shell=False):
    instructions = ('<p>Откройте Moodle AITU → Календарь → Экспорт календаря. Выберите все события, '
        'нажмите Get calendar URL и вставьте полученную ссылку сюда.</p>')
    privacy = ('<p>Ссылка даёт доступ к вашему календарю и сохраняется для этого аккаунта Google AI. '
        'При составлении плана Gemini получает названия предметов, заданий и сроки. '
        'Календарь не сообщает, сдали ли вы работу: выполненные задания отмечайте здесь.</p>')
    form = ('<form id="connect-moodle"><label for="moodle-url">'
        + ('Новая ссылка календаря Moodle' if connected else 'Ссылка календаря Moodle')
        + '</label><input id="moodle-url" type="url" required autocomplete="off" maxlength="4096">'
        '<button type="submit">' + ('Сохранить новую ссылку' if connected else 'Подключить мой Moodle')
        + '</button></form>')
    if connected:
        moodle = ('<p id="moodle-connection-status" role="status">Moodle подключён. '
            'Ссылка сохранена для вашего аккаунта; при следующем входе вводить её не нужно.</p>'
            '<details id="moodle-change"><summary>Изменить ссылку Moodle</summary>'
            '<p>Новая ссылка заменит текущее подключение только после успешной проверки.</p>'
            + instructions + privacy + form + '</details>')
    else:
        moodle = ('<p id="moodle-connection-status">Подключите календарь Moodle один раз. '
            'При следующих входах в этот аккаунт Google AI он загрузится автоматически.</p>'
            + instructions + privacy + form)
    header = ('' if shared_shell else '<header class="settings-header"><a class="settings-brand" href="/">Google AI</a>'
        '<div class="settings-actions"><a class="settings-back" href="/"><svg viewBox="0 0 24 24" aria-hidden="true">'
        '<path d="m12 5-7 7 7 7M5 12h14"/></svg>К заданиям и чатам</a></div></header>'
    )
    return ('<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Настройки · Google AI</title><link rel="stylesheet" href="/assets/campuspulse.css">'
        + THEME_HEAD + '<script src="/assets/telegram-settings.js" defer></script></head><body class="settings-page">' + header +
        '<main class="settings-main"><h1>Настройки</h1>'
        '<section class="settings-appearance" aria-labelledby="appearance-heading">'
        '<div><h2 id="appearance-heading">Оформление</h2>'
        '<p>Тема применяется ко всем страницам и сохраняется в этом браузере.</p></div>'
        + THEME_BUTTON + '</section>' + TELEGRAM_SECTION +
        '<section class="settings-card" data-connected="' + str(connected).lower() + '" '
        'aria-labelledby="moodle-settings-heading"><div class="settings-card-heading">'
        '<span class="settings-card-icon"><svg viewBox="0 0 24 24" aria-hidden="true">'
        '<path d="m3 8 9-5 9 5-9 5-9-5Zm4 3v6c3 3 7 3 10 0v-6M21 8v9"/>'
        '</svg></span><h2 id="moodle-settings-heading">Moodle</h2></div>'
        + moodle + '<p class="settings-sync">Moodle обновляется при открытии страницы и каждые 5 минут, пока вкладка открыта. '
        'В облачной версии также предусмотрено фоновое обновление каждые 15 минут. '
        'Отображаются события выбранного периода экспорта; изменения могут появляться с задержкой.</p>'
        '<p id="account-status" role="status" aria-live="polite"></p></section>'
        + google_calendar_html + classes_html + '</main><script src="/assets/account.js"></script></body></html>')
