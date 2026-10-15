# Логи проблем — контракт для ChatGPT / Codex

Цель: сохранить **decision-driving evidence**, а не максимальный объём текста. Логи должны позволять быстро найти first divergence, stage, owner и конкретный traceback, не читая всю сессию.

## Порядок чтения

1. `Логи проблем/session_*/AI_READ_FIRST.json`
   Перед выводом о чистой сессии проверить `Логи проблем/diagnostic_emergency.json`: независимая запись о сбое логирования имеет приоритет для указанного `session_id`.
2. `primary_problem.file` → конкретный `errors/*.json`
3. `task_<run_id>.json`, если проблема относится к созданию MP3
4. `events*.jsonl` только точечно по `run_id`, `preview_run_id`, `tab_id`, `stage` или времени
5. `diagnostic_summary.json` — compatibility-указатель, а не основной подробный отчёт

## Диагностика зависания GUI (4.2.3)

Если интерфейс не отвечает более ~3 секунд, независимый `TkUiStallWatchdog` сохраняет
`session_*/hangs/hang_YYYYMMDD_HHMMSS_<id>.json` **без обращения к Tk и без lock обычного логгера**.
Там есть: `error_code=UI_MAIN_THREAD_STALL`, задержка в миллисекундах на момент снимка,
`operation` и `operation_context`, текущая версия, идентификатор сессии,
стек главного потока Tk + до 7 фоновых потоков (`module`, `function`, `line`).
Ограничение — максимум 5 снимков на сессию, один на непрерывное зависание;
исходный текст, локальные переменные, содержимое clipboard, строки исходного кода
и полные файловые пути не сохраняются. При блокировке GIL снимок может задержаться.

После восстановления Tk пишет `ui_stall_recovered` с `snapshot_file` и
`stall_duration_ms` в `events.jsonl`. Долгие UI-операции отмечаются
`ui_operation_slow` с продолжительностью и числовыми размерами входных данных;
похожие сигналы агрегируются не чаще раза в 10 секунд на этап.
`AI_READ_FIRST.json` содержит `ui_stalls.latest_snapshots` и указатели в
`open_next_if_needed`, а `health` становится как минимум warning при обнаруженных
hang-файлах. **Если программа зависла окончательно или была принудительно закрыта,
summary может остаться старым: проверьте сам каталог `hangs/` напрямую.**

Необработанные исключения Tk, потоков и процесса пишутся с кодами
`TK_CALLBACK_UNCAUGHT`, `WORKER_THREAD_UNCAUGHT`, `PROCESS_UNCAUGHT`
в `errors/*.json`; прежнее стандартное сообщение Python при этом сохраняется.
Свой класс ошибки и traceback важнее любого предполагаемого `likely_cause`.

Инструмент записи НЕ является crash dump: при завершении питания/kill -9,
системном зависании, заблокированном GIL или недоступном диске файлы
могут не появиться. Это не доказывает, что зависания не было.

## Большой текст и предотвращение подвисаний

Tk автоматически отключает визуальный перенос длинной логической строки
(порог 20 000 символов), но не меняет пользовательский текст для чтения или
сохранения. Горизонтальная прокрутка доступна в окне. Событие
`large_text_view_optimized` сообщает размеры без записи содержимого.
Подсчёт предложений/символов и SHA-16 кешируются между модификациями; не
перечитываются повторно только ради очередного `text_state`.
`ui_operation_slow` отмечает `restore_tab_text`, `workspace_save`,
`queue_text_metrics`, `log_text_state_metrics`, `append_captured_text` и др.
Первопричину следующего медленного эпизода ищите по `hang.operation` и
последним `ui_operation_slow`, а не по совпадению времени с SAPI.

## Error record schema

Ключевые поля:

- `error_code` — стабильная короткая классификация (`AUDIO_OUTPUT_NOT_FOUND`, `FFMPEG_FAILURE`, …);
- `stage` — этап, где failure стал наблюдаемым;
- `likely_cause` — диагностическая гипотеза, не доказанный fix;
- `owner_module` — первый файл, который стоит открыть Codex;
- `code_locations` — последние релевантные `file / line / function` из traceback;
- `fingerprint` + `occurrences` — одинаковые ошибки одной сессии группируются;
- `context` — bounded/sanitized контекст;
- `traceback` — bounded traceback, приоритет у последних frames;
- `suggested_checks` — короткий следующий диагностический шаг.

## Чтение через SAPI

`preview_sapi_stage` связывает COM init, создание голоса/перечень доступных выходов, выбор выхода, отправку блока, completion claim, verified/failure, pause/resume/stop и cleanup по `preview_run_id`, `tab_id`, thread ID и elapsed. Логи содержат длину/хеш блока, номер блока и возвращённый stream ID; исходный текст не записывается. Перечень выходов ограничен 12 именами по 320 символов. Snapshot хранит token ID/description, доступный аудиоформат, current/queued stream, RunningState, LastHResult и позиции аудиовывода. Ошибка чтения диагностического поля сохраняется классом исключения.

