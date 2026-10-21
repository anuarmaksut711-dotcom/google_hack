# CampusPulse AI — UI и защита 22 октября 2026

Дата подготовки: 21 октября 2026, Asia/Almaty. Исполнитель этой ветки не выполнял
облачный deploy, IAM, работу с секретами или merge в main. Push ветки не означает
публикацию сайта.

## Git и изоляция

- Отдельный clone: `google_hack_pitch_ui`, рядом с основной рабочей копией.
  Основная папка `google_hack` не переключалась, не очищалась и не изменялась.
- Ветка: `codex/pitch-ui-demo`.
- Исходный актуальный `origin/main`: `4a0858a00b3f1c1c2bf382981b0400f212f06fb8`.
  Повторный fetch перед передачей подтвердил тот же SHA.
- Итоговый коммит UI/README/тестов: `c3d15957426c315b9902172f8f489d36608c5bdf`.
- Итоговый коммит материалов: `d3c7219762d4f849817e50b7e1bc806b9d516ea5`.
- Финальный SHA ветки, включая этот handoff, сообщён в результате передачи и
  разрешается через `git rev-parse origin/codex/pitch-ui-demo`. Собственный SHA
  handoff не встраивается внутрь самого коммита.

Применимых `AGENTS.md` в родительских каталогах и tracked-исходниках не найдено.
Прочитаны README и все три указанных recovery-документа. Нового
`setup/PITCH-CLOUD-HANDOFF.md` нет даже после финального fetch; он принадлежит Мерею
и здесь не создавался. Облачные сведения материалов явно исторические, от 20.10.

## Изменённые файлы

Производственный код, только разрешённая зона:

- `src/campuspulse_recovery/web/assets/client.py` — сохранение точной версии при
  потерянном ответе, блокировка действий до перечитывания, сохранение
  аутентифицированного контекста при ошибке чтения run/evidence, снятие старого
  выбора запуска, автоматическое чтение trace, понятные tool/result.
- `src/campuspulse_recovery/web/assets/styles.py` — видимое предупреждение для
  устаревшей карточки, нейтральное оформление её статуса и переносы журнала.
- `src/campuspulse_recovery/web/views/render.py` — отдельный четвёртый этап
  независимой проверки, состояние согласования и записи, ссылка на журнал,
  замена противоречивого advice-контекста Calendar ссылкой на фактические
  наблюдения и сохранённые настройки времени.
- `src/campuspulse_recovery/web/recovery_planning.py` — штатный CSRF bootstrap с
  проверкой сессии, состояния загрузки/пустого списка, валидация, защита повторов
  и обязательное перечитывание после неопределённого сохранения.
- `README.md` — назначение, пользовательский путь, границы текущего статуса,
  архитектура, технические инструкции и материалы защиты.

Новые тесты и тестовый стенд:

- `tests/pitch_ui/test_pitch_states.py`
- `tests/pitch_ui/test_pitch_client.py`, `client_pitch.cjs`
- `tests/pitch_ui/test_planning_client.py`, `test_planning_client.cjs`
- `tests/pitch_ui/browser_fixture.py`

Все остальные новые файлы находятся в `presentation/pitch-2026-10-22/`:
презентация, исходник сборки, тексты, previews, QA receipt и браузерные evidence.
Полный перечень: `git diff --name-only 4a0858a..codex/pitch-ui-demo`.
Исходники backend, старые тесты, зависимости, deploy и настройки облака не менялись.

## Проверки окончательных исходников

