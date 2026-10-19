# Подключения CampusPulse

Подготовка выполнена без запуска тестов и без запросов к Gemini, Sheets или Calendar.
Файлы конфигурации сами по себе не подключают сервисы. Доступность моделей, права и
облачный запуск нужно проверить позже, когда команда предоставит доступы.

## Что подготовлено

- Чат поддерживает `gemini_api` с ключом из переменной `GEMINI_API_KEY` и `vertex`
  с Google Application Default Credentials, явным проектом и регионом.
- `run-demo.ps1` принимает файл настроек чата и умеет запросить ключ скрытым вводом.
  Ключ передаётся дочернему процессу через окружение; этот запуск не записывает его
  в конфигурацию или аргументы командной строки. После остановки прежнее значение
  переменной восстанавливается.
- `Prepare-Connections.ps1` создаёт настройки для Gemini, Sheets, Calendar,
  Firestore и существующих скриптов развёртывания. Сеть не используется.
- Рабочие настройки по умолчанию находятся в `.campuspulse-local/`, исключённой из Git.
  Существующие файлы генератор сохраняет без изменений.

Запускать команды из корня проекта в PowerShell 7.

```powershell
./setup/Prepare-Connections.ps1
```

В этой папке появятся `chat-gemini.json`, `chat-vertex.json`,
`app-config.local.json`, `app-config.cloud.json`, `cloud-inputs.json`, `connections.json`,
а также новые `cloud-settings.json`, `cloud-run-inputs.json`, `cloud-runtime-status.json`.
Неизвестные параметры оставлены `null`. Они должны быть заполнены реальными значениями,
а не выдуманными идентификаторами. `us-central1` предложен как исходный регион;
ресурсы в нём не создавались.

## Когда руководитель даст ключ Gemini

Если это обычный ключ Gemini API / Google AI Studio, используйте:

```powershell
./run-demo.ps1 -Port 8958 -ChatTarget ./.campuspulse-local/chat-gemini.json -PromptGeminiKey
```

Вставить ключ нужно в скрытый запрос терминала, не в чат и не в JSON.
Порт 8958 выбран отдельно от уже открытого демо на 8957.
Открыть `https://127.0.0.1:8958/demo`. Сертификат локальный, самоподписанный.
Первое сообщение будет реальным обращением к Gemini; именно тогда можно проверить доступ.
До этого статус подключения не подтверждён.

В шаблоне указана модель `gemini-flash-latest`, как в документации ADK. Команда может
заменить `model` на конкретную доступную ей модель. Доступность и квота не проверялись.

Этот запуск использует настоящего провайдера чата, но **задания, календарь и вход
остаются тестовыми**. Данные и история демо сбрасываются при перезапуске.
Ключ Gemini не даёт прав на Google Sheets, Calendar, Firestore или развёртывание.

Если под «ключом» имеется в виду JSON сервисного аккаунта, не вставлять его в этот
запрос. Текущий путь Workspace использует ADC и impersonation, а не JSON-ключи.

## Если команда использует Gemini через Google Cloud

Заполнить `chat-vertex.json`: `project_id`, `location`, `model`.
Для Vertex нужен конкретный доступный в проекте идентификатор модели; шаблон его не угадывает.
Авторизация CLI и авторизация приложения — отдельные настройки. При необходимости
войти через `gcloud auth login` и `gcloud auth application-default login` в аккаунт,
которому предоставлен доступ к выбранному проекту.

```powershell
./run-demo.ps1 -Port 8958 -ChatTarget ./.campuspulse-local/chat-vertex.json
```

Этот режим также подключает только чат к тестовому демо. Использование Vertex
требует включённого соответствующего API, прав на модель и настроенного биллинга/кредитов.

## Что требуется для Sheets и Calendar

Существующие адаптеры находятся в `src/campuspulse_recovery/integrations/`.
Они используют явную таблицу, диапазон и календарь из `app-config.local.json`.

Владельцу проекта нужно предоставить:

- Project ID и номер проекта; доступ аккаунту разработчика.
- Три отдельные служебные учётные записи `campuspulse-backend`, `campuspulse-agent`,
  `campuspulse-mcp`. Для сборок предусмотрена отдельная `campuspulse-build`.
- Google Sheets ID и диапазон; по умолчанию это `'Assignments'!A1:F100`.
- Явный Google Calendar ID. Значение `primary` текущая реализация не принимает.
- Идентификатор пользователя для входа `https://accounts.google.com#SUB`, где `SUB`
  получен из проверенной Google identity, а не заменён email или произвольным текстом.

Таблицу нужно расшарить на `campuspulse-mcp@PROJECT_ID.iam.gserviceaccount.com` с правом
чтения, календарь — на тот же адрес с правом изменять события. Для локального режима
разработчику требуется право impersonation этой служебной учётной записи.
Нужны Sheets API, Calendar API и IAM Service Account Credentials API.

