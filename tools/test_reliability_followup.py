"""Follow-up regressions for bounded UI dispatch and resilient Windows capture."""
from __future__ import annotations

import queue
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tts_app.app_event_dispatch import AppEventDispatchMixin
from tts_app.app_audio_settings import AppAudioSettingsMixin
from tts_app.app_hotkeys import AppHotkeysMixin
from tts_app.task_capture import TaskCaptureMixin
from tts_app.runtime_core import DEFAULT_AUDIO_OUTPUT_LABEL

class Root:
    def __init__(self):
        self.pending = []
    def after(self, ms, callback):
        self.pending.append((ms, callback))
        return len(self.pending)
    def after_cancel(self, id):
        pass

class Var:
    def __init__(self, value): self.value = value
    def get(self): return self.value
    def set(self, value): self.value = value

class Logger:
    def __init__(self): self.events = []; self.errors = []
    def event(self, name, **fields): self.events.append((name, fields))
    def error(self, **fields): self.errors.append(fields)

class DispatchHarness(AppEventDispatchMixin, AppAudioSettingsMixin):
    def __init__(self, output=DEFAULT_AUDIO_OUTPUT_LABEL):
        self.root = Root()
        self.events = queue.Queue()
        self.logger = Logger()
        self.tabs = {}
        self.shutdown_in_progress = False
        self.global_audio_output_var = Var(output)
        self.audio_outputs = []
        self.audio_output_choices = [DEFAULT_AUDIO_OUTPUT_LABEL]
        self._audio_output_scan_running = False
        self.calls = 0
    def _load_audio_outputs_async(self): self.calls += 1

class Tests(unittest.TestCase):
    def test_flood_of_unknown_events_yields_to_tk(self):
        app = DispatchHarness()
        for i in range(400): app.events.put(("not-a-tab", "unknown", i))
        app._process_events()
        self.assertGreater(app.events.qsize(), 0, "Tk event loop monopolized by unlimited queue")
        self.assertLessEqual(app.root.pending[-1][0], 10, "backlog must be drained soon")
        for _ in range(12):
            if not app.events.qsize(): break
            app.root.pending.pop(0)[1]()
        self.assertEqual(app.events.qsize(), 0)

    def test_failed_audio_discovery_with_concrete_output_retries(self):
        app = DispatchHarness("Наушники (HUAWEI FreeBuds Pro 5)")
        app.events.put(("", "audio_outputs_error", {"exc": RuntimeError("COM trouble"), "traceback": "mock"}))
        app._process_events()
        self.assertEqual(len(app.logger.errors), 1)
        self.assertTrue(app._audio_output_missing)
        self.assertEqual(len([x for x in app.root.pending if x[0] == 12000]), 1)

    def test_default_output_discovery_error_is_bounded_retry(self):
        app = DispatchHarness()
        app.events.put(("", "audio_outputs_error", {"exc": RuntimeError("COM trouble"), "traceback": "mock"}))
        app._process_events()
        self.assertTrue(any(ms == 12000 for ms, _ in app.root.pending))

    def _hotkey(self, same_window=True):
        app = types.SimpleNamespace()
        app.root = Root()
        app.shutdown_in_progress = False
        app.logger = Logger()
        captured = []
        task = types.SimpleNamespace(
            workspace_id="tab", task_id="task",
            status_var=Var(""), append_external_selected_text=captured.append,
        )
        monitor = types.SimpleNamespace(
            enabled=True, _capture_generation=3,
            _clipboard_sequence_number=lambda: 50,
            _foreground_capture_context=lambda: {"source_pid": 77 if same_window else 88,
                                                   "source_hwnd": 123 if same_window else 321},
            _hotkey_is_plain_ctrl_c=lambda: False,
            _send_ctrl_c_to_active_window=lambda: emitted.append(True),
            get_hotkey=lambda: "F8",
        )
        emitted = []
        app.tab_hotkey_monitors = {"tab": monitor}
        app._task_by_workspace_id = lambda tabid: task
        app._on_tab_copy_hotkey = lambda tabid, info: AppHotkeysMixin._on_tab_copy_hotkey(app, tabid, info)
        info = {
            "clipboard_changed": False, "capture_generation": 3,
            "clipboard_sequence_before": 50, "source_pid": 77, "source_hwnd": 123,
            "source_process": "Telegram.exe",
        }
        return app, info, emitted, captured

    def test_retry_f8_once_when_source_window_unchanged(self):
        app, info, emitted, captured = self._hotkey()
        app._on_tab_copy_hotkey("tab", info)
        for _ in range(8):
            if not app.root.pending: break
            app.root.pending.pop(0)[1]()
        self.assertEqual(len(emitted), 1, "one bounded Ctrl+C retry only")
        self.assertEqual(captured, [])
        self.assertEqual(sum(name == "global_copy_clipboard_timeout" for name, _ in app.logger.events), 1)

    def test_no_copy_retry_when_foreground_changes(self):
        app, info, emitted, captured = self._hotkey(same_window=False)
        app._on_tab_copy_hotkey("tab", info)
        for _ in range(8):
            if not app.root.pending: break
            app.root.pending.pop(0)[1]()
        self.assertEqual(emitted, [])
        self.assertEqual(captured, [])

    def test_deferred_capture_save_does_not_sync_disk(self):
        saves = []
        task = types.SimpleNamespace(app=types.SimpleNamespace(
            schedule_workspace_save=lambda *args, **kw: saves.append(("async", kw)),
            save_workspace=lambda: saves.append(("sync", {})),
        ))
        TaskCaptureMixin._schedule_capture_persistence(task)
        self.assertEqual(saves[0][0], "async")
        self.assertEqual(saves[0][1]["keep_earliest"], True)

if __name__ == "__main__":
    unittest.main()
