from __future__ import annotations

import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "text_to_mp3.py"


class RepositoryContractTests(unittest.TestCase):
    def test_main_source_remains_valid_python(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")
        ast.parse(source, filename=str(SOURCE))

    def test_runtime_and_recovery_data_remain_ignored(self) -> None:
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        required_patterns = {
            "Настройки программы/",
            "Логи проблем/",
            "Незавершённые задачи/",
            "Бэкапы текста MP3/",
            "*.wav",
            "*.part.mp3",
        }
        missing = sorted(pattern for pattern in required_patterns if pattern not in gitignore)
        self.assertFalse(missing, f"Runtime/recovery data must stay ignored: {missing}")

    def test_windows_tts_and_ffmpeg_dependencies_are_declared(self) -> None:
        requirements = {
            line.strip().lower()
            for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        self.assertIn("pywin32", requirements)
        self.assertIn("imageio-ffmpeg", requirements)


if __name__ == "__main__":
    unittest.main()
