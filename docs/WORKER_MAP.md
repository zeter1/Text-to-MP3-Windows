# MP3 WORKER MAP — точечное чтение `_job_worker`

`tts_app/task_conversion_worker.py` намеренно оставлен одним транзакционным pipeline: recovery checkpoints, error context и cleanup используют общее локальное состояние. Вместо рискованного переписывания функции Codex должен читать её по `CODEX-REGION` диапазонам.

| Region | Lines | Что там |
|---|---:|---|
| `worker-bootstrap` | 26–119 | pywin32 init, counters, hashes, base context |
| `split-output-recovery` | 120–307 | split, output dir, FFmpeg discovery, recovery manifest, disk baseline |
| `sapi-generation` | 308–709 | chunk loop, retries, slow diagnostics, disk decisions, WAV checkpoints |
| `ffmpeg-assembly` | 710–942 | concat/transcode, progress callback, process control |
| `validate-success` | 943–1192 | final MP3 validation, snapshots, success events |
| `cancellation` | 1193–1247 | InterruptedError / user cancel path |
| `failure-recovery` | 1248–1401 | error diagnostics, recovery preservation, user message |
| `final-cleanup` | 1402–1411 | stage reset + CoUninitialize |

Быстрый поиск:

```bash
python tools/where.py CODEX-REGION
python tools/where.py report_ffmpeg_progress
```

Если задача относится к одной стадии, открывай только её range + прямой helper (`sapi_*`, `ffmpeg_tools.py`, `runtime_recovery.py` и т.п.). Полный worker нужен только для cross-stage lifecycle/recovery изменений.
