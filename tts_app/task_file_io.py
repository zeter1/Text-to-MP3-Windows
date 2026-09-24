from __future__ import annotations

from .runtime import *
from .problem_logger import ProblemLogger
from .text_processing import *
from .sapi import *
from .ffmpeg_tools import *
from .windows_tools import *

class TaskFileIoMixin:
    def load_file(self) -> None:
        kwargs = {}
        last_dir = self.app.settings.get("last_input_dir")
        if last_dir and Path(last_dir).is_dir():
            kwargs["initialdir"] = last_dir

        path = filedialog.askopenfilename(
            title="Выберите текстовый файл",
            filetypes=[
                ("Текстовые файлы", "*.txt *.md"),
                ("Все файлы", "*.*"),
            ],
            **kwargs,
        )
        if not path:
            return

        src = Path(path)

        try:
            text = read_text_file(src)
        except Exception as exc:
            self.app.log_ui_error(
                task_id=self.task_id,
                stage="load_text_file",
                exc=exc,
                context={"input_path": str(src)},
            )
            messagebox.showerror(
                APP_TITLE,
                f"Не удалось прочитать файл:\n{exc}",
            )
            return

        self.clear_saved_preview_bookmark(
            reason="load_new_text",
            schedule_save=False,
        )
        self.text_box.delete("1.0", "end")
        self.text_box.insert("1.0", text)
        self.text_box.edit_modified(False)
        self.workspace_dirty = True

        self.app.settings["last_input_dir"] = str(src.parent)
        # A filename is a meaningful user-facing title and must not be overwritten
        # by automatic renumbering.
        self.set_tab_title(src.stem, custom=True)
        self.app.renumber_default_tabs()
        self.status_var.set(
            f"Загружено: {src.name} — {len(text):,} символов.".replace(",", " ")
        )
        self.app.schedule_settings_save(self)
        self.app.schedule_workspace_save()

    def clear_text(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showwarning(
                APP_TITLE,
                "Нельзя очистить текст, пока эта вкладка создаёт MP3.",
            )
            return

        self.stop_preview(
            preserve_bookmark=False,
            reason="clear_text",
        )
        self.clear_saved_preview_bookmark(
            reason="clear_text",
            schedule_save=False,
        )
        self.text_box.delete("1.0", "end")
        self.workspace_dirty = True
        self.status_var.set("Текст очищен.")
        self.schedule_queue_status_refresh()
        self.log_text_state(
            "clear_text",
            force=True,
        )
        self.app.schedule_workspace_save()

    def _full_text(self) -> str:
        return self.text_box.get("1.0", "end-1c")
