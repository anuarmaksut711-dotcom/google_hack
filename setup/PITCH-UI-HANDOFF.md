# Интеграция сайта, 22.10.2026

Этот раздел имеет приоритет. Предыдущие записи ниже — история UI-ветки до объединения.

## Ветки и полномочия

- Интеграционная ветка: `codex/site-integration-2026-10-22`, отдельная копия `google_hack_pitch_ui`.
- База актуального main: `d8c6054d1ebacbda2cc79e397a30e6443e8a9dfe`; релиз Мерея `1382f49aaba7c37c6571bbd8355349064e039012`.
- Объединяется `codex/pitch-ui-demo`: `15bb5fd494164c55e09ef40e9a5bbbd4809cfa86`.
- Проверен `codex/chat-pages-release`: `c19aee8eba587e499f48de295e7700136023a595`. Его Calendar-исходники уже совпадали с main; дополнительно возвращены 29 полезных защитных проверок в `tests/pitch_ui/test_connected_legacy_guards.py`.
- `codex/pitch-agent-cloud` совпадает с исходным main. Существующие незакоммиченные изменения и основная копия `google_hack` сохранены.
- Пользователь разрешил интеграцию в main после проверок, обновление существующего приложения без IAM/секретов/новых ресурсов и одно событие «CampusPulse — тест». Реальная запись ещё не выполнялась.
- Итоговые SHA и фактический статус публикации фиксируются в `presentation/pitch-2026-10-22/INTEGRATION-RECEIPT.json` и последующей записи передачи. Push не считается публикацией.

## Что исправлено

- Один пользовательский сценарий и один экран «Предложенное расписание». Возможность записи определяется реальным серверным исполнителем; режимы пользователю не предлагаются.
- Управляемый study-agent сохраняет только инструменты чтения. Типизированные и проверенные блоки ответа передаются существующему ConnectedPlans, затем отдельно подтверждаются и применяются через приватный MCP.
- Точная привязка к сохранённым парам, владельцу, календарю и предложению проверяется на draft/approval/dispatch/intent/final verification. Часовой пояс пар берётся из политики студента. Calendar перечитывается перед подтверждением и каждой вставкой.
- Изменённые/неизвестные/частичные результаты не выглядят как подтверждённый успех. При повторной проверке старые успешные отметки и ссылки сразу убираются; отказ перечитывания не возвращает их.
- Файлы расписания дополняют редактируемый черновик по дням, повторы не дублируются. Предыдущие строки и даты сохраняются при следующем импорте/ошибке. Сохранение после импорта требует сверки и подтверждения полноты недели. Даты и незримые дни не выводятся из скриншота.
- В cloud runtime и test lock возвращены уже объявленные OAuth-пакеты (google-auth-oauthlib 1.5.0, requests-oauthlib 2.0.0, oauthlib 3.3.1); до этого cloud lock их не устанавливал.
- Технические ID, хеши и служебные результаты находятся в свёрнутых сведениях. В основном потоке — понятные студенту действия.

## Проверки интеграции

- Полный локальный pytest: **2472 passed, 0 skipped** (171.44 s), Firestore emulator и offline PowerShell; внешние сервисы синтетические. Первый прогон: 2431 passed, 7 ожиданий старых UI-строк; они обновлены без ослабления проверки доступа/состояний.
- Recovery offline: **347 passed**; voice literal-loopback: **53 passed**; wheel/sdist, pip check, MCP composition — passed.
- После полного прогона мелкие исправления текста/ссылок проверены 27 целевыми тестами. Точный commit дополнительно проходит Linux Cloud Build release gate перед публикацией.
- Независимый review: P0/P1 не найдено, обнаруженный P2 stale verified card воспроизведён регрессионным тестом и исправлен.
- Браузер компьютера: новая cloud app composition на loopback8964, синтетические только внешние Engine/Google/Moodle/Calendar. Два файла → дополнение пар → оба подтверждения → save/reload → editable request → typed proposal → exact consent → apply/verify → reload. Создано ровно одно тестовое событие адаптера; это не живой Google Calendar.
- Узкий экран iframe390: clientWidth=scrollWidth=370 (20px scrollbar); карточка и кнопки доступны, предупреждений/ошибок консоли нет. Свидетельства: `presentation/pitch-2026-10-22/evidence/integration/`.
- **Реальная Vertex OCR-проверка** предоставленного пользователем PNG через `AdkConversationAgent.extract_classes`: ровно 4 пары выбранного понедельника, 16:00–16:50 / 17:00–17:50 / 18:00–18:50 / 19:00–19:50, valid_from/valid_until=null. Остальные вкладки дней не превращены в пары. Использованы существующие gcloud credentials в памяти; это адаптерная проверка, не опубликованный HTTP endpoint. Оригинал и личные ответы в Git не включены.

