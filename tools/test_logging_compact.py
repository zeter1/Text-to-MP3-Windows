from __future__ import annotations

import json
import tempfile
import traceback
import sys
import shutil
import contextlib
import io
import types
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parent.parent
MIRROR = tempfile.TemporaryDirectory(prefix="tts logger тест ", dir=SOURCE_ROOT)
ROOT = Path(MIRROR.name).resolve()
(ROOT / "tts_app").mkdir()
for source in (SOURCE_ROOT / "tts_app").glob("*.py"):
    shutil.copyfile(source, ROOT / "tts_app" / source.name)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tts_app.problem_logger_session as session_module
from tts_app.problem_logger import ProblemLogger
from tts_app.sapi_preview_diagnostics import PreviewPlaybackError, preview_sapi_snapshot
assert session_module.STORAGE_BASE_DIR.resolve() == ROOT


def main() -> int:
    original_logs_dir = session_module.LOGS_DIR
    try:
        test_root = ROOT / "synthetic logs"
        session_module.LOGS_DIR = test_root
        logger = ProblemLogger()

        for _ in range(80):
            logger.event(
                "text_state",
                task_id="test_task",
                tab_id="test_tab",
                reason="periodic",
                text_chars=100,
                unread_chars=100,
                preview_running=False,
                paused=False,
            )

        for _ in range(2):
            try:
                raise RuntimeError(
                    'Устройство воспроизведения "Fake Headphones" '
                    "сейчас не найдено в Windows SAPI."
                )
            except RuntimeError as exc:
                logger.error(
                    task_id="preview_test",
                    stage="preview",
                    exc=exc,
                    traceback_text=(
                        traceback.format_exc()
                        + "synthetic traceback line\n" * 60
                    ),
                    context={
                        "preview_run_id": "preview_test",
                        "tab_id": "test_tab",
                        "api_token": "FAKE_SECRET_MUST_NOT_LEAK",
                        "long_payload": "x" * 5000,
                    },
                )

        logger.close()
        session_dir = logger.session_dir
        errors = list((session_dir / "errors").glob("*.json"))
        if len(errors) != 1:
            raise AssertionError(
                f"duplicate error files were not grouped: {len(errors)}"
            )

        error_text = errors[0].read_text(encoding="utf-8")
        error_payload = json.loads(error_text)
        if error_payload.get("occurrences") != 2:
            raise AssertionError("error occurrence counter mismatch")
        if error_payload.get("error_code") != "AUDIO_OUTPUT_NOT_FOUND":
            raise AssertionError("known error was not classified")
        if not error_payload.get("likely_cause"):
            raise AssertionError("known error has no likely_cause")
        if not error_payload.get("code_locations"):
            raise AssertionError("traceback code locations were not extracted")
        if "FAKE_SECRET_MUST_NOT_LEAK" in error_text:
            raise AssertionError("secret redaction failed")
        if len(str(error_payload.get("traceback") or "")) > 7100:
            raise AssertionError("traceback bounding failed")

        ai_payload = json.loads(
            (session_dir / "AI_READ_FIRST.json").read_text(encoding="utf-8")
        )
        if (
            (ai_payload.get("primary_problem") or {}).get("error_code")
            != "AUDIO_OUTPUT_NOT_FOUND"
        ):
            raise AssertionError("AI_READ_FIRST lost primary error")
        if int((ai_payload.get("log_volume") or {}).get("events_suppressed") or 0) < 70:
            raise AssertionError("repetitive event suppression is ineffective")

        events_file = session_dir / "events.jsonl"
        if events_file.stat().st_size >= 8 * 1024:
            raise AssertionError("synthetic event log unexpectedly large")

        critical = ProblemLogger()
        critical.event_bytes_written = session_module.EVENTS_SESSION_SOFT_LIMIT_BYTES
        for name in ("preview_started", "preview_sapi_stage", "preview_finished"):
            critical.event(name, preview_run_id="critical-preview", stage="stream_complete")
        records = [json.loads(line) for line in critical.events_file.read_text(encoding="utf-8").splitlines()]
        if [item["event"] for item in records if item.get("preview_run_id") == "critical-preview"] != [
            "preview_started", "preview_sapi_stage", "preview_finished"
        ]:
            raise AssertionError("preview boundary evidence suppressed after soft budget")
        critical.close()

        preview_log = ProblemLogger()
        endpoint_voice = types.SimpleNamespace(AudioOutput=types.SimpleNamespace(
            Id="FAKE-ENDPOINT-ID", GetDescription=lambda: "Fake output",
        ))
        preview_log.event("preview_sapi_stage", preview_run_id="endpoint-identity",
                          sapi_status=preview_sapi_snapshot(endpoint_voice), access_token="FAKE_ACCESS_SECRET")
        endpoint_record = next(item for item in map(json.loads, preview_log.events_file.read_text(
            encoding="utf-8").splitlines()) if item.get("preview_run_id") == "endpoint-identity")
        if endpoint_record["sapi_status"].get("audio_endpoint_id") != "FAKE-ENDPOINT-ID":
            raise AssertionError("actual endpoint identity redacted or missing")
        if endpoint_record.get("access_token") != "<redacted>":
            raise AssertionError("secret token redaction weakened")
        preview_log.event("preview_sapi_stage", preview_run_id="unverified", tab_id="test_tab",
                          stage="stream_completion_claimed", stream_id=1,
                          before_status={"CurrentDevicePosition": 0},
                          sapi_status={"CurrentStreamNumber": 0, "CurrentDevicePosition": 0, "LastHResult": 0})
        preview_log.event("preview_sapi_stage", preview_run_id="unverified", stage="failed")
        preview_log.error(task_id="unverified", stage="preview", exc=PreviewPlaybackError("synthetic"),
                          traceback_text="synthetic", context={"preview_run_id": "unverified", "tab_id": "test_tab"})
        preview_ai = json.loads(preview_log.ai_read_first_file.read_text(encoding="utf-8"))
        if preview_ai["primary_problem"]["error_code"] != "SAPI_PLAYBACK_UNVERIFIED":
            raise AssertionError("unverified playback has no stable AI error classification")
        if not any(item.get("stage") == "stream_completion_claimed" for item in preview_ai["recent_signals"]):
            raise AssertionError("AI entrypoint lost playback divergence stage")
        preview_log.close()

        broken = ProblemLogger()
        broken.events_file = broken.session_dir / "not-a-file"
        broken.events_file.mkdir()
        broken.event("preview_sapi_stage", stage="stream_failed")
        if not broken.disabled:
            raise AssertionError("fixture did not reach event write failure")
        emergency = test_root / "diagnostic_emergency.json"
        if not emergency.exists():
            raise AssertionError("diagnostic write failure was silently lost")
        payload = json.loads(emergency.read_text(encoding="utf-8"))
        if payload.get("diagnostic_integrity") != "degraded":
            raise AssertionError("emergency channel falsely reports healthy logs")
        ai = json.loads(broken.ai_read_first_file.read_text(encoding="utf-8"))
        if ai.get("diagnostic_integrity") != "degraded":
            raise AssertionError("AI entrypoint retained false clean diagnostic state")

        summary_broken = ProblemLogger()
        def fail_summary(status):
            raise OSError("synthetic summary failure")
        summary_broken._write_diagnostic_summary = fail_summary
        summary_broken.last_live_summary_perf = 0
        summary_broken.event("preview_finished", preview_run_id="summary-write-test")
        if "summary-write-test" not in summary_broken.events_file.read_text(encoding="utf-8"):
            raise AssertionError("summary fixture did not append primary event")
        emergency_record = json.loads(emergency.read_text(encoding="utf-8"))
        if emergency_record.get("stage") != "live_summary_write":
            raise AssertionError("summary write failure was silently lost")

        foreign = b"foreign/manual log sentinel"
        emergency.write_bytes(foreign)
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            summary_broken._record_diagnostic_failure("foreign_collision", OSError("private content"))
        if emergency.read_bytes() != foreign:
            raise AssertionError("unknown emergency filename content overwritten")
        if "foreign_collision" not in captured.getvalue() or "private content" in captured.getvalue():
            raise AssertionError("bounded safe stderr fallback failed")

        print("test_logging_compact: OK")
        return 0
    finally:
        session_module.LOGS_DIR = original_logs_dir
        MIRROR.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
