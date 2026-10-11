# CODEMAP — куда идти Codex

Главное правило: **не читать весь проект для локальной задачи**. Сначала найти symbol через `python tools/where.py <имя>`, затем открыть owner-файл ниже. Фасады нужны для стабильных импортов, но обычно не являются местом правки.

| Что меняется | Implementation owner | При необходимости рядом |
|---|---|---|
| запуск программы / composition приложения | `text_to_mp3.py`, `tts_app/app.py` | — |
| constants, common formatting/process helpers | `tts_app/runtime_core.py` | `tts_app/runtime.py` только для compatibility export |
| compact text diagnostics | `tts_app/runtime_diagnostics.py` | `runtime_core.py` |
| WAV/ffprobe/final MP3 validation/disk forecast | `tts_app/runtime_audio.py` | `ffmpeg_tools.py` |
| recovery scan / existing WAV checkpoints | `tts_app/runtime_recovery.py` | `runtime_audio.py` |
| Ctrl+C/V/X/A/Z/Y Tk shortcuts | `tts_app/runtime_shortcuts.py` | — |
| paths/settings/atomic write/backups/help files | `tts_app/runtime_storage.py` | `runtime_core.py` |
| compatibility imports старого `runtime` | `tts_app/runtime.py` | implementation owner выше |
| logger compact/redaction/error classification | `tts_app/problem_logger_compact.py` | `problem_logger_events.py`, `docs/LOGGING_FOR_AI.md` |
| logger session lifecycle / stale sessions | `tts_app/problem_logger_session.py` | `problem_logger.py` facade |
| logger warnings/observations/diagnostic insights | `tts_app/problem_logger_insights.py` | `problem_logger_summary.py` |
| AI_READ_FIRST / diagnostic summary | `tts_app/problem_logger_summary.py` | `problem_logger_compact.py`, insights/events |
| logger events/stats/errors/task lifecycle | `tts_app/problem_logger_events.py` | — |
| разбиение текста / bookmark segment helpers | `tts_app/text_processing.py` | — |
| SAPI voices/devices/synthesize WAV | `tts_app/sapi_core.py` | `runtime_core.py` |
| SAPI live preview stream / подтверждение блока | `tts_app/sapi_preview.py` | `sapi_core.py`, `text_processing.py` |
| SAPI scalar snapshots / audio-delta oracle / exception COM lifetime | `tts_app/sapi_preview_diagnostics.py` | `sapi_preview.py`, `docs/LOGGING_FOR_AI.md` |
| FFmpeg executable/concat/final MP3 | `tts_app/ffmpeg_tools.py` | `runtime_audio.py` |
| Windows autostart / открыть папку | `tts_app/windows_autostart.py` | `runtime_storage.py` |
| hotkey config/parse/enable/duplicate state | `tts_app/windows_hotkey_config.py` | `windows_hotkeys.py` composition |
| clipboard capture/context/Ctrl+C injection | `tts_app/windows_hotkey_capture.py` | hotkey config |
| local binding / low-level Windows hook loop | `tts_app/windows_hotkey_hook.py` | hotkey config |
| закрываемые вкладки notebook | `tts_app/widgets.py` | — |
| создание UI одной task-вкладки | `tts_app/task_ui_build.py` | `task_ui_state.py` |
| counters/queue/text-state/delete logging | `tts_app/task_ui_status.py` | — |
| voice/title/workspace record/task UI state | `tts_app/task_ui_state.py` | — |
| MP3 source snapshot/completion tracking | `tts_app/task_source_tracking.py` | — |
| global selection + clipboard paste | `tts_app/task_capture.py` | `windows_hotkey_capture.py` |
| загрузить файл / очистить / получить весь текст | `tts_app/task_file_io.py` | `sapi_core.py` для read_text_file |
| preview bookmark/selection/start state | `tts_app/task_preview_state.py` | `text_processing.py` |
| preview worker/start/done | `tts_app/task_preview_player.py` | `sapi_preview.py` |
| удаление прочитанного + appended tail | `tts_app/task_preview_delete.py` | preview player |
| pause/resume/stop/final preview UI state | `tts_app/task_preview_controls.py` | preview player |
| кнопки/start/cancel/recovery создания MP3 | `tts_app/task_conversion_controls.py` | worker только при pipeline-проблеме |
| тяжёлый pipeline `_job_worker` | `tts_app/task_conversion_worker.py` | **сначала `docs/WORKER_MAP.md`**, затем SAPI/FFmpeg/runtime owner |
| главное окно: init/build/restore workspace | `tts_app/app_workspace_init.py` | `task_tab.py` |
| создать/переименовать/закрыть вкладку | `tts_app/app_tabs.py` | workspace init |
| save/debounce workspace | `tts_app/app_workspace_save.py` | runtime storage |
| recovery dialogs | `tts_app/app_recovery.py` | runtime recovery |
| назначение/активация tab hotkeys | `tts_app/app_hotkeys.py` | `windows_hotkey_*` |
| голоса/устройства/общие audio settings UI | `tts_app/app_audio_settings.py` | `sapi_core.py` |
| обработка app event queue | `tts_app/app_event_dispatch.py` | producer конкретного события |
| постоянные уведомления о готовом MP3 / показать файл | `tts_app/conversion_notifications.py` | `app_event_dispatch.py` принимает `done` на Tk thread |
| shutdown / close / background threads | `tts_app/app_shutdown.py` | event dispatch |

## Composition/facade files

Обычно их **не надо читать глубоко**: `runtime.py`, `sapi.py`, `windows_tools.py`, `problem_logger.py`, `task_ui.py`, `task_sources.py`, `task_preview_runtime.py`, `app_events.py`, `app_workspace.py`, `app_recovery_hotkeys.py`, `task_tab.py`, `app.py`. Они существуют ради совместимости/MRO и указывают на implementation owner.

## Маршрут изменения

1. `python tools/where.py <symbol-or-text>`.
2. Открыть owner и нужный line range.
3. При необходимости открыть максимум 1–2 прямые зависимости.
4. Для `_job_worker` сначала выбрать stage-range по `docs/WORKER_MAP.md`.
5. После правки: `python tools/verify.py`.