## API и ожидания UI

Публичные существующие контракты не изменены:

- GET/POST `/api/account/classes`; POST `/api/account/classes/import` (raw PDF/PNG/JPEG, CSRF, 8 MiB,45s,30классов).
- `/api/chat`: typed AdviceReply обрабатывается сервером; текст чата не является согласием.
- GET `/api/google-calendar/plans`; POST `.../approve`, `.../apply`, `.../verify` с точными `plan_id` и `payload_hash`.
- `/api/moodle/planning`, `/api/runs`, `/api/runs/{run_id}/trace`, `/api/approval`, `/api/runs/continue` сохранены для recovery-композиции.
- Session/owner/CSRF rechecks сохранены. Импорт не пишет в Calendar и не сохраняет пары автоматически.

## Ограничения и приёмка после публикации

- Скриншот пользователя показывает понедельник. Для настоящего учебного плана нужны остальные дни либо явное подтверждение студентом, что пар в них нет, и даты действия. Не заполнять это выдуманными данными ради демонстрации.
- Если пары изменились уже после внешней вставки, существующее событие невозможно ретроспективно отменить транзакцией Firestore. Система прекращает дальнейшие записи и не объявляет результат проверенным; студент сверяет Calendar.
- Обновить существующие Cloud Run website/MCP согласованным образом, сохранить настройки/identity/secret refs; актуализировать существующий read-only Engine из точного wheel при необходимости. Никаких IAM/secret/resource changes.
- В новой опубликованной ревизии: обычный Google sign-in, подключение календаря, реальный screenshot import, разрешённое единственное событие «CampusPulse — тест», независимое чтение, reload без дублей. При неизвестном результате сначала проверить, не повторять вслепую.
- Облачные сведения Мерея брать из `setup/PITCH-CLOUD-HANDOFF.md` (не изменён). Локальные успешные тесты и прямой Vertex OCR не заменяют live-приёмку сайта.

Материалы: `presentation/pitch-2026-10-22/output/` (PPTX/PDF), `DEMO-SCRIPT-RU.md`, `SPEECH-RU.md`, `JURY-QA-RU.md`, `VIDEO-STATUS.md`. Статус видео и фактическая live-приёмка будут обновлены после публикации.

---

# Актуальное дополнение: единый сценарий и импорт пар, 22.10.2026

Предыдущие разделы ниже сохранены как история. Это дополнение и
`presentation/pitch-2026-10-22/SCHEDULE-RECEIPT-2026-10-22.json` имеют приоритет
для последней ревизии файлов и презентации.

## Git и границы последней доработки

- Ветка: `codex/pitch-ui-demo`, отдельный clone `google_hack_pitch_ui`.
- Исходный commit этой доработки: `acf869b1eff2e50f2669c3f9f824b7b83cfb534d`.
  Исходная база всей задачи: `4a0858a00b3f1c1c2bf382981b0400f212f06fb8`.
- Во время работы main обновился с `624624be3681fdb5f818e1568dd17c09e1f19f26`
  на `1382f49aaba7c37c6571bbd8355349064e039012` (релиз Мерея).
  Наши последние проверки относятся к UI-ветке на базе acf869b; новый backend
  main целиком не объединялся. Ни main, ни основная папка `google_hack` не менялись.
- Пользователь явно разрешил расширить зону на серверное распознавание и учёт пар.
  Deploy, IAM, секреты, ресурсы GCP, зависимости не менялись.
- Итоговый commit реализации указан в разделе передачи ниже; финальный HEAD
  документации сообщается пользователю и проверяется `git rev-parse origin/codex/pitch-ui-demo`.

## Передача последней реализации

Итоговый commit реализации, тестов и обновлённых PPTX/PDF:
`342305472300db9b713fdab8febe9a6354cd367b`.
Этот следующий commit меняет только запись передачи в документации.
Финальный HEAD ветки после него сообщён пользователю и проверен на remote;
полный диапазон для переноса: `acf869b1eff2e50f2669c3f9f824b7b83cfb534d..origin/codex/pitch-ui-demo`.
Push не является публикацией сайта. Совместная приёмка с новым main Мерея — отдельный этап.

