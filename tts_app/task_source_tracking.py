from __future__ import annotations

from .runtime import *
from .problem_logger import ProblemLogger
from .text_processing import *
from .sapi import *
from .ffmpeg_tools import *
from .windows_tools import *

class TaskSourceTrackingMixin:
    def track_mp3_source_snapshot(
        self,
        run_id: str,
        raw_text: str,
    ) -> bool:
        """Track the exact widget range owned by one MP3 conversion."""
        self._reset_mp3_source_tracking()

        try:
            start_index = self.text_box.index("1.0")
            end_index = self.text_box.index(
                f"{start_index}+{len(raw_text)}c"
            )
            if self.text_box.get(start_index, end_index) != raw_text:
                return False

            self.text_box.mark_set(
                self.mp3_source_start_mark,
                start_index,
            )
            self.text_box.mark_gravity(
                self.mp3_source_start_mark,
                "left",
            )
            self.text_box.mark_set(
                self.mp3_source_end_mark,
                end_index,
            )
            # Text inserted exactly at the old end must stay AFTER the source
            # range and therefore survive successful MP3 cleanup.
            self.text_box.mark_gravity(
                self.mp3_source_end_mark,
                "left",
            )
        except tk.TclError:
            self._reset_mp3_source_tracking()
            return False

        self.mp3_source_run_id = run_id
        self.mp3_source_chars = len(raw_text)
        self.mp3_source_sha256 = hashlib.sha256(
            raw_text.encode("utf-8")
        ).hexdigest()
        self.mp3_source_separator_pending = False

        self.app.logger.event(
            "mp3_source_snapshot_tracked",
            task_id=run_id,
            run_id=run_id,
            tab_id=self.workspace_id,
            source_chars=self.mp3_source_chars,
            source_sha256_16=self.mp3_source_sha256[:16],
        )
        return True

    def discard_mp3_source_snapshot(
        self,
        run_id: str | None,
        *,
        reason: str,
    ) -> bool:
        tracked_run_id = self.mp3_source_run_id
        if not tracked_run_id:
            return False
        if run_id and run_id != tracked_run_id:
            return False

        self.app.logger.event(
            "mp3_source_text_preserved",
            task_id=tracked_run_id,
            run_id=tracked_run_id,
            tab_id=self.workspace_id,
            reason=reason,
            source_chars=self.mp3_source_chars,
            current_text_chars=len(self._full_text()),
        )
        self._reset_mp3_source_tracking()
        return True

    def remove_completed_mp3_source(
        self,
        run_id: str | None,
    ) -> dict:
        """
        Remove only an unchanged source snapshot after verified MP3 success.

        Hotkey text appended after the left-gravity end mark is preserved. If
        anything inside the owned range changed, no text is deleted.
        """
        tracked_run_id = self.mp3_source_run_id
        if (
            not tracked_run_id
            or not run_id
            or run_id != tracked_run_id
        ):
            self.app.logger.event(
                "mp3_source_text_remove_skipped",
                task_id=run_id or self.task_id,
                run_id=run_id,
                tab_id=self.workspace_id,
                reason="snapshot_not_tracked",
                tracked_run_id=tracked_run_id,
            )
            return {
                "removed": False,
                "reason": "snapshot_not_tracked",
                "remaining_chars": len(self._full_text()),
            }

        try:
            start_index = self.text_box.index(
                self.mp3_source_start_mark
            )
            end_index = self.text_box.index(
                self.mp3_source_end_mark
            )
            current_source = self.text_box.get(
                start_index,
                end_index,
            )
        except tk.TclError as exc:
            self.app.logger.event(
                "mp3_source_text_remove_skipped",
                task_id=tracked_run_id,
                run_id=tracked_run_id,
                tab_id=self.workspace_id,
                reason="source_marks_unavailable",
                error=f"{type(exc).__name__}: {exc}",
            )
            self._reset_mp3_source_tracking()
            return {
                "removed": False,
                "reason": "source_marks_unavailable",
                "remaining_chars": len(self._full_text()),
            }

        current_sha256 = hashlib.sha256(
            current_source.encode("utf-8")
        ).hexdigest()
        snapshot_matches = bool(
            len(current_source) == self.mp3_source_chars
            and current_sha256 == self.mp3_source_sha256
        )
        if not snapshot_matches:
            self.app.logger.event(
                "mp3_source_text_remove_skipped",
                task_id=tracked_run_id,
                run_id=tracked_run_id,
                tab_id=self.workspace_id,
                reason="source_snapshot_changed",
                expected_chars=self.mp3_source_chars,
                current_source_chars=len(current_source),
                expected_sha256_16=self.mp3_source_sha256[:16],
                current_sha256_16=current_sha256[:16],
                current_text_chars=len(self._full_text()),
            )
            self._reset_mp3_source_tracking()
            return {
                "removed": False,
                "reason": "source_snapshot_changed",
                "remaining_chars": len(self._full_text()),
            }

        before_chars = len(self._full_text())
        source_chars = len(current_source)
        self.text_box.delete(start_index, end_index)

        separator_chars = 0
        if self.mp3_source_separator_pending:
            separator_end = self.text_box.index(
                f"{start_index}+1c"
            )
            if self.text_box.get(
                start_index,
                separator_end,
            ) == "\n":
                self.text_box.delete(
                    start_index,
                    separator_end,
                )
                separator_chars = 1

        self.clear_saved_preview_bookmark(
            reason="mp3_source_deleted",
            schedule_save=False,
        )
        self.workspace_dirty = True
        self._reset_mp3_source_tracking()

        remaining_chars = len(self._full_text())
        self.app.save_workspace()
        self.schedule_queue_status_refresh()
        self.log_text_state(
            "mp3_source_deleted",
            force=True,
        )
        self.app.logger.event(
            "mp3_source_text_removed",
            task_id=tracked_run_id,
            run_id=tracked_run_id,
            tab_id=self.workspace_id,
            source_chars=source_chars,
            separator_chars=separator_chars,
            removed_chars=source_chars + separator_chars,
            text_chars_before=before_chars,
            remaining_chars=remaining_chars,
        )
        return {
            "removed": True,
            "reason": "success",
            "source_chars": source_chars,
            "separator_chars": separator_chars,
            "remaining_chars": remaining_chars,
        }
