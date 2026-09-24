from __future__ import annotations

from .runtime import *
from .problem_logger import ProblemLogger
from .text_processing import *
from .sapi import *
from .ffmpeg_tools import *
from .windows_tools import *

class TaskPreviewDeleteMixin:
    def _delete_pending_read_segment(self) -> int:
        if not self.preview_pending_delete_active:
            return 0

        try:
            start_index = self.text_box.index(
                self.preview_pending_delete_start_mark
            )
            end_index = self.text_box.index(
                self.preview_pending_delete_end_mark
            )

            if not self.text_box.compare(start_index, "<", end_index):
                self.preview_pending_delete_active = False
                return 0

            delete_end = end_index
            try:
                limit = self.text_box.index(
                    self.preview_snapshot_end_mark
                )
            except tk.TclError:
                limit = "end-1c"

            while self.text_box.compare(delete_end, "<", limit):
                ch = self.text_box.get(
                    delete_end,
                    f"{delete_end}+1c",
                )
                if not ch or not ch.isspace():
                    break
                delete_end = self.text_box.index(
                    f"{delete_end}+1c"
                )

            # Do not use Text.count(..., "chars") here. Tk 8.6 counts each
            # emoji as two UTF-16 units, but build_preview_segments() supplies
            # Python character offsets. Mixing both counters shifted every
            # later sentence boundary and left fragments of read text behind.
            deleted_chars = len(
                self.text_box.get(
                    start_index,
                    delete_end,
                )
            )

            if deleted_chars:
                self.text_box.delete(start_index, delete_end)
                self.preview_deleted_chars += deleted_chars
                self.workspace_dirty = True

                # During continuous reading do not rewrite workspace after every
                # sentence. The first delete schedules a save no later than the
                # 15-second checkpoint; later deletes cannot postpone it.
                self.app.schedule_workspace_save(
                    delay_ms=int(
                        WORKSPACE_READING_SAVE_INTERVAL_SEC
                        * 1000
                    ),
                    keep_earliest=True,
                )
                self._accumulate_delete_log(
                    deleted_chars=deleted_chars,
                    deleted_segments=1,
                )
                self.schedule_queue_status_refresh()

            self.preview_pending_delete_active = False
            return deleted_chars

        except tk.TclError:
            self.preview_pending_delete_active = False
            return 0

    def finalize_delete_read_text(self) -> None:
        if bool(self.delete_read_text_var.get()):
            self._delete_pending_read_segment()

    def _unread_appended_tail(self) -> tuple[str, str] | None:
        """
        Return (text, start_index) when new text was appended below the snapshot
        currently owned by SAPI and playback is supposed to follow the end.
        """
        if not self.current_preview_follow_end:
            return None

        full_text = self._full_text()
        try:
            start_index = self.text_box.index(
                self.preview_snapshot_end_mark
            )
        except tk.TclError:
            start_index = self._index_from_text_offset(
                max(
                    0,
                    min(
                        int(self.current_preview_snapshot_end),
                        len(full_text),
                    ),
                )
            )

        end_index = self.text_box.index("end-1c")
        if not self.text_box.compare(
            start_index,
            "<",
            end_index,
        ):
            return None

        tail = self.text_box.get(start_index, end_index)
        if not normalize_text(tail):
            return None

        return tail, start_index

    def continue_appended_tail_if_needed(self) -> bool:
        """
        Start a new SAPI preview immediately after the snapshot that just
        finished, so appended chapters are read without the user pressing Play
        again.
        """
        tail_info = self._unread_appended_tail()
        if tail_info is None:
            return False

        tail, start_index = tail_info
        previous_run_id = self.current_preview_run_id

        self._start_preview_worker(
            raw_text=tail,
            preview_base_index=start_index,
            source_mode="appended_tail",
            selected_text=False,
            preserve_existing_bookmark=True,
        )

        self.app.logger.event(
            "preview_appended_tail_continued",
            task_id=self.task_id,
            tab_id=self.workspace_id,
            previous_preview_run_id=previous_run_id,
            next_preview_run_id=self.current_preview_run_id,
            start_offset=self._text_offset_from_index(start_index),
            tail_chars=len(tail),
        )
        return True

    def clear_preview_highlight(self) -> None:
        try:
            self.text_box.tag_remove(
                "preview_current",
                "1.0",
                "end",
            )
        except tk.TclError:
            pass
