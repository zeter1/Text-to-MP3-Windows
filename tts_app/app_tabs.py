from __future__ import annotations

from .runtime import *
from .problem_logger import ProblemLogger
from .text_processing import *
from .sapi import *
from .ffmpeg_tools import *
from .windows_tools import *
from .widgets import *
from .task_tab import TaskTab

class AppTabsMixin:
    def new_tab(
        self,
        *,
        title: str | None = None,
        workspace_id: str | None = None,
        restored_text: str = "",
        restored_settings: dict | None = None,
        restored_preview_bookmark: dict | None = None,
        custom_title: bool = False,
        save_workspace: bool = True,
    ) -> TaskTab:
        # task_counter remains only an internal unique-ish sequence for diagnostics.
        # It is NOT used as the visible tab number anymore.
        self.task_counter += 1
        task = TaskTab(
            self,
            self.task_counter,
            workspace_id=workspace_id,
            restored_title=title or "Вкладка",
            restored_text=restored_text,
            restored_settings=restored_settings,
            restored_preview_bookmark=restored_preview_bookmark,
            custom_title=custom_title,
        )

        self.tabs[task.task_id] = task
        self.notebook.add(
            task.frame,
            text=task.restored_title,
        )
        self.notebook.select(task.frame)

        if self.voices:
            task.apply_voice_list(self.voices)
        task.audio_output_combo["values"] = self.audio_output_choices

        self.renumber_default_tabs()

        self.logger.event(
            "tab_created",
            task_id=task.task_id,
            tab_id=task.workspace_id,
            workspace_id=task.workspace_id,
            internal_seq=self.task_counter,
            visible_tab_index=self.visible_tab_index(task),
            visible_tab_title=task.tab_title(),
            restored=bool(workspace_id),
            copy_hotkey=task.copy_hotkey_var.get(),
        )

        if save_workspace:
            self.schedule_workspace_save()

        return task

    def renumber_default_tabs(self) -> None:
        """
        Renumber only automatically named tabs as Вкладка 1, Вкладка 2, ...
        Custom names are preserved exactly as the user set them.

        Example:
            Вкладка 1 | Моя книга | Вкладка 2 | Вкладка 3
        """
        next_number = 1

        for widget_name in self.notebook.tabs():
            task = None
            for candidate in self.tabs.values():
                if str(candidate.frame) == widget_name:
                    task = candidate
                    break

            if task is None or task.custom_title:
                continue

            wanted = f"Вкладка {next_number}"
            if task.tab_title() != wanted:
                task.set_tab_title(
                    wanted,
                    custom=False,
                    schedule_save=False,
                )
            next_number += 1

        self.schedule_workspace_save()

    def task_for_tab_index(self, index: int) -> TaskTab | None:
        try:
            tabs = self.notebook.tabs()
            if not (0 <= index < len(tabs)):
                return None
            widget_name = tabs[index]
        except (tk.TclError, IndexError):
            return None

        for task in self.tabs.values():
            if str(task.frame) == widget_name:
                return task

        return None

    def visible_tab_index(self, task: TaskTab) -> int | None:
        try:
            return int(self.notebook.index(task.frame)) + 1
        except tk.TclError:
            return None

    def current_tab(self) -> TaskTab | None:
        selected = self.notebook.select()
        if not selected:
            return None

        for task in self.tabs.values():
            if str(task.frame) == selected:
                return task

        return None

    def _on_notebook_close_requested(self, _event=None) -> None:
        index = self.notebook.close_requested_index
        self.notebook.close_requested_index = None

        if index is not None:
            self.close_tab_by_index(index)

    def _on_tab_right_click(self, event) -> str | None:
        try:
            index = self.notebook.index(
                f"@{event.x},{event.y}"
            )
        except tk.TclError:
            return None

        self.context_tab_index = index

        try:
            self.notebook.select(index)
        except tk.TclError:
            pass

        try:
            self.tab_menu.tk_popup(
                event.x_root,
                event.y_root,
            )
        finally:
            self.tab_menu.grab_release()

        return "break"

    def rename_context_tab(self) -> None:
        index = self.context_tab_index
        if index is None:
            return

        task = self.task_for_tab_index(index)
        if task is None:
            return

        new_name = simpledialog.askstring(
            "Переименовать вкладку",
            "Новое имя вкладки:",
            initialvalue=task.tab_title(),
            parent=self.root,
        )
        if new_name is None:
            return

        new_name = new_name.strip()
        if not new_name:
            messagebox.showwarning(
                APP_TITLE,
                "Имя вкладки не может быть пустым.",
            )
            return

        old_name = task.tab_title()
        task.set_tab_title(new_name, custom=True)
        self.renumber_default_tabs()
        self.logger.event(
            "tab_renamed",
            task_id=task.task_id,
            tab_id=task.workspace_id,
            workspace_id=task.workspace_id,
            visible_tab_index=self.visible_tab_index(task),
            old_title=old_name,
            new_title=new_name,
        )

    def close_context_tab(self) -> None:
        index = self.context_tab_index
        if index is not None:
            self.close_tab_by_index(index)

    def close_tab_by_index(self, index: int) -> None:
        task = self.task_for_tab_index(index)
        if task is None:
            return

        if task.conversion_running():
            messagebox.showwarning(
                APP_TITLE,
                "Эта вкладка сейчас создаёт MP3.\n"
                "Сначала остановите задачу и дождитесь её завершения.",
            )
            return

        task.stop_preview()
        self._stop_tab_hotkey_monitor(
            task.workspace_id
        )

        self.logger.event(
            "tab_closed",
            task_id=task.task_id,
            tab_id=task.workspace_id,
            workspace_id=task.workspace_id,
            visible_tab_index=self.visible_tab_index(task),
            title=task.tab_title(),
        )

        text_path = WORKSPACE_TEXT_DIR / f"{task.workspace_id}.txt"

        try:
            self.notebook.forget(task.frame)
            task.frame.destroy()
        finally:
            self.tabs.pop(task.task_id, None)

        try:
            text_path.unlink(missing_ok=True)
        except OSError as exc:
            self.logger.event(
                "workspace_tab_file_delete_failed",
                path=str(text_path),
                error=f"{type(exc).__name__}: {exc}",
            )

        if not self.tabs:
            self.new_tab(save_workspace=False)

        self.renumber_default_tabs()
        self.save_workspace()
