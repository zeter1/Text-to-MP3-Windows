from __future__ import annotations

from .runtime import *
from .problem_logger import ProblemLogger
from .text_processing import *
from .sapi import *
from .ffmpeg_tools import *
from .windows_tools import *
from .widgets import *

class AppShutdownMixin:
    def _alive_background_threads(self) -> list[str]:
        alive: list[str] = []
        for task in self.tabs.values():
            if task.worker and task.worker.is_alive():
                alive.append(task.worker.name)
            if (
                task.preview_thread
                and task.preview_thread.is_alive()
            ):
                alive.append(task.preview_thread.name)
        return alive

    def _finalize_close(self, status: str) -> None:
        for workspace_id in list(
            self.tab_hotkey_monitors
        ):
            self._stop_tab_hotkey_monitor(
                workspace_id
            )

        try:
            self.save_workspace()
        except Exception:
            pass
        try:
            self.save_settings()
        except Exception:
            pass
        self.logger.close(status=status)
        self.root.destroy()

    def _poll_shutdown(self) -> None:
        if not self.shutdown_in_progress:
            return

        alive = self._alive_background_threads()
        if not alive:
            self._finalize_close("closed")
            return

        if time.perf_counter() >= self.shutdown_deadline:
            self.logger.event(
                "forced_shutdown",
                alive_threads=alive,
                grace_period_sec=SHUTDOWN_GRACE_SEC,
            )
            self._finalize_close("forced_shutdown")
            return

        self.root.after(100, self._poll_shutdown)

    def on_close(self) -> None:
        if self.shutdown_in_progress:
            return

        running = [
            task
            for task in self.tabs.values()
            if task.conversion_running()
        ]

        if running:
            answer = messagebox.askyesno(
                "Идёт создание MP3",
                f"Сейчас создаётся MP3-задач: {len(running)}.\n\n"
                "Если закрыть программу, текущая конвертация будет "
                "корректно остановлена. Исходный текст и его бэкап "
                "останутся, но текущий MP3 не будет считаться "
                "завершённым.\n\n"
                "Закрыть программу и остановить создание MP3?\n\n"
                "Выберите «Нет», чтобы продолжить конвертацию.",
                icon=messagebox.WARNING,
                default=messagebox.NO,
            )
            if not answer:
                return

        preview_running = [
            task
            for task in self.tabs.values()
            if (
                task.preview_thread
                and task.preview_thread.is_alive()
            )
        ]

        self.shutdown_in_progress = True
        self.shutdown_deadline = (
            time.perf_counter() + SHUTDOWN_GRACE_SEC
        )

        self.logger.event(
            "shutdown_requested",
            conversion_run_ids=[
                task.current_run_id
                for task in running
                if task.current_run_id
            ],
            conversion_tab_ids=[
                task.workspace_id for task in running
            ],
            conversion_ui_task_ids=[
                task.task_id for task in running
            ],
            preview_run_ids=[
                task.current_preview_run_id
                for task in preview_running
                if task.current_preview_run_id
            ],
            preview_tab_ids=[
                task.workspace_id
                for task in preview_running
            ],
            preview_ui_task_ids=[
                task.task_id for task in preview_running
            ],
            grace_period_sec=SHUTDOWN_GRACE_SEC,
        )

        for task in running:
            task.cancel_event.set()
            task.status_var.set(
                "Завершаю задачу перед закрытием программы…"
            )

        for task in self.tabs.values():
            if (
                task.preview_thread
                and task.preview_thread.is_alive()
            ):
                task.current_preview_finish_reason = "app_exit"
                self.logger.event(
                    "preview_interrupted_by_app_exit",
                    task_id=task.task_id,
                    tab_id=task.workspace_id,
                    preview_run_id=task.current_preview_run_id,
                    audio_output=task.audio_output_var.get().strip()
                    or DEFAULT_AUDIO_OUTPUT_LABEL,
                )
            if (
                task.preview_thread
                and task.preview_thread.is_alive()
            ):
                if not (
                    task.preview_pause_event.is_set()
                    and task._bookmark_is_valid()
                ):
                    task.save_current_preview_bookmark(
                        reason="app_exit",
                        absolute_offset=task.current_preview_segment_start,
                        word_length=max(1, task.current_preview_word_length),
                        force_save=False,
                    )
                task.preview_keep_bookmark_after_stop = True
            task.flush_read_delete_log(
                reason="app_exit",
                force=True,
            )
            task.log_text_state(
                "app_exit",
                force=True,
            )
            task.preview_stop_event.set()
            task.preview_pause_event.clear()

        # Save user text immediately. Final task logs are allowed to finish during
        # the grace period before the logger is closed.
        self.save_workspace()
        self.save_settings()

        if not self._alive_background_threads():
            self._finalize_close("closed")
            return

        self.root.after(100, self._poll_shutdown)
