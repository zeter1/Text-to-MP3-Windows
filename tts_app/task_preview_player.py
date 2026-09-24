from __future__ import annotations

from .runtime import *
from .problem_logger import ProblemLogger
from .text_processing import *
from .sapi import *
from .ffmpeg_tools import *
from .windows_tools import *

class TaskPreviewPlayerMixin:
    def _start_preview_worker(
        self,
        *,
        raw_text: str,
        preview_base_index: str,
        source_mode: str,
        selected_text: bool,
        preserve_existing_bookmark: bool = False,
    ) -> None:
        if self.preview_thread and self.preview_thread.is_alive():
            return

        preview_run_id = make_run_id("preview")
        self.current_preview_run_id = preview_run_id
        self.current_preview_finish_reason = None

        text = raw_text
        voice = self.voice_var.get().strip()
        if not voice:
            messagebox.showwarning(APP_TITLE, "Выберите голос.")
            return

        rate = clamp_int(self.rate_var.get(), RATE_MIN, RATE_MAX, 0)
        pitch = clamp_int(self.pitch_var.get(), PITCH_MIN, PITCH_MAX, 0)
        audio_output = (
            self.audio_output_var.get().strip()
            or DEFAULT_AUDIO_OUTPUT_LABEL
        )
        volume = clamp_int(self.volume_var.get(), 0, 100, 100)

        self.app.stop_other_previews(self)
        self.preview_stop_event.clear()
        self.preview_pause_event.clear()
        self.preview_is_paused = False
        self.preview_keep_bookmark_after_stop = False
        self.current_preview_segment_text = ""
        self.current_preview_source_mode = source_mode

        try:
            self.text_box.mark_set(
                self.preview_base_mark,
                preview_base_index,
            )
            self.text_box.mark_gravity(
                self.preview_base_mark,
                "left",
            )
            self.text_box.mark_set(
                self.preview_snapshot_end_mark,
                f"{preview_base_index}+{len(raw_text)}c",
            )
            self.text_box.mark_gravity(
                self.preview_snapshot_end_mark,
                "left",
            )
            ui_base_index = self.preview_base_mark
        except tk.TclError:
            ui_base_index = preview_base_index

        self.current_preview_base_index = ui_base_index
        self.preview_deleted_chars = 0
        self.preview_pending_delete_active = False

        self.current_preview_base_text_offset = self._text_offset_from_index(
            ui_base_index
        )
        self.current_preview_segment_start = (
            self.current_preview_base_text_offset
        )
        self.current_preview_segment_end = self.current_preview_segment_start

        full_text_chars = len(self._full_text())
        self.current_preview_snapshot_end = min(
            full_text_chars,
            self.current_preview_segment_start + len(raw_text),
        )
        self.current_preview_range_end = self.current_preview_snapshot_end

        # Full-text, cursor and saved-bookmark playback are intended to reach
        # the bottom. If more text is appended while they run, continue into it.
        self.current_preview_follow_end = bool(
            source_mode
            in {
                "all",
                "cursor",
                "saved_bookmark",
                "appended_tail",
            }
            and self.current_preview_snapshot_end >= full_text_chars
        )

        self.current_preview_segment_index = 0
        self.current_preview_segments_total = 0
        self.current_preview_word_length = 1
        if not preserve_existing_bookmark:
            self.clear_saved_preview_bookmark(
                reason="new_preview_started",
                schedule_save=False,
            )
        self.clear_preview_highlight()

        self.preview_button.configure(state="disabled")
        self.preview_cursor_button.configure(state="disabled")
        self.pause_preview_button.configure(state="normal")
        self.resume_preview_button.configure(state="disabled")
        self.stop_preview_button.configure(state="normal")

        if source_mode == "cursor":
            self.status_var.set("Начинаю чтение с позиции курсора…")
        elif selected_text:
            self.status_var.set("Воспроизвожу выделенный текст…")
        else:
            self.status_var.set("Воспроизвожу текст…")

        self.log_text_state(
            "preview_started",
            force=True,
        )

        self.app.logger.event(
            "preview_started",
            task_id=self.task_id,
            tab_id=self.workspace_id,
            preview_run_id=preview_run_id,
            chars=len(text),
            source_mode=source_mode,
            selected_text=selected_text,
            base_index=preview_base_index,
            voice=voice,
            audio_output=audio_output,
            rate=rate,
            pitch=pitch,
            volume=volume,
            preview_sapi_mode="continuous_blocks",
            preview_sapi_block_max_chars=PREVIEW_SAPI_BLOCK_MAX_CHARS,
        )

        def worker() -> None:
            try:
                def report_progress(info: dict) -> None:
                    self.app.events.put(
                        (
                            self.task_id,
                            "preview_segment",
                            {
                                **info,
                                "base_index": ui_base_index,
                            },
                        )
                    )

                def report_heartbeat(info: dict) -> None:
                    self.app.logger.event(
                        "preview_progress",
                        task_id=self.task_id,
                        tab_id=self.workspace_id,
                        preview_run_id=preview_run_id,
                        segment=info.get("segment"),
                        segments_total=info.get(
                            "segments_total"
                        ),
                        elapsed_sec=info.get("elapsed_sec"),
                        paused=info.get("paused"),
                        source_mode=source_mode,
                    )

                def report_state(state: str) -> None:
                    self.app.events.put(
                        (
                            self.task_id,
                            "preview_state",
                            {"state": state},
                        )
                    )

                def report_checkpoint(info: dict) -> None:
                    self.app.events.put(
                        (
                            self.task_id,
                            "preview_checkpoint",
                            {
                                **info,
                                "base_index": ui_base_index,
                                "preview_run_id": preview_run_id,
                            },
                        )
                    )

                def report_segment_done(info: dict) -> None:
                    self.app.events.put(
                        (
                            self.task_id,
                            "preview_segment_done",
                            {
                                **info,
                                "base_index": ui_base_index,
                                "preview_run_id": preview_run_id,
                            },
                        )
                    )

                preview_result = preview_sapi_text(
                    text=text,
                    voice_description=voice,
                    audio_output_description=audio_output,
                    rate=rate,
                    pitch=pitch,
                    volume=volume,
                    stop_event=self.preview_stop_event,
                    pause_event=self.preview_pause_event,
                    progress_callback=report_progress,
                    state_callback=report_state,
                    heartbeat_callback=report_heartbeat,
                    checkpoint_callback=report_checkpoint,
                    segment_done_callback=report_segment_done,
                )
                preview_result["preview_run_id"] = preview_run_id
                self.app.events.put(
                    (self.task_id, "preview_done", preview_result)
                )
            except Exception as exc:
                self.app.events.put(
                    (
                        self.task_id,
                        "preview_error",
                        {
                            "exc": exc,
                            "traceback": traceback.format_exc(),
                            "context": {
                                "chars": len(text),
                                "tab_id": self.workspace_id,
                                "preview_run_id": preview_run_id,
                                "source_mode": source_mode,
                                "selected_text": selected_text,
                                "base_index": preview_base_index,
                                "voice": voice,
                                "audio_output": audio_output,
                                "rate": rate,
                                "pitch": pitch,
                                "volume": volume,
                            },
                        },
                    )
                )

        self.preview_thread = threading.Thread(
            target=worker,
            name=f"preview-{self.task_id}",
            daemon=True,
        )
        self.preview_thread.start()

    def show_preview_segment(
        self,
        *,
        base_index: str,
        start: int,
        end: int,
        index: int,
        total: int,
        text: str,
    ) -> None:
        try:
            self.text_box.tag_remove("preview_current", "1.0", "end")

            adjusted_start = max(
                0,
                int(start) - int(self.preview_deleted_chars),
            )
            adjusted_end = max(
                adjusted_start,
                int(end) - int(self.preview_deleted_chars),
            )

            start_index = self.text_box.index(
                f"{base_index}+{adjusted_start}c"
            )
            end_index = self.text_box.index(
                f"{base_index}+{adjusted_end}c"
            )

            self.text_box.tag_add(
                "preview_current",
                start_index,
                end_index,
            )
            self.text_box.tag_raise("preview_current")
            self.text_box.see(start_index)

            short_text = " ".join(str(text).split())
            if len(short_text) > 110:
                short_text = short_text[:107] + "..."

            self.current_preview_segment_text = short_text
            self.current_preview_segment_start = (
                self.current_preview_base_text_offset
                + adjusted_start
            )
            self.current_preview_segment_end = (
                self.current_preview_base_text_offset
                + adjusted_end
            )
            self.current_preview_segment_index = int(index)
            self.current_preview_segments_total = int(total)
            self.current_preview_word_length = max(
                1,
                self.current_preview_segment_end
                - self.current_preview_segment_start,
            )
            self.status_var.set(
                f"Читаю {index}/{total}: {short_text}"
            )
        except tk.TclError:
            # Если пользователь успел изменить текст во время чтения, не ломаем
            # само воспроизведение из-за невозможности подсветить старый диапазон.
            pass

    def handle_preview_segment_done(self, info: dict) -> None:
        try:
            spoken_text = str(info.get("text") or "")
            self.session_read_segments += 1
            self.session_read_chars += len(spoken_text)
            self.schedule_queue_status_refresh()
        except Exception:
            pass

        if not bool(self.delete_read_text_var.get()):
            return

        run_id = str(info.get("preview_run_id") or "")
        if (
            run_id
            and self.current_preview_run_id
            and run_id != self.current_preview_run_id
        ):
            return

        # Keep the newest completed sentence as a one-sentence buffer.
        self._delete_pending_read_segment()

        try:
            base_index = str(
                info.get("base_index")
                or self.current_preview_base_index
            )
            original_start = int(info.get("start") or 0)
            original_end = int(info.get("end") or original_start)

            adjusted_start = max(
                0,
                original_start - self.preview_deleted_chars,
            )
            adjusted_end = max(
                adjusted_start,
                original_end - self.preview_deleted_chars,
            )

            start_index = self.text_box.index(
                f"{base_index}+{adjusted_start}c"
            )
            end_index = self.text_box.index(
                f"{base_index}+{adjusted_end}c"
            )

            self.text_box.mark_set(
                self.preview_pending_delete_start_mark,
                start_index,
            )
            self.text_box.mark_gravity(
                self.preview_pending_delete_start_mark,
                "left",
            )
            self.text_box.mark_set(
                self.preview_pending_delete_end_mark,
                end_index,
            )
            self.text_box.mark_gravity(
                self.preview_pending_delete_end_mark,
                "right",
            )
            self.preview_pending_delete_active = True

        except (tk.TclError, TypeError, ValueError):
            self.preview_pending_delete_active = False
