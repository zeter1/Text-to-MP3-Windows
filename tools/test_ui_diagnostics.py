"""Regression tests: large text, UI freeze evidence, exception forwarding."""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tts_app.task_text_performance import TaskTextPerformanceMixin, has_long_line
from tts_app.ui_stall_diagnostics import (
    ExceptionHooks, UiStallMonitor, _stack_evidence,
    acquire_single_instance, release_single_instance,
)


class FakeLogger:
    def __init__(self, path):
        self.session_dir = path
        self.session_id = "synthetic_123"
        self.events = []
        self.errors = []

    def event(self, name, **kwargs):
        self.events.append({"event": name, **kwargs})

    def error(self, **kwargs):
        self.errors.append(kwargs)


class FakeRoot:
    def __init__(self):
        self.scheduled = []
        self.errors = []

    def after(self, delay, callback):
        self.scheduled.append((delay, callback))

    def report_callback_exception(self, cls, value, tb):
        self.errors.append((cls, value))


class FakeText:
    def __init__(self, text=""):
        self.text = text
        self.wrap = None

    def configure(self, **kwargs):
        self.wrap = kwargs.get("wrap")

    def index(self, expr):
        if expr == "end-1c":
            return f"{self.text.count(chr(10)) + 1}.{len(self.text.rsplit(chr(10), 1)[-1])}"
        raise AssertionError(expr)


class FakeVariable:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class FakeScrollbar:
    def __init__(self):
        self.visible = False

    def pack(self, **kwargs):
        self.visible = True

    def pack_forget(self):
        self.visible = False


class TaskHarness(TaskTextPerformanceMixin):
    def __init__(self, logger):
        self.task_id = "task-1"
        self.workspace_id = "tab-1"
        self.app = type("App", (), {"logger": logger})()
        self.text_box = FakeText()
        self._word_wrap_var = FakeVariable(True)
        self.horizontal_scrollbar = FakeScrollbar()
        self._text_metrics_cache = None
        self._display_long_line = False
        self._word_wrap_auto_disabled = False
        self._manual_wrap_large_line = False
        self.reads = 0

    def _full_text(self):
        self.reads += 1
        return self.text_box.text


