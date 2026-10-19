# Agent Engine: серверный runtime и пакет для развёртывания

Добавлен `campuspulse_recovery.agent_engine.runtime.AgentEngineRuntime` — custom
Python agent для закреплённого Vertex SDK **1.165.1**, Python **3.12**.
Он запускает существующий ADK recovery worker внутри процесса. Текущий backend
по-прежнему обращается к Cloud Run: его transport, API-клиенты и фронтенд не менялись.

**Статус: код runtime и offline-подготовка проверены локально. Облачный deployment
и доступный Agent Engine endpoint ещё не подтверждены.** Старые `deploy-agent.py`
и `query-agent.py` сохраняют свои LOCAL_ONLY ограничения.

## Подготовка локального пакета

Нужно окружение разработки из README с установленными cloud-зависимостями.
Команды выполняются из корня репозитория. Сначала соберите текущие исходники:

```sh
python -m build --no-isolation
```

Для Agent Engine подготовьте отдельный приватный `engine-settings.json` по схеме
`CloudSettings`. Нужны реальные значения команды. `chat.provider` должен быть
`vertex`, с явными project/location/model; project совпадает с `application.project_id`.
API-ключи и токены в этом JSON не поддерживаются. Существующие настройки друзей
не перезаписывайте. Пример команды ниже предполагает уже подготовленный файл:

```sh
python deploy/prepare-agent-engine.py \
  --settings .campuspulse-local/engine-settings.json \
  --wheel dist/campuspulse_recovery-0.1.0-py3-none-any.whl \
  --requirements requirements/cloud-linux.lock \
  --output .validation/agent-engine-bundle
```

CLI ничего не загружает и не разворачивает. Он не читает ADC, не вызывает Google API,
не меняет IAM и не имеет `--execute`. Успех: exit 0 и `prepared_not_deployed`;
ошибка: exit 2 и `blocked`. В stdout нет настроек аккаунта или приватных путей.
Для повторной подготовки выберите новый каталог: существующий не перезаписывается.

Проверяются формат wheel, Python/portable tag, SHA-256 записей RECORD, наличие
runtime и совпадение кода runtime/worker/authority с текущим установленным пакетом.
Поэтому старый wheel после изменения исходников нужно пересобрать. Lock должен
содержать полные закреплённые версии с SHA-256; другие версии ADK/Vertex/cloudpickle
требуют повторной проверки совместимости, а не снятия проверки.

В каталоге будут:

| Файл | Назначение |
|---|---|
| `campuspulse_recovery-…whl` | Проверенный продуктовый wheel |
| `requirements.lock` | Неизменённый исходный cloud lock |
| `deployment-requirements.txt` | Исходные pins и установка wheel по имени с SHA-256 |
| `settings.json` | Проверенная конфигурация без токенов вызовов |
| `create-config.json` | Аргументы создания для закреплённого SDK |
| `operations.json` | Схема операции, полученная настоящим SDK |
| `manifest.json` | Размеры/хеши файлов, статус и список непроверенных облачных условий |

`extra_packages` загружает wheel, а `deployment-requirements.txt` явно устанавливает
его. Оба нужны. Пакет готов только при наличии финального manifest; при обычной
ошибке записи новый неполный каталог удаляется. На POSIX каталог имеет права 0700,
файлы — 0600. `.validation/` и `.campuspulse-local/` исключены из Git; настройки
содержат приватные идентификаторы и не предназначены для публичной презентации.

## Контракт вызова для команды подключений

Runtime предоставляет только:

```python
result = await remote.async_query(invocation=server_grant, service_token=backend_id_token)
```

Это приватный вызов backend, а не запрос из браузера или текст модели.
Платформенный OAuth для Agent Engine и `service_token` worker — разные credentials.
Второй является подписанным Google ID-токеном backend service account с аудиторией
из `agent_origin`. Эта аудитория используется логически: runtime не посылает HTTP
на агентский Cloud Run URL, а обращается к ASGI worker внутри процесса. Worker
по-прежнему проверяет подпись, issuer, audience, principal и время действия токена.

