from __future__ import annotations

from .runtime import *
from .problem_logger import ProblemLogger
from .text_processing import *
from .sapi import *
from .ffmpeg_tools import *
from .windows_tools import *

class TaskUiStatusMixin:
    def _on_text_modified(self, _event=None) -> None:
        try:
            modified = bool(self.text_box.edit_modified())
        except tk.TclError:
            return

        if not modified:
            return

        self.text_box.edit_modified(False)
        self.workspace_dirty = True

        if (
            self.preview_thread
            and self.preview_thread.is_alive()
            and bool(self.delete_read_text_var.get())
        ):
            self.app.schedule_workspace_save(
                delay_ms=int(
                    WORKSPACE_READING_SAVE_INTERVAL_SEC
                    * 1000
                ),
                keep_earliest=True,
            )
        else:
            self.app.schedule_workspace_save()

        self.schedule_queue_status_refresh()

    @staticmethod
    def _format_char_count(value: int) -> str:
        return f"{max(0, int(value)):,}".replace(",", " ")

    def schedule_queue_status_refresh(self) -> None:
        # Throttle instead of debounce: the first request schedules an update
        # and rapid sentence events cannot postpone it forever.
        if self.queue_status_after_id:
            return

        try:
            self.queue_status_after_id = self.app.root.after(
                QUEUE_STATUS_REFRESH_MS,
                self.refresh_queue_status,
            )
        except tk.TclError:
            self.queue_status_after_id = None

    def refresh_queue_status(self) -> None:
        self.queue_status_after_id = None
        try:
            text = self._full_text()
            chars = len(text)
            sentence_count = len(
                re.findall(
                    r"[.!?…]+(?=\s|$)",
                    text,
                )
            )
            if chars and sentence_count == 0:
                sentence_count = max(
                    1,
                    sum(
                        1
                        for line in text.splitlines()
                        if line.strip()
                    ),
                )

            label = (
                f"В очереди: ~{sentence_count} предл. / "
                f"{self._format_char_count(chars)} симв."
            )
            if self.session_captured_chars:
                label += (
                    " • добавлено: "
                    + self._format_char_count(
                        self.session_captured_chars
                    )
                )
            if self.session_read_chars:
                label += (
                    " • прочитано: "
                    + self._format_char_count(
                        self.session_read_chars
                    )
                )
            self.queue_status_var.set(label)
        except (tk.TclError, RuntimeError):
            pass

    def log_text_state(
        self,
        reason: str,
        *,
        force: bool = False,
    ) -> None:
        now_perf = time.perf_counter()
        if (
            not force
            and now_perf - self._text_state_last_log_perf
            < TEXT_STATE_LOG_INTERVAL_SEC
        ):
            return

        try:
            text = self._full_text()
            chars = len(text)
            if (
                self.preview_thread
                and self.preview_thread.is_alive()
            ):
                unread_start = max(
                    0,
                    min(
                        self.current_preview_segment_start,
                        chars,
                    ),
                )
            elif self._bookmark_is_valid():
                unread_start = max(
                    0,
                    min(
                        int(
                            (self.preview_bookmark or {}).get(
                                "sentence_start_offset",
                                0,
                            )
                            or 0
                        ),
                        chars,
                    ),
                )
            else:
                unread_start = 0

            self.app.logger.event(
                "text_state",
                task_id=self.task_id,
                tab_id=self.workspace_id,
                preview_run_id=self.current_preview_run_id,
                reason=reason,
                text_chars=chars,
                text_sha256_16=hashlib.sha256(
                    text.encode("utf-8")
                ).hexdigest()[:16],
                unread_chars=max(0, chars - unread_start),
                sentence_endings_approx=len(
                    re.findall(
                        r"[.!?…]+(?=\s|$)",
                        text,
                    )
                ),
                delete_read_text=bool(
                    self.delete_read_text_var.get()
                ),
                preview_running=bool(
                    self.preview_thread
                    and self.preview_thread.is_alive()
                ),
                paused=bool(self.preview_is_paused),
                session_captured_chars=self.session_captured_chars,
                session_read_segments=self.session_read_segments,
                session_read_chars=self.session_read_chars,
                session_deleted_segments=self.session_deleted_segments,
                session_deleted_chars=self.session_deleted_chars,
            )
            self._text_state_last_log_perf = now_perf
        except (tk.TclError, RuntimeError):
            pass

    def _accumulate_delete_log(
        self,
        *,
        deleted_chars: int,
        deleted_segments: int = 1,
    ) -> None:
        self._delete_log_chars += max(
            0,
            int(deleted_chars),
        )
        self._delete_log_segments += max(
            0,
            int(deleted_segments),
        )
        self.session_deleted_chars += max(
            0,
            int(deleted_chars),
        )
        self.session_deleted_segments += max(
            0,
            int(deleted_segments),
        )

        if (
            time.perf_counter()
            - self._delete_log_last_flush_perf
            >= READ_DELETE_LOG_FLUSH_INTERVAL_SEC
        ):
            self.flush_read_delete_log(
                reason="interval",
                force=True,
            )

    def flush_read_delete_log(
        self,
        *,
        reason: str,
        force: bool = False,
    ) -> None:
        if (
            self._delete_log_segments <= 0
            and self._delete_log_chars <= 0
        ):
            return

        now_perf = time.perf_counter()
        if (
            not force
            and now_perf - self._delete_log_last_flush_perf
            < READ_DELETE_LOG_FLUSH_INTERVAL_SEC
        ):
            return

        segments = self._delete_log_segments
        chars = self._delete_log_chars
        self._delete_log_segments = 0
        self._delete_log_chars = 0
        self._delete_log_last_flush_perf = now_perf

        self.app.logger.event(
            "preview_read_text_deleted_batch",
            task_id=self.task_id,
            tab_id=self.workspace_id,
            preview_run_id=self.current_preview_run_id,
            reason=reason,
            deleted_segments=segments,
            deleted_chars=chars,
            session_deleted_segments=self.session_deleted_segments,
            session_deleted_chars=self.session_deleted_chars,
            text_chars=len(self._full_text()),
        )
        self.log_text_state(
            f"delete_batch_{reason}",
            force=True,
        )
