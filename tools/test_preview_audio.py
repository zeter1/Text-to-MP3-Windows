from __future__ import annotations

import shutil
import sys
import tempfile
import threading
import types
import unittest
import queue
import weakref
from pathlib import Path
from unittest.mock import patch

# __file__, not cwd, owns storage. Import only a source mirror: no user state.
SOURCE_ROOT = Path(__file__).resolve().parents[1]
MIRROR = tempfile.TemporaryDirectory(prefix="tts preview тест ", dir=SOURCE_ROOT)
ROOT = Path(MIRROR.name).resolve()
(ROOT / "tts_app").mkdir()
for source in (SOURCE_ROOT / "tts_app").glob("*.py"):
    shutil.copyfile(source, ROOT / "tts_app" / source.name)
sys.path.insert(0, str(ROOT))
from tts_app import runtime
from tts_app.sapi_preview import preview_sapi_text
from tts_app.task_preview_player import TaskPreviewPlayerMixin
from tts_app.app_hotkeys import AppHotkeysMixin
from tts_app.sapi_preview_diagnostics import preview_diagnostic, PreviewPlaybackError
assert runtime.STORAGE_BASE_DIR.resolve() == ROOT


class FakeVoice:
    def __init__(self, mode="failed"):
        self.mode = mode
        self.step = 0
        self.sink = None
        self.closed = False
        self.spoken_count = 0
        self.AudioOutputStream = types.SimpleNamespace(Status=types.SimpleNamespace(
            CurrentDevicePosition=0, CurrentSeekPosition=0, State=0,
        ))

    @property
    def Status(self):
        failed = self.mode == "failed"
        return types.SimpleNamespace(
            CurrentStreamNumber=0 if failed else 1,
            LastStreamNumberQueued=1, RunningState=0 if failed else 2,
            LastHResult=0, InputWordPosition=0 if failed else self.step * 30,
            InputWordLength=0 if failed else 5, InputSentencePosition=0,
        )

    def Speak(self, text, flags):
        if not text:
            self.closed = True
            if self.mode == "cleanup_failure":
                raise RuntimeError("synthetic cleanup failure")
            return 2
        self.spoken_count += 1
        if self.sink and self.mode != "failed":
            self.sink.OnStartStream(1, 0)
        return 1

    def WaitUntilDone(self, timeout):
        if self.closed or self.mode == "failed":
            return True
        self.step += 1
        if self.mode != "second_failed" or self.spoken_count == 1:
            self.AudioOutputStream.Status.CurrentDevicePosition = self.step * 100
        if self.sink:
            self.sink.OnWord(1, self.step * 100, 0, 5)
        if self.mode == "partial":
            if self.step == 1:
                return False
            raise RuntimeError("synthetic output failure")
        if self.sink:
            self.sink.OnEndStream(1, 200)
        return True

    def Resume(self):
        pass

    def Pause(self):
        pass


