"""Private interpreter resolution tests, without importing application storage."""
from pathlib import Path
import importlib.util
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / 'tts_app' / 'engine_runtime.py'
SPEC = importlib.util.spec_from_file_location('tested_engine_runtime', PATH)
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


class RuntimeTests(unittest.TestCase):
    def test_source_uses_current_interpreter(self):
        with patch.object(runtime.sys, 'frozen', False, create=True):
            self.assertEqual(runtime.engine_python(), sys.executable)

    def test_frozen_uses_only_adjacent_private_interpreter(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            private = base / 'engine_runtime' / 'python.exe'
            private.parent.mkdir()
            private.write_bytes(b'fixture')
            with patch.object(runtime.sys, 'frozen', True, create=True), patch.object(runtime.sys, 'executable', str(base / 'ZeTer-Voice.exe')):
                self.assertEqual(Path(runtime.engine_python()), private)
                private.unlink()
                with self.assertRaisesRegex(RuntimeError, 'Распакуйте'):
                    runtime.engine_python()

    def test_child_environment_drops_machine_python_overrides(self):
        with patch.dict(os.environ, {'PYTHONPATH': 'untrusted', 'PYTHONHOME': 'untrusted'}):
            child = runtime.engine_environment()
            self.assertNotIn('PYTHONPATH', child)
            self.assertNotIn('PYTHONHOME', child)
            self.assertEqual(child['PYTHONNOUSERSITE'], '1')
            self.assertEqual(os.environ['PYTHONHOME'], 'untrusted')


if __name__ == '__main__':
    unittest.main()
