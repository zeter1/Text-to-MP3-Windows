# Архитектура после context-oriented refactor

Цель архитектуры — не только разделение ответственности, но и **минимальная стоимость AI-контекста** при локальных изменениях.

## Слои

`text_to_mp3.py` — стабильная точка запуска и legacy facade.

`runtime.py`, `sapi.py`, `windows_tools.py`, `problem_logger.py` — компактные compatibility facades. Реальная логика вынесена в owner-модули (`runtime_*`, `sapi_*`, `windows_*`, `problem_logger_*`). Старые импорты сохранены.

`task_*.py` — функциональность одной пользовательской вкладки. Крупные mixin’ы дополнительно собраны из узких частей: UI build/status/state, source tracking/capture/file I/O, preview player/delete/controls.

`app_*.py` — главное окно. Workspace, tabs, persistence, recovery, hotkeys, event dispatch и shutdown имеют отдельных владельцев.

`task_conversion_worker.py` остаётся намеренно цельным транзакционным pipeline. Для него введён range-routing в `docs/WORKER_MAP.md`, чтобы не читать весь метод для локальной стадии.

## Совместимость

Public composition names (`ProblemLogger`, `GlobalCopyHotkeyMonitor`, `TaskUiMixin`, `TaskSourcesMixin`, `TaskPreviewRuntimeMixin`, `AppEventsMixin`, `AppWorkspaceMixin`, `AppRecoveryHotkeysMixin`) сохранены. Cross-mixin вызовы через `self` продолжают работать через MRO.

Пути пользовательских данных по-прежнему находятся рядом с корнем программы (либо fallback storage), а не внутри `tts_app/`. Windows autostart по-прежнему указывает на корневой `text_to_mp3.py`.

## AI navigation contract

- `AGENTS.md` — обязательный старт для Codex.
- `docs/CODEMAP.md` — task → owner routing.
- `docs/SYMBOL_INDEX.md/.json` — symbol → file:range.
- `docs/WORKER_MAP.md` — stage → range для большого conversion worker.
- `tools/where.py` — дешёвый поиск владельца без чтения проекта.
- `tools/verify.py` — единый быстрый gate после правок.
