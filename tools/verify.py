from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

COMMANDS = [
    [sys.executable, "-m", "compileall", "-q", "text_to_mp3.py", "tts_app"],
    [sys.executable, "tools/smoke_refactor.py"],
    [sys.executable, "tools/verify_structure.py"],
    [sys.executable, "tools/test_logging_compact.py"],
    [sys.executable, "tools/test_ui_diagnostics.py"],
    [sys.executable, "tools/test_preview_audio.py"],
    [sys.executable, "tools/test_extra_voices.py"],
    [sys.executable, "tools/test_engine_manager.py"],
    [sys.executable, "tools/test_engine_runtime.py"],
    [sys.executable, "tools/test_runtime_resilience.py"],
    [sys.executable, "tools/test_reliability_followup.py"],
    [sys.executable, "tools/test_conversion_notifications.py"],
    [sys.executable, "tools/build_code_index.py"],
]


def main() -> int:
    for command in COMMANDS:
        print("+", " ".join(command))
        completed = subprocess.run(command, cwd=ROOT)
        if completed.returncode:
            return completed.returncode
    print("verify: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
