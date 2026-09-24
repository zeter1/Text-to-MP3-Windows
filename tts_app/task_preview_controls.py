from __future__ import annotations

from .runtime import *
from .problem_logger import ProblemLogger
from .text_processing import *
from .sapi import *
from .ffmpeg_tools import *
from .windows_tools import *

class TaskPreviewControlsMixin:
    def pause_preview(self) -> None:
        if not (self.preview_thread and self.preview_thread.is_alive()):
            return
        if self.preview_pause_event.is_set():
            return

        self.preview_pause_event.set()
        self.preview_is_paused = True
        self.pause_preview_button.configure(state="disabled")
        self.resume_preview_button.configure(state="normal")
        self.stop_preview_button.configure(state="normal")

        # Save a safe sentence-level checkpoint immediately. The SAPI worker
        # then refines it to the current word and saves again.
        self.save_current_preview_bookmark(
            reason="pause_requested_fallback",
            absolute_offset=self.current_preview_segment_start,
            word_length=max(1, self.current_preview_word_length),
            force_save=True,
            log_event=False,
            accuracy="fallback_sentence",
        )
        self.flush_read_delete_log(
            reason="pause",
            force=True,
        )
        self.log_text_state(
            "pause_requested",
            force=True,
        )

        current = self.current_preview_segment_text
        self.status_var.set(
            f"Пауза: при «Продолжить» чтение начинается на одно предложение назад. {current}" if current else "Пауза."
        )

        self.app.logger.event(
            "preview_pause_requested",
            task_id=self.task_id,
            tab_id=self.workspace_id,
            preview_run_id=self.current_preview_run_id,
            bookmark_char_offset=(
                self.preview_bookmark.get("char_offset")
                if isinstance(self.preview_bookmark, dict)
                else None
            ),
            bookmark_accuracy="fallback_sentence_until_sapi_checkpoint",
            audio_output=self.audio_output_var.get().strip()
            or DEFAULT_AUDIO_OUTPUT_LABEL,
        )

    def resume_preview(self) -> None:
        # Same-process resume is intentionally implemented as a controlled
        # restart, NOT voice.Resume(). Some SAPI voices can finish/advance the
        # queued sentence while Pause/Resume is being handled, which looks like
        # a jump to the next sentence. We purge that paused stream and restart
        # one sentence BEFORE the paused one.
        if self.preview_thread and self.preview_thread.is_alive():
            if not self.preview_pause_event.is_set():
                return

            # The Pause button saves a fallback bookmark immediately and the SAPI
            # worker normally refines it. Keep a safe bookmark even if the user
            # clicked Continue extremely quickly.
            if not self._bookmark_is_valid():
                self.save_current_preview_bookmark(
                    reason="resume_previous_sentence_fallback",
                    absolute_offset=self.current_preview_segment_start,
                    word_length=max(
                        1,
                        self.current_preview_word_length,
                    ),
                    force_save=True,
                )

            self.preview_resume_previous_pending = True
            self.preview_keep_bookmark_after_stop = True
            self.preview_resume_from_run_id = (
                self.current_preview_run_id
            )
            self.current_preview_finish_reason = (
                "resume_rewind"
            )

            bookmark = (
                dict(self.preview_bookmark)
                if isinstance(self.preview_bookmark, dict)
                else {}
            )
            paused_sentence_offset = int(
                bookmark.get(
                    "sentence_start_offset",
                    self.current_preview_segment_start,
                )
                or 0
            )
            previous_offset = bookmark.get(
                "previous_sentence_start_offset"
            )
            if previous_offset is None:
                previous_offset = previous_preview_segment_start(
                    self._full_text(),
                    paused_sentence_offset,
                )

            self.app.logger.event(
                "preview_resume_requested",
                task_id=self.task_id,
                tab_id=self.workspace_id,
                preview_run_id=self.current_preview_run_id,
                resume_mode="restart_previous_sentence",
                paused_sentence_offset=paused_sentence_offset,
                resume_previous_sentence_offset=int(
                    previous_offset
                ),
                audio_output=self.audio_output_var.get().strip()
                or DEFAULT_AUDIO_OUTPUT_LABEL,
            )

            # Wake the paused worker, let it Resume only for purge/cleanup, and
            # stop this old SAPI stream. The event loop starts the replacement
            # preview as soon as COM has been released.
            self.preview_stop_event.set()
            self.preview_pause_event.clear()

            self.preview_is_paused = False
            self.pause_preview_button.configure(state="disabled")
            self.resume_preview_button.configure(state="disabled")
            self.stop_preview_button.configure(state="disabled")
            self.status_var.set(
                "Возвращаюсь на одно предложение назад…"
            )
            self.flush_read_delete_log(
                reason="resume_rewind",
                force=True,
            )
            self.log_text_state(
                "resume_rewind_requested",
                force=True,
            )
            return

        # After a program restart there is no live SAPI object, so immediately
        # start a new preview one sentence before the persisted pause sentence.
        if self.resume_saved_preview():
            return

    def apply_preview_state(self, state: str) -> None:
        if state == "paused":
            self.preview_is_paused = True
            self.pause_preview_button.configure(state="disabled")
            self.resume_preview_button.configure(state="normal")
            self.stop_preview_button.configure(state="normal")
            current = self.current_preview_segment_text
            self.status_var.set(
                f"Пауза: {current}" if current else "Пауза."
            )

        elif state == "running":
            self.preview_is_paused = False
            self.pause_preview_button.configure(state="normal")
            self.resume_preview_button.configure(state="disabled")

    def stop_preview(
        self,
        *,
        preserve_bookmark: bool = False,
        reason: str = "user_stop",
    ) -> None:
        if reason == "user_stop":
            self.preview_resume_previous_pending = False

        finish_reason = {
            "user_stop": "user_stop",
            "another_tab_started_preview": "another_tab",
            "app_exit": "app_exit",
            "mp3_creation_started": "mp3_creation",
            "mp3_recovery_started": "mp3_creation",
            "clear_text": "clear_text",
            "clipboard_paste": "text_changed",
        }.get(reason, reason or "other")
        if (
            self.preview_thread
            and self.preview_thread.is_alive()
        ):
            self.current_preview_finish_reason = finish_reason

        running = bool(
            self.preview_thread and self.preview_thread.is_alive()
        )

        if preserve_bookmark and running:
            # If SAPI already saved the exact paused word, do not overwrite it
            # with the coarser sentence-start fallback.
            if not (
                self.preview_pause_event.is_set()
                and self._bookmark_is_valid()
            ):
                self.save_current_preview_bookmark(
                    reason=reason,
                    absolute_offset=self.current_preview_segment_start,
                    word_length=max(1, self.current_preview_word_length),
                    force_save=True,
                )
            self.preview_keep_bookmark_after_stop = True
        elif not preserve_bookmark:
            self.clear_saved_preview_bookmark(
                reason=reason,
                schedule_save=True,
            )
            self.preview_keep_bookmark_after_stop = False

        if running:
            self.app.logger.event(
                "preview_stop_requested",
                task_id=self.task_id,
                tab_id=self.workspace_id,
                preview_run_id=self.current_preview_run_id,
                paused=bool(self.preview_pause_event.is_set()),
                preserve_bookmark=preserve_bookmark,
                reason=reason,
                audio_output=self.audio_output_var.get().strip()
                or DEFAULT_AUDIO_OUTPUT_LABEL,
            )
            self.preview_stop_event.set()
            self.preview_pause_event.clear()
            self.status_var.set("Останавливаю воспроизведение…")
        elif not preserve_bookmark:
            self.clear_preview_highlight()
            self.preview_is_paused = False
            self.pause_preview_button.configure(state="disabled")
            self.resume_preview_button.configure(state="disabled")
            self.stop_preview_button.configure(state="disabled")
            if not self.conversion_running():
                self.status_var.set("Готово к работе.")

        self.flush_read_delete_log(
            reason=finish_reason,
            force=True,
        )
        self.log_text_state(
            f"preview_stop_{finish_reason}",
            force=True,
        )

    def preview_finished_ui(self, *, stopped: bool = False) -> None:
        keep_bookmark = bool(
            stopped
            and self.preview_keep_bookmark_after_stop
            and self._bookmark_is_valid()
        )

        self.preview_pause_event.clear()
        self.current_preview_run_id = None
        self.preview_button.configure(state="normal")
        self.preview_cursor_button.configure(state="normal")
        self.pause_preview_button.configure(state="disabled")

        if keep_bookmark:
            self.preview_is_paused = True
            self.resume_preview_button.configure(state="normal")
            self.stop_preview_button.configure(state="normal")
            self._restore_preview_bookmark_ui()
        else:
            if not stopped:
                self.clear_saved_preview_bookmark(
                    reason="preview_completed",
                    schedule_save=True,
                )
            self.clear_preview_highlight()
            self.preview_is_paused = False
            self.current_preview_segment_text = ""
            self.resume_preview_button.configure(state="disabled")
            self.stop_preview_button.configure(state="disabled")
            if not self.conversion_running():
                self.status_var.set("Готово к работе.")

        self.preview_keep_bookmark_after_stop = False
        self.flush_read_delete_log(
            reason="preview_finished_ui",
            force=True,
        )
        self.schedule_queue_status_refresh()