Grant выдаёт существующий backend в Firestore. Он связан с живой сессией, run,
операцией и точными серверными аргументами; повторный захват запрещён. Owner,
session, план, инструменты и ресурсы не выбираются через параметры `async_query`.
Путь `read` читает источники, `recover` запускает ADK, `evidence` читает независимую
проверку. Применение требует прежнего согласования точной версии плана и проверок
актуальности, включая Moodle effort/availability.

Оболочка возвращает JSON:

```json
{"status_code": 200, "payload": {"...": "результат прежнего worker"}}
```

- `200`: читать и проверять прежний тип результата соответствующей операции.
- `403`: `service_or_invocation_rejected`; dispatch не разрешён.
- `503`: `invocation_outcome_unconfirmed`; успех не подтверждён. После записи
  нельзя повторять вызов вслепую — действуют прежние процедуры reconciliation.

Исполняемая часть имеет async-бюджет 125 секунд и лимит ответа 800000 байт.
После отмены worker завершает свои транзакционные проверки и cleanup; это не
гарантия возврата строго за 125 секунд и не доказательство отмены внешней записи.
Отмена вызывающего coroutine передаётся дальше. Ресурсы создаются на вызов;
закрытие Google-клиента отдельно ограничено пятью секундами.

`set_up` и вход в query постоянно подавляют Python SDK logging, как прежняя
точка cloud-запуска: исключения MCP/ADK могут содержать приватные данные.
Typed/redacted журнал Firestore сохраняется. В create config также выключена
платформенная telemetry. Не включайте запись аргументов query, сырых HTTP-тел,
ID-токенов или grants в сторонние логи/трассировку. Токены в runtime не сохраняются
и не попадают в конфигурацию pickle.

## Что должен сделать Мерей в облачной части

1. Проверить проект, billing, регион Agent Engine и доступность выбранной Gemini.
2. Использовать отдельный agent service account из настроек. Подтвердить его
   доступ к Firestore, Vertex и приватному MCP; backend — единственный прикладной
   вызывающий Agent Engine. Права подписания/получения сервисного ID-токена и
   реальный staging bucket также требуют проверки.
3. Установить тот же проверенный wheel в окружение deployer. Из корня подготовленного
   bundle создать `AgentEngineRuntime` по `settings.json` и передать runtime плюс
   аргументы `create-config.json` в закреплённый SDK `vertexai.agent_engines.create`.
   `vertexai.init` должен получать реальные project/location/staging_bucket.
   Подготовщик этот облачный вызов не выполняет.
4. Сохранить фактический `projects/…/locations/…/reasoningEngines/…`, проверить
   `operation_schemas()` и вызвать приватный метод из backend SDK transport.
   Существующий `PlatformAgentClient` со stock `async_stream_query` не является
   адаптером для этой custom operation; его нельзя подменять разбором текста модели.
5. Пройти живой Moodle propose → approval → apply → verify, повтор, отзыв сессии
   и изменение входных данных. Сохранить resource/API и redacted evidence для демо.

Только после этого второй deliverable кейса можно назвать закрытым. Локальный
manifest, Cloud Run endpoint и успешный pytest не доказывают deployment Agent Engine.

## Локальная проверка

```sh
python -m pytest tests/agent_engine -q
```

Тесты используют настоящий закреплённый SDK schema builder, cloudpickle, pip dry-run,
ASGI worker, ADK, локальный MCP transport, solver, grants и approval. Внешние модель,
Calendar, проверка входа и документы синтетические; реальные подключения не проверяются.
Полный прогон с Firestore Emulator описан в README.
Зафиксированный результат проверки: [AGENT-ENGINE-VALIDATION.md](AGENT-ENGINE-VALIDATION.md).

Официальные источники: [custom operations](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/use-a-custom-agent),
[развёртывание и установка wheel](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/deploy-an-agent).
Публичная документация может описывать более новый SDK; локальный контракт здесь
дополнительно проверяется исходниками и тестами версии 1.165.1.