## Пользовательский результат

Moodle → «Пары и время для учёбы» → «Предложенное расписание» → подтверждение
точной версии → отдельное «Применить в Google Calendar» → независимый verify.
Нет пользовательского переключателя режимов. Runtime-capability checks сохраняются:
без исполнителя расчёт/запись недоступны, а редактор пар доступен.

PDF/PNG/JPEG → bounded модель → editable draft → обязательная сверка человеком →
сохранение существующим API. Даты не выдумываются; исходный файл не хранится.
Пары исключаются из времени для заданий, сами пары автоматически в Calendar не записываются.
Изменение пар инвалидирует старое согласие вплоть до последнего транзакционного guard.
`applied_unverified` теперь «Запись пока не подтверждена», без утверждения факта записи.
Названия заданий показаны из точного сохранённого owner-bound снимка, ID остаются в details.

## Изменённые файлы и API

- `agent/conversation.py`: bounded `extract_classes` без истории/инструментов.
- `cloud/backend.py`, `cloud/calendar_app.py`: делегация распознавания в существующих обёртках.
- `lms/schedule_import.py`: validated draft, типы, размер, nullable dates.
- `lms/manual_schedule.py`: опциональный transaction reader для owner-bound read.
- `lms/planning.py`: private class basis, вычитание занятости, версионирование данных.
- `lms/multiuser.py`: один экземпляр chat/import agent; всегда доступная подготовка.
- `web/manual_schedule.py`: additive import route и доработка существующего редактора.
- `web/recovery_planning.py`, `web/assets/planning_styles.py`: единая подготовка и responsive styles.
- `web/views/render.py`, `web/views/app.py`, `web/assets/client.py`, `sidebar.py`, `styles.py`:
  подписи/навигация/прямой расчёт, optional сохранённый study tool_trace/run ID, переносы и точные названия заданий.
- `tests/pitch_ui/test_classes_constraints*.py`, `test_schedule_import*`,
  `test_schedule_preparation.py`, `test_unified_schedule_ui.*`, `test_study_trace_ui.*`, existing pitch states/fixture.
- В `tests/web/test_ui_states.py` и `tests/lms/test_web.py` обновлены только две UI-строки
  из-за переименования. Backend-проверки, доступ и ожидания операций не ослаблялись.
- `README.md`, материалы `presentation/pitch-2026-10-22/`, этот handoff.
  Пути исходников выше — внутри `src/campuspulse_recovery/`.

Существующие контракты сохранены:

- GET/POST `/api/account/classes`: сохранённый недельный шаблон.
- GET/POST `/api/moodle/planning`: estimates, raw working_windows, revision.
- POST `/api/runs`, GET `/api/runs/{run_id}`, GET `/api/runs/{run_id}/trace`.
- POST `/api/approval`, `/api/runs/continue`: отдельное подтверждение точного plan reference.
- `/auth/session`: CSRF bootstrap, привязка к живой сессии до/после async операций.

Новый endpoint: POST `/api/account/classes/import`, raw body с точным Content-Type
`application/pdf`, `image/png` или `image/jpeg`; CSRF и owner/session обязательны.
Лимиты 8 МиБ, 45 секунд, 30 пар, 4096 output tokens / 24000 chars; MIME+magic bytes.
Ответ `{draft:{classes,valid_from,valid_until},warning}` не сохраняет расписание.
Ошибки безопасные: 400/413/415/422/429/503, без raw model/SDK text.

## Проверки и материалы

Полный pytest: **2369 passed, 0 skipped**, Firestore Emulator + offline PowerShell.
Recovery offline: **347 passed**; voice loopback: **53 passed**.
Сборка wheel/sdist, pip check и MCP composition пройдены.
Независимый read-only review — без actionable findings.
Полный журнал и границы: `presentation/pitch-2026-10-22/SCHEDULE-VALIDATION-2026-10-22.md`.
Свежий receipt: `SCHEDULE-RECEIPT-2026-10-22.json` в той же папке.

Браузер компьютера: PDF/draft/review/save, reload, минуты/окна, proposal с исключением
пары, exact approval, separate apply, verified, reload, actual trace. Desktop iframe1280:
client=scroll=1260; narrow iframe390: client=scroll=370 (scrollbars20). Переполнения
документа нет, таблица прокручивается отдельно. Новый узкий tab: console warning/error0.
HTTP replay не создал дублей: 2 fake Calendar mutations, apply_plan1, verify_plan1.
Это локальные тестовые сервисы, а не реальные Gemini/Moodle/Calendar/GCP.

