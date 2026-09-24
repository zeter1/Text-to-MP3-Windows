from __future__ import annotations

from .runtime import *
from .runtime_storage import _is_transient_atomic_replace_error
from .problem_logger import ProblemLogger
from .text_processing import *
from .sapi import *
from .ffmpeg_tools import *
from .windows_tools import *
from .widgets import *

class AppWorkspaceSaveMixin:
    def schedule_workspace_save(
        self,
        delay_ms: int = 1000,
        *,
        keep_earliest: bool = False,
    ) -> None:
        delay_ms = max(0, int(delay_ms))
        due_perf = time.perf_counter() + delay_ms / 1000.0

        if (
            self.workspace_after_id
            and keep_earliest
            and self.workspace_save_due_perf is not None
            and self.workspace_save_due_perf <= due_perf
        ):
            return

        if self.workspace_after_id:
            try:
                self.root.after_cancel(self.workspace_after_id)
            except Exception:
                pass

        self.workspace_save_due_perf = due_perf
        self.workspace_after_id = self.root.after(
            delay_ms,
            self.save_workspace,
        )

    def save_workspace(self) -> None:
        self.workspace_after_id = None
        self.workspace_save_due_perf = None
        save_started = time.perf_counter()
        written_text_bytes = 0
        dirty_tabs_written = 0
        atomic_replace_retries = 0

        try:
            WORKSPACE_TEXT_DIR.mkdir(
                parents=True,
                exist_ok=True,
            )

            ordered_tasks: list[TaskTab] = []
            for widget_name in self.notebook.tabs():
                for task in self.tabs.values():
                    if str(task.frame) == widget_name:
                        ordered_tasks.append(task)
                        break

            records: list[dict] = []

            for task in ordered_tasks:
                text_path = WORKSPACE_TEXT_DIR / f"{task.workspace_id}.txt"

                if task.workspace_dirty or not text_path.exists():
                    text = task.text_box.get("1.0", "end-1c")
                    atomic_replace_retries += atomic_write_text(
                        text_path,
                        text,
                    )
                    written_text_bytes += len(
                        text.encode("utf-8")
                    )
                    dirty_tabs_written += 1
                    task.workspace_dirty = False

                records.append(task.workspace_record())

            selected_index = 0
            try:
                selected_index = self.notebook.index(
                    self.notebook.select()
                )
            except tk.TclError:
                pass

            manifest = {
                "schema": 3,
                "saved_at": now_iso(),
                "selected_index": selected_index,
                "tabs": records,
            }
            atomic_replace_retries += atomic_write_json(
                WORKSPACE_FILE,
                manifest,
            )

            elapsed = time.perf_counter() - save_started
            self.workspace_last_saved_perf = time.perf_counter()
            failures_before_success = self.workspace_save_failure_count
            self.workspace_save_failure_count = 0
            if atomic_replace_retries or failures_before_success:
                self.logger.event(
                    "workspace_save_lock_recovered",
                    atomic_replace_retries=atomic_replace_retries,
                    failures_before_success=failures_before_success,
                    dirty_tabs_written=dirty_tabs_written,
                )
            if elapsed >= WORKSPACE_SLOW_SAVE_SEC:
                self.logger.event(
                    "workspace_save_slow",
                    duration_ms=round(elapsed * 1000.0, 1),
                    dirty_tabs_written=dirty_tabs_written,
                    text_bytes_written=written_text_bytes,
                    tabs_total=len(records),
                )

        except Exception as exc:
            retry_scheduled = False
            retry_delay_ms = None
            if (
                _is_transient_atomic_replace_error(exc)
                and not self.shutdown_in_progress
                and self.workspace_save_failure_count
                < len(WORKSPACE_SAVE_RETRY_DELAYS_MS)
            ):
                retry_delay_ms = WORKSPACE_SAVE_RETRY_DELAYS_MS[
                    self.workspace_save_failure_count
                ]
                self.workspace_save_failure_count += 1
                self.schedule_workspace_save(
                    retry_delay_ms,
                    keep_earliest=True,
                )
                retry_scheduled = True
            self.logger.error(
                task_id="app",
                stage="save_workspace",
                exc=exc,
                traceback_text=traceback.format_exc(),
                context={
                    "workspace_file": str(WORKSPACE_FILE),
                    "tabs_count": len(self.tabs),
                    "winerror": getattr(exc, "winerror", None),
                    "retry_scheduled": retry_scheduled,
                    "retry_delay_ms": retry_delay_ms,
                },
            )

    def _task_by_workspace_id(
        self,
        workspace_id: str,
    ) -> TaskTab | None:
        for task in self.tabs.values():
            if task.workspace_id == workspace_id:
                return task
        return None