| Проверка | Результат | Граница |
| --- | --- | --- |
| `python -m pytest -q --tb=short` | **2256 passed, 26 skipped, 35 warnings**, 248.26 s | Локальные тестовые внешние сервисы; 12 Firestore Emulator и 14 PowerShell тестов пропущены |
| `python -m pytest -q tests/pitch_ui` | **13 passed** | Выполняются реальные функции поставляемого JS с контролируемыми DOM/IO, плюс серверный renderer |
| Связанный web/planning/recovery прогон | **305 passed**, 6 warnings до последней дополнительной render-регрессии | Вся эта область повторно включена в окончательный полный pytest |
| Recovery evaluation `--mode offline` | **347 passed**, 95 warnings | Scripted model, тестовые Google-адаптеры |
| Voice evaluation `--mode literal-loopback` | **53 passed** | Протокол; качество речи/Gemini не измеряется |
| `python -m build --no-isolation` | wheel и sdist собраны | Окончательные исходники |
| `python -m pip check` | No broken requirements found | Отдельное игнорируемое `.venv-pitch`, Python 3.12 |
| `python deploy/check-recovery-composition.py` | `offline_mcp_composition_ok` | Инструменты не вызываются; авторизация сохранена |
| `git diff --check` | чисто | Разрешённая зона |
| PPTX/PDF | 7 слайдов / 7 страниц; editable native objects; visual/text/font checks | Подробности и хеши в `QA-RECEIPT.json` материалов |

Полные локальные логи в игнорируемой `.validation/pitch-final-pytest.log`,
`pitch-ui-tests.log`, `pitch-final-build.log`, `pitch-pip-check.log`,
`pitch-composition.log`, `pitch-recovery.log`, `pitch-voice.log`.
Скопированная среда имела дубликат metadata пакета с некорректной версией;
исправлена только отдельная `.venv-pitch`. Исходная `.venv-test` не менялась.
Первый sandbox-прогон сетевых тестов отклонил loopback bind; повтор с разрешением
локальных сокетов прошёл. Это не неисправность приложения.

### Работающий браузер

Использован in-app браузер на компьютере, обычные клики/ввод и снимки экрана.
Основные взаимодействия шли в видимой области браузера ~639 px. Для точных
1280 и 390 px использован тестовый iframe с реальным media-query viewport:
переходы к плану и журналу также проверены кликами. У документа при вертикальном
scrollbar оставалось 1260 / 370 px; общего горизонтального переполнения не было.
Таблица изменений сохраняет собственную горизонтальную прокрутку.

Фактически выполнено:

1. Штатные challenge/signin/session внутри тестовой композиции с синтетическим
   verifier; получено Moodle-задание `Homework` из тестового ICS.
2. Введены 90 минут и окно 17.10 22:00–18.10 02:00, сохранены текущим API.
   После перезагрузки значения вернулись. Неверные 22:07 отклонены до сохранения.
3. Создано предложение, показаны две операции, версия 1 и её хеш.
4. Согласование показало отдельную кнопку применения, ещё без verified-успеха.
5. Отдельное применение завершилось независимым verify; перезагрузка сохранила
   verified-состояние и не показывала повторное применение.
6. В журнале фактические read/apply/verify, числа операций и безопасные technical
   details. Счётчики стенда: **2 Calendar mutations, apply_plan 1, verify_plan 1**.
7. Полный HTTP-check стенда отдельно проверил replay тем же ключом без новых записей.

Часы стенда намеренно фиксированы на **17.10.2026 21:00 Asia/Almaty**; это не
текущее время и не личные события. Тестовый proxy принимает только literal
loopback, переименовывает только свои cookies и запрещает внешнюю сеть/настоящие
credentials внутри сервера. Эти адаптации находятся исключительно в тестах и
не добавлены в продукт. Ранний конфликт чужих localhost cookies исправлен в
стенде; свежая вкладка финального сценария показала 0 console errors/warnings.

Evidence: `presentation/pitch-2026-10-22/evidence/`: `planning-saved.png`,
`proposal.png`, `verified-reloaded.png`, `journal.png`, `desktop-verified-1280.png`,
`narrow-verified-390.png`, `narrow-journal-390.png`, `narrow-planning-390.png`,
`fixture-receipts.json`, `proposal-trace.json`. Эти файлы содержат только
синтетические данные, не cookies/токены/личные Moodle URL.

