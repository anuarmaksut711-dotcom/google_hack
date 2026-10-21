# Свежая локальная проверка

Дата: 21.10.2026. Ветка: `codex/pitch-ui-demo`, база `origin/main` `4a0858a00b3f1c1c2bf382981b0400f212f06fb8`. Проверки выполнялись в изолированном `.venv-pitch` для текущих локальных изменений интерфейса и regression-тестов.

| Проверка | Подтверждённый результат | Граница |
| --- | --- | --- |
| Full pytest | 2256 passed, 26 skipped, 35 warnings, 248.26 с | 12 пропусков без Firestore Emulator, 14 без PowerShell. Внешние identity/model/Calendar-сервисы синтетические. |
| Recovery evaluation, `offline` | 347 passed, 95 warnings | Scripted model и синтетические Google-адаптеры. |
| Voice evaluation, `literal-loopback` | 53 passed | Локальные протоколы и loopback. Это не качество распознавания речи. |
| Wheel и sdist | Собраны | Проверка упаковки текущего локального кода. |
| `pip check` | Пройден | Установленные зависимости `.venv-pitch`. |
| MCP composition | Пройден | `offline_mcp_composition_ok`, без внешнего cloud tool dispatch. |
| Полный браузерный сценарий | Сохранение 90 минут и окна, перезагрузка, proposal, точное согласие, отдельный Apply, verified, повторная загрузка | Локальный fixture с синтетическими внешними сервисами. Фактические счётчики: 2 Calendar writes, 1 `apply_plan`, 1 `verify_plan`, 8 Calendar reads. |
| Адаптивность | Проверены отрисованные desktop 1280 и narrow 390, переходы к плану и журналу | `clientWidth` 1260/370 из-за вертикального scrollbar 20 px. Горизонтального overflow страницы нет. |
| Console | 0 ошибок и предупреждений в свежей вкладке | Наблюдение в локальном fixture. |

Тестовые числа переданы из завершённых команд текущей сессии подготовки, а не скопированы из checkpoint 20 октября. Эти результаты не подтверждают текущий GCP rollout, два реальных аккаунта, настоящий Gemini или персональный Google Calendar.

Основной браузерный сценарий описан в `DEMO-SCRIPT-RU.md`. Он использует `PYTHONPATH=src:. .venv-pitch/bin/python tests/pitch_ui/browser_fixture.py` и `http://127.0.0.1:8963/fixture/start`. Время fixture заморожено: 17.10.2026 21:00 Asia/Almaty. Баннер явно отмечает синтетический режим.

Для облачной истории используется отдельный `setup/RECOVERY-ROLLOUT-2026-10-20.md`. Его результаты не выдаются за свежую облачную проверку. `setup/PITCH-CLOUD-HANDOFF.md` в исходной базе отсутствовал, текущий personal cloud recovery остаётся неподтверждённым.
