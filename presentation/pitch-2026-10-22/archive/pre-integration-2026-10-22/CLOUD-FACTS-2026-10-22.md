# Облачные сведения для защиты

Источник: `setup/PITCH-CLOUD-HANDOFF.md` Мерея в ветке `origin/codex/pitch-agent-cloud`,
SHA `1382f49aaba7c37c6571bbd8355349064e039012`, прочитан через Git 22.10.2026.
[Исходный handoff](https://github.com/anuarmaksut711-dotcom/google_hack/blob/1382f49aaba7c37c6571bbd8355349064e039012/setup/PITCH-CLOUD-HANDOFF.md).
Файл Мерея в этой ветке не создавался и не изменялся.

В новой ветке реализован read-only study-agent: обычный веб-чат вызывает существующий
managed Agent Engine через `async_query(invocation, service_token)`. ADK выбирает
`read_assignments()` и `read_schedule()` через приватный MCP. Backend связывает
вызов с владельцем и живой сессией; после генерации заново проверяет пары.
Trace содержит фактические `tool`, `status`, `duration_ms`, `result_count`.
В этом релизе нет инструментов записи Calendar и не включается recovery.

Handoff явно оставляет cloud acceptance pending: финальный source SHA/image/revisions
и успешный пользовательский запуск должны быть внесены после реального rollout.
Наличие этой ветки не доказывает публикацию или доступность функций. UI-ветка
не объединяла её и не выполняла deploy. Исторический checkpoint 20 октября
не заменяет новую приёмку. Ресурсные идентификаторы и сведения доступа в слайды не перенесены.

Для объединения нужно сохранить нашу делегацию `extract_classes` через
`CalendarConversation`, а в `StudyConversation` добавить делегацию к её локальному
`delegate` для импорта файлов (или отдельный явно настроенный импортёр).
Данные загрузки не должны отправляться как обычный prompt в read-only study MCP.
`scoped_agent_factory` Мерея и наш единый `selected_chat_agent` нужно объединить
так, чтобы импортёр оставался внутри приложения конкретного владельца.

При последнем fetch этот же SHA уже находится в origin/main. Он не объединялся
с UI-веткой: кроме study-agent, восстанавливает отдельный ConnectedPlans writer
и CSS подмену plan panel. Точные merge-запросы и риск class freshness отдельного
writer зафиксированы в setup/PITCH-UI-HANDOFF.md. UI умеет показать optional
сохранённые tool_trace/agent_run_id; отсутствие trace не подменяется текстом модели.