Независимый read-only review изменённой области не нашёл actionable P0–P2;
proof gate и привязка согласования сохранены. Запросы за границами UI ниже
остаются открытыми.

Опубликованный URL проверен только до обычной страницы `/login`; новый вход,
личный recovery и реальная запись Calendar в этом запуске не проверены.

## API и ожидания UI — контракты сохранены

| API | Ожидание |
| --- | --- |
| `GET /auth/session`; header `X-CSRF-Bootstrap: 1` перед POST | `session_id`, валидный `csrf_token`; смена сессии запрещает отправку сохранённой формы |
| `GET /api/moodle` | текущий safe snapshot.tasks; только незавершённые задачи в форме |
| `GET /api/moodle/planning` | `revision`, `estimates`, `working_windows` |
| `POST /api/moodle/planning` | `expected_revision` + изменяемые поля; 409 требует reread; null означает неизвестно, minutes 0 исключает работу, [] windows означает нет времени |
| `POST /api/runs` | только стабильный `request_key`; ответ содержит настоящий `run_id` и статус |
| `POST /api/approval` / `revoke` | точные `plan_id`, `version`, `payload_hash`, стабильный `request_key` |
| `POST /api/runs/continue` | стабильный `request_key` и точный `plan`; согласование не отправляет этот запрос автоматически |
| `GET /ui/state?run_id=...` | HTML с авторизованной версией/согласованием/независимым evidence; только эта существующая proof gate разрешает verified |
| `GET /api/runs/{run_id}/trace?after=N` | safe `records`, `after`, `next`; сохраняются layer/call/operation/attempt/sequence и доступные числовые результаты |

GET/refresh не согласуют и не применяют. Unknown, partial, applied_unverified,
verification_failed не получают оформление подтверждённого успеха. Запросы не
переигрываются автоматически после потерянного ответа. Не добавлялись обход
авторизации, Calendar selector или вымышленные мысли модели.

Trace загружается после получения сохранённого run_id. Текущий API создания run
не даёт браузеру поток tool-событий до своего ответа; real-time reasoning не заявлен.
Назначение инструмента — статическое описание, результат — только safe summary
реального события. Если событие отсутствует, UI его не дорисовывает.

## Конкретные запросы к Мерею

1. **Облачный handoff.** Добавить `setup/PITCH-CLOUD-HANDOFF.md` на своей ветке:
   опубликованный URL и SHA, provenance website/Engine/MCP, результат настоящего
   approve/apply/verify и replay, проверка двух аккаунтов и оставшиеся ограничения.
   Секреты и личные привязки вне Git. До этого облако/реальное видео непроверены.
2. **Ошибка получения evidence.** `AccountFeed.project()` в `lms/multiuser.py`
   вызывает `CloudBackend.stored_evidence()` в `cloud/backend.py`; ошибка удалённого
   чтения срывает весь `/ui/state`. Нужно консервативно вернуть сохранённое
   partial/unknown/applied_unverified/verification_failed без evidence. Ошибки
   авторизации и смену сессии не маскировать. Отсутствие
   evidence никогда не должно давать verified. Сейчас UI сохраняет прошлую
   карточку, блокирует действия и позволяет reread/снять выбор.
3. **Контекст recovery.** `CalendarChatProjection` формирует advice-текст
   «свободные часы неизвестны / запись не подключена», независимо от сохранённого
   planning и включённого recovery. UI больше не показывает его как статус
   Calendar, но чат всё ещё может получать этот контекст. Нужна типизированная
   безопасная recovery-сводка из авторизованного planning и Calendar snapshot:
   revision/update time, известность effort/windows, read status/time/horizon,
   counts/безопасные интервалы; без Calendar IDs, URL и credentials. Отдельно
   желательно передать safe display titles для task IDs в plan diff.