- PPTX: `presentation/pitch-2026-10-22/output/CampusPulse-AI-Pitch-2026-10-22-final.pptx`.
- PDF: тот же путь с `.pdf`; 7 страниц, все просмотрены, package/layout/import0 findings.
- Сценарий: `DEMO-SCRIPT-RU.md`, речь: `SPEECH-RU.md`, вопросы: `JURY-QA-RU.md` в папке защиты.
- Тестовый PDF: `samples/synthetic-weekly-classes.pdf`; скриншоты: `evidence/schedule/`.
- Видео реального облачного пути отсутствует: read-only релиз Мерея не включает
  запись/recovery и оставляет live acceptance pending. Причина и запись после
  разблокировки: `VIDEO-STATUS.md`. Dummy video или fake success не добавлялись.

## Обязательные запросы Мерею при объединении с main1382f49

Свежий `setup/PITCH-CLOUD-HANDOFF.md` прочитан из его ветки/актуального main, но здесь
не создавался и не редактировался. Его факты: read-only study-agent, live acceptance pending.
Точные риски объединения, найденные review:

1. В `lms/multiuser.py` сохранить `scoped_agent_factory`/`my_du_transport` из main.
   Вычислить ОДИН выбранный агент: scoped factory для study без recovery, иначе
   существующая factory. Передать тот же объект в `create_ui_app` и
   `inner.state.class_schedule_importer`. Сохранить нашу установку `/planning` для
   всех аккаунтов и старую сигнатуру вызова `install_class_routes(auth,account)`.
   Сохранить main `agenda_source` для Calendar projection.
2. В `cloud/calendar_app.py` объединить main study settings/factory/health с нашей
   делегацией `CalendarConversation.extract_classes`. В `StudyConversation` добавить
   `extract_classes` → `self.delegate.extract_classes`. Файлы не передавать как
   обычный prompt в study MCP; его read-only grant contracts сохраняются.
3. В `AdkConversationAgent` сохранить наш extraction method и CTA «Составить расписание»;
   новые инструкции main со словами «Мой план» согласовать с новым названием UI.
4. В main `web/google_calendar.py` правила
   `body[data-chat-mode=calendar_advice][data-view=plan]:has(#google-calendar) #panel`
   и `#feedback` скрывают наш честный status panel, показывая альтернативные «Планы из чата».
   При `StudyConversation.owns_sources=True` backend намеренно не создаёт эти карточки.
   Нужно убрать эту подмену: раздел «Предложенное расписание» должен показывать наш
   panel/недоступность recovery, а Google Calendar — отдельную agenda/connection страницу.
   Не превращать пустой список study-чата в успешный Calendar proposal.
5. В main `integrations/connected_plans.py` отдельный writer проверяет Calendar
   snapshot/binding, но не сохраняет и не перечитывает `cp_class_schedules`.
   Наш `MoodleApprovalStore` его не защищает. До показа этого writer как единого
   расписания с учётом пар нужно связать class basis с proposal и проверять его
   на approve/dispatch/final Calendar I/O. После изменения пар старый draft не
   должен записываться. Study path сам по себе не пишет.
6. В managed study runtime не обещать подтверждение «Мой план»: backend с
   `owns_sources=True` намеренно не создаёт Calendar draft. Оставить достоверный
   статус и новое название. В UI уже добавлено отображение optional сохранённых
   `tool_trace`/`agent_run_id`; без этих полей вызовы не выдумываются.
7. Согласовать итоговую серверную возможность записи. Read-only study-agent не может
   выполнить обещание Calendar; включать реальный recovery только штатными настройками
   после независимой live приёмки. До этого UI не показывает успех и не обходит auth.

8. Дополнить `requirements/test.lock` тремя уже объявленными OAuth-зависимостями:
   google-auth-oauthlib1.5.0, requests-oauthlib2.0.0, oauthlib3.3.1. В этой зоне
   зависимости не менялись. Локальная venv содержала эти три пакета вручную ещё
   после предыдущего аудита. Готовый проверенный кандидат —
   `presentation/pitch-2026-10-22/backend-candidates/0001-include-declared-oauth-test-dependencies.patch`.
   Это не доказательство чистой установки из неизменённого test.lock.