`WaitUntilDone=True` и `LastHResult=0` сами по себе не доказывают чтение. Перед `segment_done` проверяются совпадение CurrentStreamNumber с ID, который вернул Speak, отсутствие failed HRESULT и положительный прирост CurrentDevicePosition относительно snapshot перед этим блоком. ID может повторяться между блоками; одного ID недостаточно. Поллинг текста используется для подсветки/закладки, а удаление подтверждается целиком после успешного блока. При ошибке или Stop неподтверждённый блок сохраняется. UI принимает только `playback_verified` для текущего run ID.

Ошибка без положительного подтверждения имеет код `SAPI_PLAYBACK_UNVERIFIED`; `likely_cause` не устанавливает аппаратную причину. По run ID сначала сравнить `stream_submitted` → `stream_completion_claimed` → `failed`, затем `cleanup_started/completed/failed` и `com_released`. HRESULT/позиции устройства подтверждают доступный программный boundary; слышимость, качество звука и состояние Bluetooth требуют отдельного runtime evidence.

Cleanup делает Resume при паузе, purge и WaitUntilDone с пределом 1000 мс; вторичная ошибка не заменяет первичную. COM wrappers и неисполняющиеся traceback frames освобождаются до CoUninitialize. В UI queue передаётся исключение без живого traceback/cause/context и заранее сохранённый текст traceback.

## Размер и шум

- `text_state` полностью поддерживает latest-state в summary, но в raw timeline пишется редко: checkpoint или переход важных флагов.
- Идентичные низкосигнальные события дедуплицируются; количество подавленных событий остаётся в `log_volume.suppressed_by_type`.
- Routine events имеют soft budget; high-value события (`error`, task final status, low disk, recovery, forced shutdown и т.п.) не отбрасываются из-за budget.
- `preview_started`, `preview_finished` и `preview_sapi_stage` являются high-value; heartbeat остаётся редким routine event, максимум раз в существующий 15-секундный интервал. Поллинги аудио не создают поток raw events.
- Raw event file вращается небольшими частями вместо гигантского файла.
- Успешные `task_<run_id>.json` хранят compact summary; failed task сохраняет больше evidence.
- Диагностические JSON пишутся compact UTF-8 без formatting whitespace.

## Целостность диагностики

Event/error/init/close и live-summary write failure выключает основной logger и устанавливает `diagnostic_integrity=degraded`, отдельно от результата продукта. `AI_READ_FIRST` и compatibility summary содержат этот verdict, если их ещё можно записать. Независимый bounded файл `diagnostic_emergency.json` содержит session ID, stage, класс исключения и errno/winerror; сообщение исключения, пользовательский текст и пути отсутствуют. Один файл заменяется только при распознанной собственной записи schema=1; неизвестное, повреждённое или слишком большое содержимое сохраняется. Если независимый файл недоступен, применяется best-effort stderr; в pythonw без console сохранение fallback на диск не гарантировано. Retry нет.

Существующее хранение сессий (`LOG_RETENTION_DAYS`/`LOG_MAX_SESSIONS` в `runtime_core.py`) и форматы настроек этой задачей не меняются.

## Privacy / data safety

- Полный исходный пользовательский текст не является routine log payload.
- `failed_chunk` сохраняется только bounded-фрагментом и только при конкретной ошибке.
- Очевидные credential keys (`token`, `password`, `secret`, `authorization`, cookies/API keys) редактируются до записи.
- Длинные строки/коллекции ограничиваются; для длинных строк сохраняется длина и short hash.
- Пути внутри каталога программы и home могут нормализоваться до `<APP_DIR>` / `<HOME>`.

## Правило для исправления бага

`likely_cause` — только гипотеза. Перед patch проверить `error_code → code_locations → recent_signals → traceback/context`. Не менять соседние подсистемы без evidence.

## Проверка после изменения logger

```bash
python tools/verify.py
```

Gate запускать в source mirror: импорты вычисляют storage от `__file__` и создают `.write_test`; новый cwd или TEMP не изолируют пользовательские данные. Копировать только `text_to_mp3.py`, `tts_app/*.py`, `tools/*.py`, без runtime/user data. Generated indexes после принятого gate переносить из зеркала, не редактировать вручную.

В gate входят `tools/test_logging_compact.py` (dedup/grouping/redaction, critical events после budget, error classification, event/summary write failure, emergency/collision) и `tools/test_ui_diagnostics.py` (снимки зависания, приватность, uncaught hooks, огромные строки), `tools/test_preview_audio.py` (failed async без acknowledgement, healthy/partial/Stop, повторный stream ID без audio delta, UI guard, COM lifetime и exception queue). Эти тесты используют свои synthetic source mirrors и fake COM; реальный GUI/SAPI/устройство/EXE ими не проверяется.