class PreviewTests(unittest.TestCase):
    def run_preview(self, voice, stop=None, choose_voice=None, watched_tokens=(), diagnostic=None):
        done = []
        teardown_observations = []
        pythoncom = types.ModuleType("pythoncom")
        pythoncom.CoInitialize = lambda: None
        def uninitialize():
            owner_frame = sys._getframe(1)
            teardown_observations.append((
                owner_frame.f_locals["voice"] is not None,
                owner_frame.f_locals["status"] is not None,
                any(token() is not None for token in watched_tokens),
            ))
        pythoncom.CoUninitialize = uninitialize
        pythoncom.PumpWaitingMessages = lambda: None
        client = types.ModuleType("win32com.client")
        client.Dispatch = lambda name: voice
        def with_events(obj, cls):
            obj.sink = cls()
            obj.sink.close = lambda: None
            return obj.sink
        client.WithEvents = with_events
        dynamic = types.ModuleType("win32com.client.dynamic")
        dynamic.Dispatch = lambda obj: obj
        client.dynamic = dynamic
        win32com = types.ModuleType("win32com")
        win32com.client = client
        modules = {"pythoncom": pythoncom, "win32com": win32com, "win32com.client": client,
                   "win32com.client.dynamic": dynamic}
        with patch.dict(sys.modules, modules), patch("tts_app.sapi_preview.set_voice_by_description", side_effect=choose_voice), patch(
            "tts_app.sapi_preview.set_audio_output_by_description", return_value="Fake JBL"
        ):
            try:
                result = preview_sapi_text(
                    "Первое предложение. Второе предложение.", "Fake Voice", "Fake JBL",
                    0, 0, 100, stop or threading.Event(), threading.Event(),
                    segment_done_callback=done.append,
                    diagnostic_callback=diagnostic,
                )
                error = None
            except Exception as exc:
                result, error = None, exc
        # Assertions live OUTSIDE the product boundary that preserves a primary
        # failure. A fake teardown assertion inside it could be swallowed.
        self.assertEqual(teardown_observations, [(False, False, False)], "COM reference survives teardown")
        return result, done, error

    def test_failed_async_does_not_acknowledge_or_delete_text(self):
        result, done, error = self.run_preview(FakeVoice())
        self.assertEqual(done, [], "failed async output falsely acknowledges read text")
        self.assertIsNone(result)
        self.assertIsNotNone(error)

    def test_healthy_stream_acknowledges_all_sentences(self):
        result, done, error = self.run_preview(FakeVoice("healthy"))
        self.assertIsNone(error)
        self.assertEqual([item["index"] for item in done], [1, 2])
        self.assertEqual(result["completed_chunks"], 2)

    def test_partial_failure_does_not_acknowledge_unfinished_stream(self):
        result, done, error = self.run_preview(FakeVoice("partial"))
        self.assertIsNone(result)
        self.assertIsNotNone(error)
        self.assertEqual(done, [])

    def test_stop_before_stream_preserves_text(self):
        stop = threading.Event()
        stop.set()
        result, done, error = self.run_preview(FakeVoice("healthy"), stop)
        self.assertIsNone(error)
        self.assertTrue(result["stopped"])
        self.assertEqual(done, [])

    def test_unverified_ack_cannot_reach_ui_delete_boundary(self):
        tab = types.SimpleNamespace(
            session_read_segments=0, session_read_chars=0,
            current_preview_run_id="current",
            schedule_queue_status_refresh=lambda: None,
            delete_read_text_var=types.SimpleNamespace(get=lambda: True),
            _delete_pending_read_segment=lambda: self.fail("unverified text deleted"),
        )
        TaskPreviewPlayerMixin.handle_preview_segment_done(tab, {"text": "Unread", "preview_run_id": "current"})
        self.assertEqual(tab.session_read_segments, 0)

    def test_stale_ack_cannot_increment_current_read_counters(self):
        tab = types.SimpleNamespace(
            session_read_segments=0, session_read_chars=0,
            current_preview_run_id="current",
            schedule_queue_status_refresh=lambda: None,
            delete_read_text_var=types.SimpleNamespace(get=lambda: False),
        )
        TaskPreviewPlayerMixin.handle_preview_segment_done(tab, {
            "text": "Old", "preview_run_id": "old", "playback_verified": True,
        })
        self.assertEqual(tab.session_read_segments, 0)

    def test_reused_stream_id_does_not_verify_second_block_without_audio_delta(self):
        with patch("tts_app.sapi_preview.PREVIEW_SAPI_BLOCK_MAX_CHARS", 25):
            result, done, error = self.run_preview(FakeVoice("second_failed"))
        self.assertIsNone(result)
        self.assertIsNotNone(error)
        self.assertEqual([item["index"] for item in done], [1])

    def test_short_stream_finishing_between_polls_is_verified(self):
        result, done, error = self.run_preview(FakeVoice("healthy"))
        self.assertIsNone(error)
        self.assertTrue(all(item["playback_verified"] for item in done))
        tab = types.SimpleNamespace(
            session_read_segments=0, session_read_chars=0, current_preview_run_id="current",
            schedule_queue_status_refresh=lambda: None,
            delete_read_text_var=types.SimpleNamespace(get=lambda: False),
        )
        for item in done:
            TaskPreviewPlayerMixin.handle_preview_segment_done(tab, {**item, "preview_run_id": "current"})
        self.assertEqual(tab.session_read_segments, 2)
        self.assertEqual(tab.session_read_chars, 38)

    def test_cleanup_error_is_observable_after_valid_stream(self):
        result, done, error = self.run_preview(FakeVoice("cleanup_failure"))
        self.assertIsNone(result)
        self.assertEqual(len(done), 2)
        self.assertEqual(str(error), "synthetic cleanup failure")

    def test_helper_com_token_released_before_uninitialize(self):
        watched = []
        class Token:
            pass
        def fail_selection(voice, description):
            token = Token()
            watched.append(weakref.ref(token))
            raise RuntimeError("synthetic selection failure")
        result, done, error = self.run_preview(FakeVoice(), choose_voice=fail_selection, watched_tokens=watched)
        self.assertEqual(str(error), "synthetic selection failure")
        self.assertIn("fail_selection", error.preview_traceback_text)
        self.assertEqual(done, [])

    def test_lifetime_oracle_detects_traceback_release_disabled(self):
        watched = []
        class Token:
            pass
        def fail_selection(voice, description):
            token = Token()
            watched.append(weakref.ref(token))
            raise RuntimeError("synthetic selection failure")
        with patch("tts_app.sapi_preview.release_preview_exception_frames", return_value=""), self.assertRaises(
            AssertionError, msg="negative control did not detect leaked traceback token"
        ):
            self.run_preview(FakeVoice(), choose_voice=fail_selection, watched_tokens=watched)

    def test_callback_failure_without_console_cannot_replace_primary_error(self):
        def bad_callback(info):
            raise RuntimeError("synthetic diagnostic failure")
        with patch.object(sys, "stderr", None), patch.object(sys, "stdout", None):
            result, done, error = self.run_preview(FakeVoice(), diagnostic=bad_callback)
        self.assertIsInstance(error, PreviewPlaybackError)
        self.assertEqual(done, [])

    def test_worker_queue_exception_has_text_trace_but_no_live_frames(self):
        variable = types.SimpleNamespace(get=lambda: "Fake", set=lambda value: None)
        number = types.SimpleNamespace(get=lambda: 0)
        button = types.SimpleNamespace(configure=lambda **kwargs: None)
        tab = types.SimpleNamespace(
            preview_thread=None, voice_var=variable, audio_output_var=variable,
            rate_var=number, pitch_var=number, volume_var=number,
            preview_stop_event=threading.Event(), preview_pause_event=threading.Event(),
            preview_base_mark="base", preview_snapshot_end_mark="end",
            text_box=types.SimpleNamespace(mark_set=lambda *args: None, mark_gravity=lambda *args: None),
            _text_offset_from_index=lambda index: 0, _full_text=lambda: "Unread",
            clear_saved_preview_bookmark=lambda **kwargs: None,
            clear_preview_highlight=lambda: None, log_text_state=lambda *args, **kwargs: None,
            preview_button=button, preview_cursor_button=button, pause_preview_button=button,
            resume_preview_button=button, stop_preview_button=button, status_var=variable,
            task_id="test-task", workspace_id="test-tab",
            app=types.SimpleNamespace(stop_other_previews=lambda task: None, events=queue.Queue(),
                                      logger=types.SimpleNamespace(event=lambda *args, **kwargs: None)),
        )
        def failed_preview(**kwargs):
            try:
                raise ValueError("original selection evidence")
            except ValueError as cause:
                raise RuntimeError("primary speech failure") from cause
        def inline_thread(*, target, **kwargs):
            return types.SimpleNamespace(start=target)
        with patch("tts_app.task_preview_player.preview_sapi_text", side_effect=failed_preview), patch(
            "tts_app.task_preview_player.threading.Thread", side_effect=inline_thread
        ):
            TaskPreviewPlayerMixin._start_preview_worker(
                tab, raw_text="Unread", preview_base_index="1.0", source_mode="all", selected_text=False,
            )
        task_id, kind, payload = tab.app.events.get_nowait()
        self.assertEqual(kind, "preview_error")
        self.assertIsNone(payload["exc"].__traceback__)
        self.assertIsNone(payload["exc"].__cause__)
        self.assertIsNone(payload["exc"].__context__)
        self.assertIn("original selection evidence", payload["traceback"])