После объединения повторить весь release gate на ИНТЕГРИРОВАННОМ SHA, реальный OCR
PDF+скриншот настроенной моделью, два владельца/изоляцию, пары/даты/пересечения,
изменение исходных данных после согласия, реальные разрешённые тестовые Calendars,
повтор без дублей, fresh verify, reload и резервное видео. Push этой UI-ветки не является
публикацией или приёмкой приложения. Локальные 2369 не относятся к ещё не объединённому backend1382f49.

---

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
   **Обновление 22.10:** для `/ui/state` исправлено в `web/views/`: необязательная
   проекция может отказать, но авторизованная сохранённая карточка остаётся доступна
   без verified-успеха и кнопок. Ошибки доступа/привязки и чтения плана не скрываются.
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

## Исправления после аудита — 22 октября

База исправлений: `6d0edc92d587bbb9e4049f9d978025dd4d31b781`. Во время работы
main обновился до `624624be3681fdb5f818e1568dd17c09e1f19f26`; его изменения только
в `web/assets/client.py` и `styles.py` включены в отдельную pitch-ветку. Объединения
в main не было. Финальный SHA сообщается при передаче.

Изменено:

- `web/views/app.py` — доступность карточки при отказе optional source/evidence
  projection. План, согласование и current ref читаются раньше и не имеют такого
  fallback. `BindingConflict`/`AccessDenied` не скрываются. После await повторно
  проверяется сессия. Никаких старых evidence или частичных source payload.
- `web/views/render.py` — предупреждение, отсутствие verified-успеха/ссылок и
  действий при недоступной проекции; точный план сохраняется. Пустой экран не
  утверждает наличие сохранённой версии. Успешный reread восстанавливает отображение.
- `tests/pitch_ui/test_projection_failure.py` — шесть регрессий, включая настоящий
  локальный цикл apply/verify → отказ → восстановление без новых Calendar mutations.
- `tests/pitch_ui/browser_fixture.py` — только синтетические loopback-контролы
  отказа/восстановления для браузерного QA. В продукт они не входят.
- README, `FIXES-2026-10-22.md`, receipt и обезличенные browser evidence.

Публичные JSON API и форматы планов не изменены. HTML `/ui/state` при отказе
необязательной проекции теперь возвращает авторизованную карточку с предупреждением
вместо 503. Неопределённый результат не становится успешным. Ошибки чтения самого
плана/согласования/current ref по-прежнему возвращают безопасную ошибку.

В `presentation/pitch-2026-10-22/backend-candidates/` подготовлены два reviewable
патча для Мерея. Это файлы материалов; реальные `requirements/test.lock` и
`tests/lms/test_web.py` не менялись. Патчи проверены в отдельном временном snapshot
`c19aee8`: **2184 passed, 14 skipped**, целевые проверки **6 passed**. Зависимости
в тестовой venv дополнены объявленными OAuth-пакетами; чистый Linux CI кандидатов
должен выполнить владелец файлов.

Независимый read-only code review не нашёл actionable defects; небольшие проверки
ошибок plan/approval/current store подтвердили 503 до вызова optional projection.
Окончательные результаты полного прогона, hash исходников и нюансы переносного
PowerShell записаны в `FIX-RECEIPT-2026-10-22.json` рядом с отчётом исправлений.

Итоговый полный прогон после интеграции нового main: **2288 passed, 0 skipped**,
35 warnings, 153.69 s; UI **19 passed**, recovery **347**, voice **53**, build,
pip check и offline composition прошли. PowerShell Prepare-проверки выполнены
реальным официальным CLI с явным импортом его стандартных модулей только в
локальном launcher (автопоиск модулей portable archive на этом Mac не сработал).
Продуктовые deploy-скрипты не менялись; облачных команд не было.

После интеграции main полный browser-flow повторён; отказ и перезагрузка дают
`applied_unverified` без действий, восстановление возвращает verified. Фактические
счётчики финального стенда: apply **1**, verify **1**, mutations **2**, reads **9**;
0 console errors/warnings. Viewport 1280/390 проверены для неизменённого outage
renderer перед интеграцией двух chat-only assets; финальный общий flow проверен
повторно после интеграции. Подробности и evidence: `FIXES-2026-10-22.md`.

Браузерный сценарий подтверждён только с синтетическими внешними сервисами.
PITCH-CLOUD-HANDOFF отсутствует и после нового fetch. Остались задачи Мерея:
воспроизводимая `.live_mcp` композиция, точный контекст planning/Calendar, событие
solver и реальная cloud acceptance. Условия реального видео в VIDEO-STATUS.md
сохраняются; cloud-факты и слайды не обновлялись предположениями.