4. **Наблюдаемость solver.** В фактически прочитанном trace proposal есть
   read_assignments/read_calendar и backend candidate, но нет события solve_repair.
   Добавить safe solver start/result через существующий журнал, если его надо
   показывать на защите. События и успешный Function Calling не выдумываются UI.

## Материалы и видео

В `presentation/pitch-2026-10-22/`:

- `output/CampusPulse-AI-Pitch-2026-10-22-final.pptx` — редактируемые 7 слайдов.
- `output/CampusPulse-AI-Pitch-2026-10-22-final.pdf` — проверенный PDF.
- `DEMO-SCRIPT-RU.md` — 2–3 минуты, основной проверенный локальный путь и fallback.
- `SPEECH-RU.md` — текст выступления по слайдам.
- `JURY-QA-RU.md` — короткие ответы жюри.
- `README.md`, `LOCAL-VALIDATION.md`, `QA-RECEIPT.json`, `build-deck.mjs`, `previews/`.
- `VIDEO-STATUS.md` — причина отсутствия реальной облачной записи и точные условия
  её подготовки. Реальное cloud-видео **не создано**; локальные evidence его не заменяют.

## После финального деплоя

Сверить serving SHA с веткой, повторить форму на desktop/mobile, проверку
сохранения/перезагрузки, actual model-initiated tools, exact plan consent,
независимое чтение Calendar, replay без дублей, stale-consent rejection,
cross-account denial, unbound user и все четыре неуспешных статуса. Сопоставить
UI, trace и выделенный Calendar. После этого обновить облачные слайды/оговорки
и записать реальное видео по `VIDEO-STATUS.md`. Это требует отдельной проверки
опубликованной системы; отправленная Git-ветка её не подтверждает.

## Повторный аудит всего проекта — 22 октября

Проверены исходники `e57ce4dea0a818040d77afad44b65bd9324dace3`, текущий main
`4a0858a00b3f1c1c2bf382981b0400f212f06fb8`, отдельный snapshot Calendar-ветки
`c19aee8eba587e499f48de295e7700136023a595` и опубликованный сайт read-only.
Полный отчёт с причинами, воспроизведением и таблицей соответствия:
[`PROJECT-AUDIT-2026-10-22.md`](../presentation/pitch-2026-10-22/PROJECT-AUDIT-2026-10-22.md).

- Свежий pytest UI-ветки с Firestore Emulator: **2268 passed, 14 skipped**;
  все пропуски — PowerShell 7. Recovery **347**, voice **53**, build, composition,
  pip check и полный локальный HTTP-flow прошли. Это тестовые внешние сервисы.
- Опубликованная сессия загрузила 15 Moodle-задач, но recovery `runtime=false`,
  Calendar не подключён. Реальный полный сценарий, Engine acceptance и видео
  остаются неподтверждёнными; PITCH-CLOUD-HANDOFF всё ещё отсутствует.
- Запросы 2–4 выше подтверждены повторно: `/ui/state` выдаёт 503 при отказе
  evidence при живой сессии; контекст Calendar статический; solver-event отсутствует.
- Новые запросы Мерею: исправить зависимость connected MCP от отсутствующего
  `.live_mcp` для чистой сборки; включить объявленные OAuth-пакеты в test.lock;
  согласовать assertion `tests/lms/test_web.py:165` с новым режимом Calendar.
  После ручного добавления трёх OAuth-пакетов только в отдельную локальную venv
  Calendar-ветка дала **2183 passed, 1 failed, 14 skipped**; один сбой воспроизведён
  отдельно. Это не успешный прогон чистой установки по README.

Добавлены только этот раздел, отчёт/receipt и обезличенный screenshot в материалах.
Производственные исходники, зависимости, backend-тесты и облако в ходе аудита
не изменялись. Проверявшийся локальный emulator остановлен. Итоговый SHA
документального аудита сообщается при передаче и разрешается через ref ветки.
