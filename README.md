# CampusPulse Recovery

Python-проект агента для восстановления учебного расписания. Основной стек:
Python 3.12, Google ADK, Gemini через Vertex AI, MCP, FastAPI и Firestore.

Ниже описана установка и локальная проверка. Подключение настоящих Google API
и развёртывание Agent Engine требуют отдельной настройки; локальные тесты
не подтверждают облачный деплой или качество ответов реального Gemini.

Серверный сценарий Moodle: [оценки времени, доступные окна и API планирования](setup/MOODLE-PLANNING.md).

Расписание пар AITU для планирования в чате: [подключение My DU](setup/MY-DU.md).

Следующий облачный этап: [runtime Agent Engine, offline-пакет и контракт для команды подключений](setup/AGENT-ENGINE.md).

Подключение recovery к персональному сайту Moodle: [контракт и включение](setup/RECOVERY-WEB-HANDOFF.md).
Облачная инфраструктура: [handoff GCP](setup/RECOVERY-GCP-HANDOFF.md).

## Установка для разработки и тестов

Требуется Python **3.12**. Выполняйте команды из корня репозитория.

macOS / Linux:

```sh
python3.12 -m venv .venv-test
source .venv-test/bin/activate
```

Windows PowerShell:

```powershell
py -3.12 -m venv .venv-test
.\.venv-test\Scripts\Activate.ps1
```

После активации окружения команды одинаковы:

```sh
python -m pip install --require-hashes -r requirements/test.lock
python -m pip install --no-deps --no-build-isolation .
python -m pip check
```

`test.lock` объединяет зависимости всех Python-компонентов и инструментов
тестирования. Для отдельных развёртываемых сервисов сохраняются их собственные
lock-файлы. Не объединяйте компонентные lock-файлы в одно окружение:
их транзитивные версии могут различаться.

## Основные тесты

### Голосовой ввод в веб-чате

При подключённом `AdkConversationAgent` кнопка «Голосовой ввод» использует ту же
модель и серверные credentials Gemini/Vertex AI, что и чат. Нужны HTTPS (или
localhost для разработки), браузер с MediaRecorder и разрешение на микрофон.
Нажмите кнопку, произнесите сообщение и нажмите «Остановить запись».
Запись автоматически заканчивается через минуту; лимит загрузки — 4 МиБ.
Сервер отправляет аудио в Gemini для расшифровки, не сохраняет запись и добавляет
текст в черновик. Проверьте текст и отправьте сообщение обычной кнопкой.
При смене исходников перезапустите сервер и обновите страницу.

### Запуск проверок

```sh
python -m pytest -q
```

Pytest загружает текущие исходники из `src`. Проверки упаковки отдельно собирают
wheel и проверяют его содержимое. Установка проекта необходима также для проверки
импорта установленного пакета в изолированном дочернем Python.

Без Firestore Emulator интеграционные проверки хранилища пропускаются с объяснением.
Остальные тесты используют локальные тестовые данные и транспорт.

## Проверка настоящих транзакций Firestore локально

Требуются Java 21+ и Node.js 22 или 24 LTS. Следующая команда скачивает официальный
Firebase CLI и Firestore Emulator при первом запуске, выполняет тесты и останавливает
эмулятор. Google-аккаунт и облачный проект для неё не нужны.

```sh
npx --yes firebase-tools@15.34.0 emulators:exec --only firestore --project demo-campuspulse-tests --config tests/firestore-emulator.json "python -m pytest -q"
```

Тестовая конфигурация слушает только `127.0.0.1:8686`. Проверки используют
анонимные credentials и отдельные тестовые пространства данных. Перед запуском
освободите порт, если другой экземпляр эмулятора уже работает.

При конкуренции эмулятор может завершить обе транзакции неопределённым результатом.
Процессный тест проверяет не более одного успешного захвата, сохранённого владельца
и захват свободной блокировки после завершения конкуренции. Это не проверка
гарантированного прогресса под нагрузкой или поведения облачного Firestore.

Документация: [Firebase Local Emulator Suite](https://firebase.google.com/docs/emulator-suite/install_and_configure).

## Offline evaluation

Эти проверки запускаются отдельно: для них нужны собственные сетевые guards.
Не запускайте весь каталог `evals` обычным pytest.

```sh
python -I -S -B evals/recovery/run_evaluation.py --mode offline --output .validation/recovery-review
python -B evals/voice/run_checks.py --mode literal-loopback --report .validation/voice-review/report.json -- evals/voice -q
```

Для повторного recovery evaluation выберите новый каталог `--output`: существующие
результаты намеренно не перезаписываются. SDK берётся из активного Python-окружения;
ручная настройка `PYTHONPATH` или Windows-путей не требуется.

Recovery evaluation использует scripted-модель и тестовые Google-адаптеры.
Voice evaluation проверяет локальные протоколы и инфраструктуру тестов.
Эти результаты не являются измерением качества речи или реальной модели Gemini.

## Сборка и обновление зависимостей

```sh
python -m build --no-isolation
```

Для пересоздания общего тестового lock-файла:

```sh
python -m pip install --require-hashes -r requirements/bootstrap.lock
python -m uv --no-config pip compile pyproject.toml --all-extras --universal --python-version 3.12.13 --constraint requirements/agent-linux.lock --constraint requirements/dev.lock --generate-hashes --no-header --no-annotate --output-file requirements/test.lock
```

После изменения деклараций зависимостей также пересоздайте затронутые компонентные
lock-файлы. В Windows существующий `requirements/resolve.ps1` обновляет компонентные
и общий тестовый lock-файлы через локальный `.tools/uv/bin/uv.exe`.
