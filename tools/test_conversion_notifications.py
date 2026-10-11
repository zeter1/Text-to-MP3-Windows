from __future__ import annotations

import queue
import shutil
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

# Import only synthetic source; storage is resolved from __file__, not cwd.
SOURCE_ROOT = Path(__file__).resolve().parents[1]
MIRROR = tempfile.TemporaryDirectory(prefix="tts уведомления тест ", dir=SOURCE_ROOT)
ROOT = Path(MIRROR.name).resolve()
(ROOT / "tts_app").mkdir()
for source in (SOURCE_ROOT / "tts_app").glob("*.py"):
    shutil.copyfile(source, ROOT / "tts_app" / source.name)
sys.path.insert(0, str(ROOT))
from tts_app import runtime
from tts_app.app_event_dispatch import AppEventDispatchMixin
from tts_app.conversion_notifications import ConversionNotifications, reveal_audio_file

assert runtime.STORAGE_BASE_DIR.resolve() == ROOT


class CompletionDispatchTests(unittest.TestCase):
    def test_completed_file_uses_persistent_notification_without_modal_dialog(self):
        output = ROOT / "Аудио с пробелом" / "Готовый файл.mp3"
        task = types.SimpleNamespace(
            current_mp3_text_backup_path=None,
            remove_completed_mp3_source=Mock(return_value={"removed": True}),
            set_job_idle=Mock(), status_var=Mock(),
        )
        app = types.SimpleNamespace(
            events=queue.Queue(), tabs={1: task},
            root=types.SimpleNamespace(after=Mock()),
            completion_notifications=Mock(), shutdown_in_progress=False,
            _process_events=Mock(),
        )
        app.events.put((1, "done", {"output": str(output), "run_id": "run-1"}))
        with patch("tts_app.app_event_dispatch.messagebox.showinfo") as modal:
            AppEventDispatchMixin._process_events(app)
        modal.assert_not_called()
        app.completion_notifications.show.assert_called_once()
        self.assertEqual(app.completion_notifications.show.call_args.args[0], output)
        task.remove_completed_mp3_source.assert_called_once_with("run-1")
        task.set_job_idle.assert_called_once()

    def test_shutting_down_does_not_create_a_new_window(self):
        task = types.SimpleNamespace(
            current_mp3_text_backup_path=None,
            remove_completed_mp3_source=Mock(return_value={}),
            set_job_idle=Mock(), status_var=Mock(),
        )
        app = types.SimpleNamespace(
            events=queue.Queue(), tabs={1: task},
            root=types.SimpleNamespace(after=Mock()),
            completion_notifications=Mock(), shutdown_in_progress=True,
            _process_events=Mock(),
        )
        app.events.put((1, "done", {"output": str(ROOT / "done.mp3"), "run_id": "run-1"}))
        AppEventDispatchMixin._process_events(app)
        app.completion_notifications.show.assert_not_called()
        task.set_job_idle.assert_called_once()

    def test_reveal_selects_exact_unicode_file_without_shell(self):
        output = ROOT / "Аудио с пробелом" / "Готовый файл, 1.mp3"
        output.parent.mkdir(exist_ok=True)
        output.write_bytes(b"synthetic output")
        with patch("tts_app.conversion_notifications.os.name", "nt"), \
             patch("tts_app.conversion_notifications.subprocess.Popen") as process:
            reveal_audio_file(output)
        process.assert_called_once_with(["explorer.exe", "/select,", str(output)])
        self.assertEqual(output.read_bytes(), b"synthetic output")

    def test_removed_file_opens_existing_folder_without_recreating_anything(self):
        output = ROOT / "Существующая папка" / "Удалённый файл.mp3"
        output.parent.mkdir(exist_ok=True)
        with patch("tts_app.conversion_notifications.os.name", "nt"), \
             patch("tts_app.conversion_notifications.os.startfile", create=True) as start:
            reveal_audio_file(output)
        start.assert_called_once_with(str(output.parent))
        missing = ROOT / "Исчезнувший диск" / "file.mp3"
        with self.assertRaises(FileNotFoundError):
            reveal_audio_file(missing)
        self.assertFalse(missing.parent.exists())