class HotkeyCaptureTests(unittest.TestCase):
    def make_capture(self, *, running=False, paused=False):
        appended, events, statuses = [], [], []
        clipboard = {"text": ""}
        stop_event, pause_event = threading.Event(), threading.Event()
        if paused:
            pause_event.set()
        tab = types.SimpleNamespace(
            task_id="capture-task", workspace_id="capture-tab",
            tab_title=lambda: "Capture test",
            status_var=types.SimpleNamespace(set=statuses.append),
            preview_thread=types.SimpleNamespace(is_alive=lambda: running),
            preview_is_paused=paused, preview_stop_event=stop_event,
            preview_pause_event=pause_event, preview_bookmark={"start_offset": 12},
            auto_read_hotkey_var=types.SimpleNamespace(get=lambda: True),
            append_external_selected_text=appended.append,
        )
        monitor = types.SimpleNamespace(
            enabled=True, get_hotkey=lambda: "Ctrl+C",
            remember_text_and_check_duplicate=lambda text: False,
        )
        app = types.SimpleNamespace(
            shutdown_in_progress=False,
            root=types.SimpleNamespace(clipboard_get=lambda: clipboard["text"]),
            _task_by_workspace_id=lambda workspace_id: tab,
            tab_hotkey_monitors={"capture-tab": monitor},
            logger=types.SimpleNamespace(event=lambda name, **fields: events.append((name, fields))),
        )
        def capture(text):
            clipboard["text"] = text
            AppHotkeysMixin._on_tab_copy_hotkey(app, "capture-tab", {
                "clipboard_changed": True, "source_process": "synthetic.exe",
                "copy_latency_ms": 120,
            })
        return capture, tab, appended, events

    def test_punctuation_only_capture_does_not_start_idle_reading(self):
        for text in (".", ". . .", ", , .", "english.text", "…"):
            with self.subTest(text=text):
                capture, tab, appended, events = self.make_capture()
                capture(text)
                self.assertEqual(appended, [], "punctuation entered the reading queue")
                self.assertFalse(tab.preview_stop_event.is_set())

    def test_punctuation_during_reading_keeps_next_valid_capture(self):
        capture, tab, appended, events = self.make_capture(running=True)
        capture("Первый текст.")
        capture(". . .")
        capture("Следующий текст, 123.")
        self.assertEqual(appended, ["Первый текст.", "Следующий текст, 123."])
        self.assertTrue(tab.preview_thread.is_alive())
        self.assertFalse(tab.preview_stop_event.is_set())
        self.assertFalse(tab.preview_pause_event.is_set())

    def test_punctuation_during_pause_keeps_bookmark_and_next_valid_capture(self):
        capture, tab, appended, events = self.make_capture(running=True, paused=True)
        capture(".")
        capture("Продолжение.")
        self.assertEqual(appended, ["Продолжение."])
        self.assertEqual(tab.preview_bookmark, {"start_offset": 12})
        self.assertTrue(tab.preview_is_paused)
        self.assertTrue(tab.preview_pause_event.is_set())
        self.assertFalse(tab.preview_stop_event.is_set())

    def test_words_numbers_and_the_existing_filter_are_preserved(self):
        for text, expected in (("Я", "Я"), ("7", "7"), ("123,45.", "123,45."),
                               ("Русский — English текст.", "Русский текст.")):
            with self.subTest(text=text):
                capture, tab, appended, events = self.make_capture()
                capture(text)
                self.assertEqual(appended, [expected])

    def test_filtered_empty_capture_still_does_not_enter_queue(self):
        capture, tab, appended, events = self.make_capture(running=True)
        capture("English only")
        self.assertEqual(appended, [])
        self.assertFalse(tab.preview_stop_event.is_set())


if __name__ == "__main__":
    try:
        unittest.main()
    finally:
        MIRROR.cleanup()