Заголовки листа `Assignments` в указанном порядке:

```text
assignment_id | course | title | deadline | remaining_minutes | status
```

`deadline` должен содержать реальный срок с часовым поясом, `remaining_minutes` —
оставшееся время работы, `status` — `todo`, `in_progress` или `done`.

## Moodle: автоматические задания и баллы

Добавлен отдельный источник AITU Moodle, постоянный кеш и фоновая синхронизация
через Cloud Scheduler. Настройка реального студенческого аккаунта, необходимые
API-функции, хранение токена и команды запуска описаны в [MOODLE.md](MOODLE.md).
Тесты интеграции выполняются без реальных учётных данных; действующий токен и
развёртывание по-прежнему нужны для проверки доступа именно в AITU.

## Облачный сервер: код добавлен, запуск с реальными аккаунтами впереди

Новая точка запуска: `python -m campuspulse_recovery.cloud`. Один образ из
`deploy/Dockerfile.cloud` используется для трёх сервисов Cloud Run:

| Сервис | Назначение | Учётная запись |
| --- | --- | --- |
| `campuspulse-web` | Вход Google, UI, чат Gemini, история, согласование точного плана | backend |
| `campuspulse-agent` | ADK/Gemini, вызовы инструментов, расчёт кандидата | agent |
| `campuspulse-mcp` | Реальные Sheets/Calendar, сохранение снимков, запись и проверка событий | mcp |

Для совместной сборки создан extra `cloud` и отдельно разрешённый
`requirements/cloud-linux.lock` с хешами. Нельзя просто объединить прежние lock-файлы:
ADK 2.6.3 требует OpenTelemetry не выше 1.42.1, тогда как FastAPI 0.143.3 требует
не ниже 1.44. Облачный extra использует FastAPI 0.141.1 и ограничения ADK.
Обновление lock-файла: `./requirements/resolve-cloud.ps1` (разрешение зависимостей,
не установка и не тест). Это разрешение выполнено; Docker-сборка ещё не запускалась.

В `src/campuspulse_recovery/cloud/` добавлены проверка подписанных сервисных ID-токенов,
одноразовые вызовы с привязкой к живой пользовательской сессии в Firestore,
серверная основа расчёта, независимое чтение результата проверки и учёт завершения
пишущего процесса. Применение по-прежнему требует сохранённого согласования точной
версии плана; текст в чате его не заменяет. Чат получает реальные источники через MCP.

Этот путь запускает ADK в Cloud Run. Старый альтернативный путь Agent Engine
(`deploy-agent.py`, `deploy-mcp.ps1`, `ManagedRuntimeProtocol`) не используется и его
ограничения не сняты. Если хакатон требует именно Agent Engine, это отдельное условие:
новый Cloud Run runtime не следует представлять как развёртывание Agent Engine.

Для отдельного пути Agent Engine теперь подготовлены custom server runtime и offline
bundle: [AGENT-ENGINE.md](AGENT-ENGINE.md). Они сохраняют проверку сервисного токена
и Firestore grants. Текущий backend transport не переключён; настоящий deployment,
IAM, SDK-вызов и живое демо остаются отдельной работой команды подключений.

### Что заполнить

В `cloud-settings.json`:

- `application`: реальные Project ID, три служебных аккаунта, Sheet ID, диапазон,
  Calendar ID, часовой пояс и разрешённый Google `sub`.
- `backend_origin`, `agent_origin`, `mcp_origin`: адреса вида
  `https://SERVICE_NAME-PROJECT_NUMBER.REGION.run.app` без завершающего `/`.
- `google_signin_client_id`: OAuth Web client ID. В Google Auth Platform добавить
  `backend_origin` в Authorized JavaScript origins, настроить consent screen и
  разрешённого тестового пользователя. Для используемого popup/callback входа
  OAuth client secret серверу не нужен. Google sign-in не выдаёт доступ к таблице
  и календарю — их нужно расшарить на MCP-аккаунт отдельно.
- `chat`: `{"provider":"gemini_api","model":"gemini-flash-latest"}` либо
  `{"provider":"vertex","project_id":"…","location":"…","model":"…"}`.
  Во втором случае API-ключ не нужен, требуется Vertex IAM и доступная модель.
- `working_hours`: реальные разрешённые часы учёбы по местному часовому поясу.
  Например, `[{"weekdays":[0,1,2,3,4],"start":"17:00","end":"21:00"}]`
  означает понедельник–пятницу. Это пример, не автоматически принятое расписание.
  Границы должны быть кратны 15 минутам, ночные окна нужно разделить по дням.

В `cloud-run-inputs.json`: Project ID/number, deployer account, регионы, Firestore,
Artifact Registry, staging bucket и отдельный build service account. Файл использует
секрет `campuspulse-cloud-settings` для всех трёх ролей и `campuspulse-gemini-key`
только для backend/agent. В режиме Vertex секрет ключа можно убрать из `secrets`
и `secret_readers`. Не используйте прежний `cloud-inputs.json` для нового пути.
Ранее созданные файлы генератор не перезаписывает; старый `connections.json` может
содержать прежний список недостающих адаптеров. Актуальное описание — здесь.