@unittest.skipUnless("--gui" in sys.argv, "real Tk smoke: run with --gui")
class NotificationTkTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        self.root = tk.Tk()
        self.root.withdraw()
        self.manager = ConversionNotifications(self.root)
        self.errors = []
        self.root.report_callback_exception = lambda *error: self.errors.append(error)

    def tearDown(self):
        self.root.destroy()
        self.assertEqual(self.errors, [])

    def buttons(self, parent):
        from tkinter import ttk
        result = []
        for child in parent.winfo_children():
            if isinstance(child, ttk.Button):
                result.append(child)
            result.extend(self.buttons(child))
        return result

    def test_burst_open_click_and_cross_keep_other_notifications(self):
        from tts_app.conversion_notifications import notification_work_area
        files = [ROOT / f"Готовая задача {index}.mp3" for index in range(4)]
        for output in files:
            self.manager.show(output, "Тестовое уведомление.")
        self.root.update()
        self.assertEqual(len(self.manager.windows), 4)
        self.assertFalse(self.root.tk.call("after", "info"))
        left, top, right, bottom = notification_work_area(self.root)
        rectangles = []
        for window in self.manager.windows:
            self.assertTrue(window.winfo_viewable())
            self.assertTrue(window.attributes("-topmost"))
            self.assertTrue(window.overrideredirect())
            x, y, w, h = window.winfo_rootx(), window.winfo_rooty(), window.winfo_width(), window.winfo_height()
            self.assertGreaterEqual(x, left)
            self.assertGreaterEqual(y, top)
            self.assertLessEqual(x + w, right)
            self.assertLessEqual(y + h, bottom)
            rectangles.append((x, y, x + w, y + h))
        for index, a in enumerate(rectangles):
            for b in rectangles[index + 1:]:
                self.assertTrue(a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])

        first = self.manager.windows[0]
        with patch("tts_app.conversion_notifications.reveal_audio_file") as reveal:
            next(b for b in self.buttons(first) if b["text"] == "Показать файл").invoke()
        reveal.assert_called_once_with(files[0])
        self.assertEqual(len(self.manager.windows), 4)
        with patch("tts_app.conversion_notifications.reveal_audio_file", side_effect=FileNotFoundError("missing")), \
             patch("tts_app.conversion_notifications.messagebox.showerror") as error:
            next(b for b in self.buttons(first) if b["text"] == "Показать файл").invoke()
        error.assert_called_once()
        self.assertTrue(first.winfo_exists())
        self.root.tk.call(first.protocol("WM_DELETE_WINDOW"))
        self.assertTrue(first.winfo_exists())
        next(b for b in self.buttons(first) if b["text"] == "×").invoke()
        self.root.update()
        self.assertFalse(first.winfo_exists())
        self.assertEqual(len(self.manager.windows), 3)
        for window in list(self.manager.windows):
            self.manager.dismiss(window)
        self.assertEqual(self.manager.windows, [])
        self.manager.show(files[0])
        self.root.update()
        self.assertTrue(self.manager.windows[0].winfo_viewable())

    def test_long_display_path_keeps_file_button_inside_work_area(self):
        from tts_app.conversion_notifications import notification_work_area
        output = ROOT.joinpath(*(["Длинная папка с пробелом" * 5] * 12), "Имя файла " * 20 + ".mp3")
        self.manager.show(output, "Конвертированный текст удалён. Бэкап текста хранится 45 дней.")
        self.root.update()
        window = self.manager.windows[0]
        button = next(b for b in self.buttons(window) if b["text"] == "Показать файл")
        left, top, right, bottom = notification_work_area(self.root)
        self.assertGreaterEqual(window.winfo_rooty(), top)
        self.assertLessEqual(window.winfo_rooty() + window.winfo_height(), bottom)
        self.assertGreater(button.winfo_width(), 40)
        self.assertLessEqual(button.winfo_rooty() + button.winfo_height(), bottom)
        self.assertTrue(button.winfo_viewable())
    def test_minimize_and_shutdown_with_notifications(self):
        self.root.deiconify()
        self.root.update()
        self.manager.show(ROOT / "После сворачивания.mp3")
        self.root.iconify()
        self.root.update()
        self.assertTrue(self.manager.windows[0].winfo_viewable())
        self.manager.show(ROOT / "Завершилась, пока главное окно свёрнуто.mp3")
        self.root.update()
        self.assertTrue(self.manager.windows[1].winfo_viewable())
        self.root.deiconify()
        self.root.update()
        self.manager.show(ROOT / "Вторая задача.mp3")
        self.assertEqual(len(self.manager.windows), 3)
        # tearDown destroys the root with both independent windows still open.

    def test_worker_cannot_mutate_tk(self):
        errors = []
        def worker():
            try:
                self.manager.show(ROOT / "worker.mp3")
            except RuntimeError as exc:
                errors.append(str(exc))
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertEqual(self.manager.windows, [])

    def test_two_completion_events_create_two_independent_windows_on_tk_thread(self):
        def task():
            return types.SimpleNamespace(
                current_mp3_text_backup_path=None,
                remove_completed_mp3_source=Mock(return_value={"removed": True}),
                set_job_idle=Mock(), status_var=Mock(),
            )
        app = types.SimpleNamespace(
            root=self.root, events=queue.Queue(), tabs={1: task(), 2: task()},
            shutdown_in_progress=False, _process_events=Mock(),
        )
        for index in (1, 2):
            app.events.put((index, "done", {"output": str(ROOT / f"Задача {index}.mp3"), "run_id": f"run-{index}"}))
        with patch("tts_app.app_event_dispatch.messagebox.showinfo") as modal:
            AppEventDispatchMixin._process_events(app)
        self.root.update()
        modal.assert_not_called()
        self.assertEqual(len(app.completion_notifications.windows), 2)
        self.assertEqual(app.completion_notifications.thread_id, threading.get_ident())
        self.assertIsNot(app.completion_notifications.windows[0], app.completion_notifications.windows[1])


if __name__ == "__main__":
    try:
        if "--gui" in sys.argv:
            sys.argv.remove("--gui")
        unittest.main()
    finally:
        MIRROR.cleanup()
