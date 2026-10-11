# AGENTS.md — экономный контекст Codex

## Быстрый старт

1. **Не сканируй проект целиком.** Сначала открой `docs/CODEMAP.md`.
2. Если известен symbol/метод/текст события: `python tools/where.py <имя>`.
3. Открой owner-файл и только нужный диапазон строк. Для `_job_worker` используй `docs/WORKER_MAP.md`.
4. Расширяй контекст только на прямую зависимость, если без неё нельзя доказать изменение.
5. После правки запускай `python tools/verify.py`.

До import/test/startup изолируй source по [контракту LOGGING_FOR_AI](docs/LOGGING_FOR_AI.md): storage вычисляется от `__file__` и проверяет запись `.write_test`. Полный gate выполняй в source mirror без пользовательских данных; индексы переноси из принятого mirror.

## Архитектурное правило

- `text_to_mp3.py` — стабильная точка входа/compatibility facade. Бизнес-логику туда не добавлять.
- `runtime.py`, `sapi.py`, `windows_tools.py`, `problem_logger.py` и несколько `app_*/task_*` файлов — **facade/composition roots**. Для изменения поведения идти в implementation owner из `docs/CODEMAP.md`.
- Не объединять mixin-модули обратно. Разбиение специально уменьшает AI-context.
- Не превращать маленькие owner-модули в generic «utils.py»/«helpers.py»: ownership должен оставаться очевидным.
- Единственное осознанное исключение по размеру — `task_conversion_worker.py`: это транзакционный pipeline с общим recovery/error state. Читай его по stage-range из `docs/WORKER_MAP.md`.

## Context diet

- Для локальной задачи целевой набор: **1 owner + 0–2 прямые зависимости**.
- Сначала search/symbol index, затем targeted read. Не читать соседние файлы «на всякий случай».
- `docs/SYMBOL_INDEX.md`/`.json` — карта символов; обновляется `python tools/build_code_index.py`.
- `docs/MODULE_SIZES.json` — контроль размера модулей.
- `Настройки программы/`, `Логи проблем/`, `Незавершённые задачи/`, `Бэкапы текста MP3/` — runtime/user data, не source. Не сканировать без прямой задачи по этим данным.
- Для диагностики из `Логи проблем/session_*` сначала читать `AI_READ_FIRST.json`, затем указанный `errors/*.json`; `events*.jsonl` открывать точечно по ID/stage, а не целиком.

## Перед правкой

- Найди exact symbol/text/event через `tools/where.py` или `rg`.
- Проверь owner в `docs/CODEMAP.md`.
- Зафиксируй 1–3 реально затронутых файла.
- Сохраняй public behavior, state formats и compatibility facades, если задача явно не требует миграции.
- Для небольшого изменения не делай unrelated cleanup/refactor.

## После правки

Основной gate:

```bash
python tools/verify.py
```

Он выполняет compileall, smoke helpers, import/facade checks, preview/logger regression tests, module-size budget и регенерацию symbol index.

Для SAPI/hotkey/audio/real FFmpeg claims всё равно нужен Windows/runtime proof; static PASS не равен runtime PASS.