### Развёртывание позже, когда команда предоставит доступы

Команды ниже **сейчас не запускались**. Они создают облачные ресурсы и могут расходовать
кредиты. Требуются доступ к проекту, подключённый биллинг и права deployer на создание
ресурсов, сборку, Cloud Run и IAM. Скрипты не подключают биллинг автоматически.

```powershell
# Подготовка инфраструктуры; сначала прочитать вывод Inspect.
./deploy/prepare-gcp.ps1 -InputsPath ./.campuspulse-local/cloud-run-inputs.json -Mode Inspect
./deploy/prepare-gcp.ps1 -InputsPath ./.campuspulse-local/cloud-run-inputs.json -Mode Provision -PolicyAndQuotaReviewed
./deploy/iam.ps1 -InputsPath ./.campuspulse-local/cloud-run-inputs.json -Mode Apply -CloudRunRuntime
```

В Secret Manager добавить значение ключа в `campuspulse-gemini-key` через консоль,
запомнить номер версии. Не помещать значение в JSON, аргументы команд или Git.
В команде ниже `:1` — пример номера версии, его нужно заменить фактическим.

```powershell
# Подготовленный план без сети и без запуска сервисов.
./deploy/deploy-cloud.ps1 -InputsPath ./.campuspulse-local/cloud-run-inputs.json -SettingsPath ./.campuspulse-local/cloud-settings.json -GeminiKeySecret campuspulse-gemini-key:1

# Фактическая сборка и развёртывание после заполнения настроек.
./deploy/deploy-cloud.ps1 -InputsPath ./.campuspulse-local/cloud-run-inputs.json -SettingsPath ./.campuspulse-local/cloud-settings.json -GeminiKeySecret campuspulse-gemini-key:1 -Mode Deploy
```

В режиме Vertex убрать `-GeminiKeySecret`. Если образ уже собран, параметр
`-ImageDigest REGION-docker.pkg.dev/PROJECT/REPO/IMAGE@sha256:DIGEST` пропускает сборку.
Скрипт загружает только разрешённые исходники и lock-файлы, закрепляет digest образа
и конкретные версии секретов, разворачивает роли с разными service accounts,
выдаёт backend→agent→MCP `run.invoker`. Публичен веб-вход; внутренние сервисы требуют
IAM и дополнительной проверки токена приложением. Функциональные тесты скрипт не запускает.

После развёртывания открыть `backend_origin/login`. `/healthz` означает только,
что процесс запущен; он не подтверждает Gemini, Google-доступы или успешную запись.
Статус `verified` в приложении получается только из независимой проверки Calendar.

### Что обязательно проверить потом

Сборка pinned-зависимостей, Google sign-in, разрешённый пользователь, чтение реальной
таблицы/календаря, ответ Gemini, предложение плана, согласование, применение и повторное
чтение Calendar. Отдельно проверить отзыв сессии/согласования и обрыв запроса записи.
Изменение рабочих часов требует нового плана. Старая согласованная версия с другими
окнами будет отклонена. Для обновления конфигурации использовать один новый secret
version для всех трёх сервисов, не направлять пользователей на смешанные версии.

После неопределённого удалённого таймаута или аварийного завершения процесса сервер
не выдаёт выдуманное доказательство остановки и не повторяет запись вслепую. Такой
случай остаётся заблокированным до проверки журнала и календаря оператором; наличие
таймаута само по себе не доказывает, что Google не принял событие. Успешно завершённые
пишущие задачи сохраняют подтверждение остановки в Firestore.

Текущий статус: код и инструкция подготовлены, локальные проверки Moodle,
интерфейса, авторизации и регрессии выполнены (подробности в `MOODLE.md`).
**Docker-сборка, фактическое развёртывание и работа с реальными аккаунтами
не проверены**. Микрофон не включён
в эту серверную задачу. Одного ключа Gemini для всего облака недостаточно.

## Официальные справочники

- [Gemini в Google ADK](https://adk.dev/agents/models/google-gemini/)
- [Ключи Gemini API](https://ai.google.dev/gemini-api/docs/api-key)
- [Авторизация приложения через ADC](https://docs.cloud.google.com/sdk/gcloud/reference/auth/application-default/login)
- [Совместный доступ к Google Calendar](https://developers.google.com/workspace/calendar/api/concepts/sharing)
- [Cloud Run: авторизация сервисов](https://docs.cloud.google.com/run/docs/authenticating/service-to-service)
- [Cloud Run: адреса сервисов](https://docs.cloud.google.com/run/docs/triggering/https-request)
- [Настройка Google Identity Services](https://developers.google.com/identity/gsi/web/guides/get-google-api-clientid)
