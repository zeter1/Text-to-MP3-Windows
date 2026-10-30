"""Synthetic regression tests for Bluetooth output recovery and safe UI polling.
Run only from the project source mirror; no Windows SAPI or user storage touched.
"""
from __future__ import annotations

import queue
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tts_app.app_audio_settings import AppAudioSettingsMixin
from tts_app.app_event_dispatch import AppEventDispatchMixin
from tts_app.runtime_core import DEFAULT_AUDIO_OUTPUT_LABEL


class FakeVar:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class FakeRoot:
    def __init__(self):
        self.scheduled = {}
        self.cancelled = []
        self.next_id = 0

    def after(self, ms, callback):
        self.next_id += 1
        self.scheduled[self.next_id] = (ms, callback)
        return self.next_id

    def after_cancel(self, task_id):
        self.cancelled.append(task_id)
        self.scheduled.pop(task_id, None)


class FakeLogger:
    def __init__(self):
        self.events = []
        self.errors = []

    def event(self, event, **fields):
        self.events.append((event, fields))

    def error(self, **fields):
        self.errors.append(fields)


class AudioHarness(AppAudioSettingsMixin, AppEventDispatchMixin):
    def __init__(self, choice):
        self.root = FakeRoot()
        self.logger = FakeLogger()
        self.events = queue.Queue()
        self.tabs = {}
        self.shutdown_in_progress = False
        self.global_audio_output_var = FakeVar(choice)
        self.audio_outputs = []
        self.audio_output_choices = [DEFAULT_AUDIO_OUTPUT_LABEL]
        self._audio_output_scan_running = False
        self.scan_count = 0

    def _load_audio_outputs_async(self):
        self.scan_count += 1

    def loaded(self, outputs):
        self.events.put(("", "audio_outputs_loaded", {
            "outputs": outputs, "current": "",
        }))
        self._process_events()


class AudioRecoveryTests(unittest.TestCase):
    OUTPUT = "Наушники (HUAWEI FreeBuds Pro 5)"

    def test_missing_device_retained_and_rechecked_nonblocking(self):
        app = AudioHarness(self.OUTPUT)
        app.loaded(["Realtek speakers"])
        self.assertEqual(app.global_audio_output_var.get(), self.OUTPUT)
        self.assertTrue(app._audio_output_missing)
        self.assertIn(self.OUTPUT, app.audio_output_choices)
        self.assertEqual(sum(d == 12000 for d, _ in app.root.scheduled.values()), 1)
        timer_id = app._audio_recheck_after_id
        delay, callback = app.root.scheduled.pop(timer_id)
        callback()
        self.assertEqual(app.scan_count, 1)
        self.assertEqual(app.global_audio_output_var.get(), self.OUTPUT)
        self.assertFalse(any(n == "audio_output_reconnected" for n, _ in app.logger.events))

    def test_reconnection_recognized_without_default_fallback(self):
        app = AudioHarness(self.OUTPUT)
        app.loaded([])
        scheduled_id = app._audio_recheck_after_id
        app.loaded([self.OUTPUT, "Realtek speakers"])
        self.assertFalse(app._audio_output_missing)
        self.assertIsNone(app._audio_recheck_after_id)
        self.assertIn(scheduled_id, app.root.cancelled)
        self.assertEqual(app.global_audio_output_var.get(), self.OUTPUT)
        self.assertEqual(sum(n == "audio_output_unavailable" for n, _ in app.logger.events), 1)
        self.assertEqual(sum(n == "audio_output_reconnected" for n, _ in app.logger.events), 1)

    def test_system_default_never_triggers_reconnect_loop(self):
        app = AudioHarness(DEFAULT_AUDIO_OUTPUT_LABEL)
        app.loaded(["Realtek speakers"])
        self.assertFalse(app._audio_output_missing)
        self.assertFalse(any(ms == 12000 for ms, _ in app.root.scheduled.values()))

    def test_windows_endpoint_rename_rebinds_saved_choice(self):
        app = AudioHarness("Наушники (3- HUAWEI FreeBuds Pro 5)")
        app.loaded(["Наушники (4- HUAWEI FreeBuds Pro 5)"])
        self.assertEqual(app.global_audio_output_var.get(), "Наушники (4- HUAWEI FreeBuds Pro 5)")
        self.assertFalse(app._audio_output_missing)

    def test_concurrent_audio_scans_are_coalesced(self):
        app = AudioHarness(self.OUTPUT)
        starts = []
        class FakeThread:
            def __init__(self, **fields):
                starts.append(fields)
            def start(self):
                pass
        with patch("tts_app.app_audio_settings.threading.Thread", FakeThread):
            AppAudioSettingsMixin._load_audio_outputs_async(app)
            AppAudioSettingsMixin._load_audio_outputs_async(app)
        self.assertTrue(app._audio_output_scan_running)
        self.assertEqual(len(starts), 1)


if __name__ == "__main__":
    unittest.main()