class Tests(unittest.TestCase):
    def test_long_lines_do_not_change_text_and_disable_expensive_wrap(self):
        text = "A" * 25000 + "\n" + "B" * 12
        self.assertTrue(has_long_line(text))
        self.assertFalse(has_long_line("A" * 2000 + "\n" + "B" * 19000))
        logger = FakeLogger(Path("unused"))
        task = TaskHarness(logger)
        task.configure_text_view_for_content(text, origin="restore")
        self.assertEqual(task.text_box.wrap, "none")
        self.assertTrue(task.horizontal_scrollbar.visible)
        task.text_box.text = text
        m = task._text_metrics(include_hash=True)
        self.assertEqual(m["chars"], len(text))
        self.assertEqual(task._text_metrics()["sentences"], 2)
        self.assertEqual(task.reads, 1)  # cached after identical text-state probes
        self.assertTrue(any(event["event"] == "large_text_view_optimized" for event in logger.events))
        task._word_wrap_var.set(True)
        with patch("tts_app.task_text_performance.messagebox.askyesno", return_value=False):
            task._on_word_wrap_changed()
        self.assertEqual(task.text_box.wrap, "none")  # safe default
        task._word_wrap_var.set(True)
        with patch("tts_app.task_text_performance.messagebox.askyesno", return_value=True):
            task._on_word_wrap_changed()
        self.assertEqual(task.text_box.wrap, "word")  # explicit opt-in
        self.assertFalse(task.horizontal_scrollbar.visible)
        task.text_box.text = "A" * 21000
        task._text_changed_performance()
        self.assertEqual(task.text_box.wrap, "word")  # explicit user approval sticks

    def test_auto_wrap_returns_after_huge_text_is_removed(self):
        logger = FakeLogger(Path("unused"))
        task = TaskHarness(logger)
        huge = "A" * 1_365_218
        task.configure_text_view_for_content(huge, origin="restore")
        task.text_box.text = huge
        self.assertEqual(task.text_box.wrap, "none")
        self.assertTrue(task._word_wrap_auto_disabled)

        # Deletion during reading leaves a regular ~4.7K article.
        article = "Абзац один.\n" + "Текст статьи. " * 350
        task.text_box.text = article
        task._text_changed_performance()
        metrics = task._text_metrics()
        self.assertEqual(metrics["chars"], len(article))
        self.assertEqual(task.text_box.wrap, "word")
        self.assertFalse(task.horizontal_scrollbar.visible)
        self.assertFalse(task._word_wrap_auto_disabled)
        self.assertEqual(task.text_box.text, article)
        self.assertEqual(
            [e["event"] for e in logger.events].count("large_text_wrap_auto_restored"), 1
        )
        # A cached metrics read should not trigger a second transition.
        task._text_metrics()
        self.assertEqual(
            [e["event"] for e in logger.events].count("large_text_wrap_auto_restored"), 1
        )

    def test_auto_wrap_returns_if_many_short_paragraphs_remain(self):
        task = TaskHarness(FakeLogger(Path("unused")))
        task.configure_text_view_for_content("X" * 25000, origin="restore")
        text = ("A" * 1000 + "\n") * 30
        task.text_box.text = text
        task._text_changed_performance()
        task._text_metrics()
        self.assertEqual(task.text_box.wrap, "word")
        self.assertEqual(task.text_box.text, text)

    def test_manual_wrap_after_shrink_has_no_stale_warning(self):
        task = TaskHarness(FakeLogger(Path("unused")))
        task.configure_text_view_for_content("X" * 25000, origin="restore")
        task.text_box.text = "A small article."
        task._text_changed_performance()
        task._word_wrap_var.set(True)
        with patch("tts_app.task_text_performance.messagebox.askyesno") as warning:
            task._on_word_wrap_changed()
        warning.assert_not_called()
        self.assertEqual(task.text_box.wrap, "word")

    def test_explicitly_disabled_wrap_is_not_auto_restored(self):
        task = TaskHarness(FakeLogger(Path("unused")))
        task.configure_text_view_for_content("X" * 25000, origin="restore")
        task._word_wrap_var.set(False)
        task._on_word_wrap_changed()
        task.text_box.text = "A small article."
        task._text_changed_performance()
        task._text_metrics()
        self.assertEqual(task.text_box.wrap, "none")

    def test_watchdog_snapshot_and_recovery_pointer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = FakeRoot()
            logger = FakeLogger(Path(directory))
            monitor = UiStallMonitor(root, logger, threshold_sec=0.12)
            monitor.start()
            try:
                with monitor.operation("synthetic_long_op", text_chars=100,
                                       forbidden_raw_text="THIS_MUST_NOT_BE_LOGGED"):
                    # In real life the UI main thread is stuck in Tk/native code;
                    # test makes no Tk heartbeat until the simulated stall ends.
                    deadline = time.monotonic() + 2.0
                    while monitor._stall is None and time.monotonic() < deadline:
                        time.sleep(0.02)
                self.assertIsNotNone(monitor._stall)
                monitor._heartbeat()
                recovered = [e for e in logger.events if e["event"] == "ui_stall_recovered"]
                self.assertEqual(len(recovered), 1)
                snap_path = logger.session_dir / recovered[0]["snapshot_file"]
                doc = json.loads(snap_path.read_text(encoding="utf-8"))
                self.assertEqual(doc["error_code"], "UI_MAIN_THREAD_STALL")
                self.assertEqual(doc["operation"], "synthetic_long_op")
                self.assertTrue(any(item["role"] == "tk_main" for item in doc["threads"]))
                self.assertNotIn("THIS_MUST_NOT_BE_LOGGED", snap_path.read_text(encoding="utf-8"))
                self.assertFalse(any("filename" in frame for item in doc["threads"] for frame in item["frames"]))
                self.assertGreaterEqual(recovered[0]["stall_duration_ms"], 120)
            finally:
                monitor.stop()
                if monitor._thread:
                    monitor._thread.join(timeout=2)

    def test_uncaught_hooks_log_and_restore_prior_handlers(self):
        root = FakeRoot()
        logger = FakeLogger(Path("unused"))
        original_thread, original_sys = threading.excepthook, sys.excepthook
        hooks = ExceptionHooks(root, logger)
        try:
            try:
                raise ValueError("synthetic-error")
            except ValueError:
                cls, val, tb = sys.exc_info()
                root.report_callback_exception(cls, val, tb)
                hooks._record("synthetic_worker", cls, val, tb, thread_name="example")
            self.assertEqual(len(logger.errors), 2)
            self.assertEqual(logger.errors[0]["stage"], "tk_callback_uncaught")
            self.assertEqual(len(root.errors), 1)
        finally:
            hooks.uninstall()
        self.assertIs(threading.excepthook, original_thread)
        self.assertIs(sys.excepthook, original_sys)

    def test_non_windows_mutex_is_noop(self):
        if sys.platform == "win32":
            self.skipTest("Test covers non-Windows fallback only")
        lock, already_running = acquire_single_instance()
        self.assertIsNone(lock)
        self.assertFalse(already_running)
        release_single_instance(lock)


if __name__ == "__main__":
    unittest.main()
