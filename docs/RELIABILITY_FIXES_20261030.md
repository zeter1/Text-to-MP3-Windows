# Отчёт об исправлениях SAPI, clipboard и Tk — 30.10.2026

## Применённые изменения

1. **Bluetooth:** сохранённое устройство вывода не заменяется другим без выбора пользователя; если оно пропало из Windows SAPI, запускаются неблокирующие проверки каждые 12 секунд. Когда устройство найдено, проверка останавливается. Одновременно выполняется максимум одна проверка. После `AUDIO_OUTPUT_NOT_FOUND` список перечитывается сразу. Неподтверждённое чтение не повторяется и текст не удаляется.
2. **Clipboard:** после обычного тайм-аута Ctrl+C до двух дополнительных проверок через `Tk.after(125)`; читать разрешено только действительно обновлённый буфер. Для каждого срабатывания вводится capture generation; старые попытки не поглощают новое Ctrl+C. Ранее существовавшие повторные попытки при временной блокировке остались.
3. **Tk:** после завершения инициализации стадия watchdog называется `idle_tk_mainloop`, а в снимке зависания сохраняется последняя успешно завершённая UI-операция и её возраст (до 30 секунд). Это улучшение диагностики; причина прежнего 10-секундного зависания не доказана.

## Область

Корень: `G:\moi programi na python\Перевод текста в аудио движком от microsoft`.
Использованы как руководство навыки `zeter-problem-logs-web`, `zeter-root-cause-debug`, `zeter-dev-autopilot`, `zeter-work-verifier` и `zeter-code-review-web`. Файлы навыков не изменены. Пользовательские тексты, настройки, MP3, драйверы, настройки Bridgerator не изменялись.

Изменённые файлы: `tts_app/app_audio_settings.py`, `tts_app/app_event_dispatch.py`, `tts_app/app_hotkeys.py`, `tts_app/windows_hotkey_capture.py`, `tts_app/app_workspace_init.py`, `tts_app/ui_stall_diagnostics.py`, `tools/test_preview_audio.py`, `tools/test_ui_diagnostics.py`, `tools/verify.py`, `CHANGELOG.md`, `docs/LOGGING_FOR_AI.md`, три сгенерированных индекса; добавлен `tools/test_runtime_resilience.py`.

## Проверки и доказательства

- `python -B tools/verify.py` в изолированном source mirror — **exit 0 / verify: OK**; 8 UI-tests (1 skipped), 26 preview/hotkey tests, 5 Bluetooth recovery tests, 9 notification tests (5 skipped). Всего 42 PASS, 6 SKIP. Compile, smoke, структура, logger, штатная регенерация индексов — PASS.
- Негативный контроль на baseline: новые Bluetooth-тесты выдали **4 ожидаемых ERROR**, exit 1, показывая, что новая проверка выявляет отсутствие исправления. Исправленная версия: **5 PASS**.
- Windows SAPI enumeration в изолированном mirror: **exit 0**, доступно 9 выходов, HUAWEI FreeBuds Pro 5 присутствует, JBL Tune 520BT отсутствует на момент проверки. Звук не включался.
- Все **15 проверенных файлов совпадают по SHA-256** с исходниками, на которых запускались тесты.
- Резервная копия до изменений: `_maintenance_backups/audio_clipboard_ui_20261030_181955`. SHA: `BASELINE_SHA256.json` и `FINAL_SHA256.json`; тестовый mirror: `validation_source`. Исходники baseline для negative control: `baseline_negative_control`.

## Не подтверждено реальными испытаниями

**NOT VERIFIED:** живое отключение/подключение Bluetooth во время работы программы, слышимость речи, длительное использование горячих клавиш в Telegram, полная GUI-сессия, упакованная EXE. Программа не может исправить сам сбой драйвера Windows. Если внешнее приложение вообще не обновит clipboard, безопасный timeout останется. Фактическая первопричина старого зависания Tk остаётся неизвестной.
