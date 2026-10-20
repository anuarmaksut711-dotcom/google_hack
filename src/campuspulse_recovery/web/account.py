"""First Moodle connection and optional settings; saved feed URLs stay server-side."""


def render_moodle_settings(*, connected, my_du_html):
    instructions = ('<p>Откройте Moodle AITU → Календарь → Экспорт календаря. Выберите все события, '
        'нажмите Get calendar URL и вставьте полученную ссылку сюда.</p>')
    privacy = ('<p>Ссылка даёт доступ к вашему календарю и сохраняется для этого аккаунта CampusPulse. '
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
            'При следующих входах в этот аккаунт CampusPulse он загрузится автоматически.</p>'
            + instructions + privacy + form)
    return ('<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Настройки · CampusPulse</title><link rel="stylesheet" href="/assets/login.css">'
        '</head><body><main><a href="/">К заданиям и чатам</a><h1>Настройки</h1>'
        '<section aria-labelledby="moodle-settings-heading"><h2 id="moodle-settings-heading">Moodle</h2>'
        + moodle + '<p id="account-status" role="status" aria-live="polite"></p></section>'
        + my_du_html + '</main><script src="/assets/account.js"></script></body></html>')
