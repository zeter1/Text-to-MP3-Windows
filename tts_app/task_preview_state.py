from __future__ import annotations

from .runtime import *
from .problem_logger import ProblemLogger
from .text_processing import *
from .sapi import *
from .ffmpeg_tools import *
from .windows_tools import *

class TaskPreviewStateMixin:
    def _full_text_sha256(self) -> str:
        return hashlib.sha256(
            self._full_text().encode("utf-8")
        ).hexdigest()

    def _text_offset_from_index(self, index: str) -> int:
        try:
            # Tk 8.6 reports a non-BMP character (for example an emoji) as
            # two UTF-16 units in Text.count(..., "chars"), while Python
            # offsets and Text's +Nc index movement treat it as one character.
            # Using the actual returned string keeps bookmarks and delete
            # offsets in the same coordinate system as Python's len().
            return len(
                self.text_box.get(
                    "1.0",
                    self.text_box.index(index),
                )
            )
        except (tk.TclError, TypeError, ValueError):
            return 0

    def _index_from_text_offset(self, offset: int) -> str:
        offset = max(0, int(offset))
        return self.text_box.index(f"1.0+{offset}c")

    def clear_saved_preview_bookmark(
        self,
        *,
        reason: str,
        schedule_save: bool = True,
    ) -> None:
        had_bookmark = isinstance(self.preview_bookmark, dict)
        self.preview_bookmark = None
        self.preview_keep_bookmark_after_stop = False
        if had_bookmark:
            self.app.logger.event(
                "preview_bookmark_cleared",
                task_id=self.task_id,
                tab_id=self.workspace_id,
                reason=reason,
            )
        if schedule_save:
            self.app.schedule_workspace_save()

    def _bookmark_is_valid(self) -> bool:
        """
        A bookmark remains valid when text was only APPENDED after it was saved.

        We hash exactly the prefix that existed at pause time. Therefore adding
        new chapters at the bottom does not invalidate the saved sentence, while
        edits/deletions inside the old text still invalidate it safely.
        """
        bookmark = self.preview_bookmark
        if not isinstance(bookmark, dict):
            return False

        try:
            offset = int(bookmark.get("char_offset") or 0)
            saved_chars = int(bookmark.get("text_chars") or 0)
        except Exception:
            return False

        full_text = self._full_text()
        if (
            not full_text
            or saved_chars <= 0
            or len(full_text) < saved_chars
            or not (0 <= offset < len(full_text))
        ):
            return False

        expected_hash = str(bookmark.get("text_sha256") or "")
        if not expected_hash:
            return False

        saved_prefix = full_text[:saved_chars]
        return expected_hash == hashlib.sha256(
            saved_prefix.encode("utf-8")
        ).hexdigest()

    def _restore_preview_bookmark_ui(self) -> None:
        if not isinstance(self.preview_bookmark, dict):
            return
        if not self._bookmark_is_valid():
            self.preview_bookmark = None
            return

        try:
            offset = int(self.preview_bookmark.get("char_offset") or 0)
            sentence_start = max(
                0,
                int(
                    self.preview_bookmark.get(
                        "sentence_start_offset",
                        offset,
                    )
                    or 0
                ),
            )
            sentence_end = max(
                sentence_start + 1,
                int(
                    self.preview_bookmark.get(
                        "sentence_end_offset",
                        sentence_start + 1,
                    )
                    or (sentence_start + 1)
                ),
            )
            start_index = self._index_from_text_offset(sentence_start)
            end_index = self._index_from_text_offset(
                min(sentence_end, len(self._full_text()))
            )
            self.clear_preview_highlight()
            self.text_box.tag_add(
                "preview_current",
                start_index,
                end_index,
            )
            self.text_box.tag_raise("preview_current")
            self.text_box.see(start_index)
            self.text_box.mark_set("insert", start_index)
        except tk.TclError:
            pass

        self.preview_is_paused = True
        self.pause_preview_button.configure(state="disabled")
        self.resume_preview_button.configure(state="normal")
        self.stop_preview_button.configure(state="normal")
        self.preview_button.configure(state="normal")
        self.preview_cursor_button.configure(state="normal")

        offset = int(self.preview_bookmark.get("char_offset") or 0)
        total = max(1, len(self._full_text()))
        percent = min(100.0, max(0.0, offset / total * 100.0))
        snippet = str(self.preview_bookmark.get("snippet") or "").strip()
        if snippet:
            self.current_preview_segment_text = snippet
            self.status_var.set(
                f"Сохранена пауза ({percent:.1f}%): {snippet}"
            )
        else:
            self.status_var.set(
                f"Сохранена пауза книги: {percent:.1f}%. "
                "Нажмите «Продолжить»."
            )

    def save_current_preview_bookmark(
        self,
        *,
        reason: str,
        absolute_offset: int | None = None,
        word_length: int | None = None,
        force_save: bool = True,
        log_event: bool = True,
        accuracy: str = "sentence",
    ) -> bool:
        full_text = self._full_text()
        if not full_text:
            return False

        if absolute_offset is None:
            absolute_offset = self.current_preview_segment_start
        absolute_offset = max(
            0,
            min(int(absolute_offset), max(0, len(full_text) - 1)),
        )
        if word_length is None:
            word_length = self.current_preview_word_length
        word_length = max(1, int(word_length or 1))

        snippet_start = max(0, absolute_offset - 35)
        snippet_end = min(len(full_text), absolute_offset + 90)
        snippet = " ".join(
            full_text[snippet_start:snippet_end].split()
        )
        if len(snippet) > 120:
            snippet = snippet[:117] + "..."

        sentence_start = max(
            0,
            min(
                int(self.current_preview_segment_start),
                max(0, len(full_text) - 1),
            ),
        )
        sentence_end = max(
            sentence_start + 1,
            min(
                int(
                    self.current_preview_segment_end
                    or (sentence_start + 1)
                ),
                len(full_text),
            ),
        )
        sentence_text = full_text[sentence_start:sentence_end]
        previous_sentence_start = previous_preview_segment_start(
            full_text,
            sentence_start,
        )

        self.preview_bookmark = {
            "schema": 2,
            "state": "paused",
            "saved_at": now_iso(),
            "reason": reason,
            "char_offset": absolute_offset,
            "sentence_start_offset": sentence_start,
            "sentence_end_offset": sentence_end,
            "previous_sentence_start_offset": previous_sentence_start,
            "sentence_sha256": hashlib.sha256(
                sentence_text.encode("utf-8")
            ).hexdigest(),
            "end_offset": max(
                absolute_offset + 1,
                int(self.current_preview_range_end or len(full_text)),
            ),
            "range_was_to_end": bool(self.current_preview_follow_end),
            "word_length": word_length,
            "segment_index": self.current_preview_segment_index,
            "segments_total": self.current_preview_segments_total,
            "source_mode": self.current_preview_source_mode,
            "text_sha256": hashlib.sha256(
                full_text.encode("utf-8")
            ).hexdigest(),
            "text_chars": len(full_text),
            "snippet": snippet,
        }
        self.preview_keep_bookmark_after_stop = True

        if log_event:
            self.app.logger.event(
                "preview_bookmark_saved",
                task_id=self.task_id,
                tab_id=self.workspace_id,
                preview_run_id=self.current_preview_run_id,
                reason=reason,
                accuracy=accuracy,
                char_offset=absolute_offset,
                sentence_start_offset=sentence_start,
                sentence_end_offset=sentence_end,
                previous_sentence_start_offset=previous_sentence_start,
                word_length=word_length,
                text_chars=len(full_text),
                source_mode=self.current_preview_source_mode,
            )

        if force_save:
            # Persist immediately so a pause survives an immediate restart.
            self.app.save_workspace()
        else:
            self.app.schedule_workspace_save()
        return True

    def apply_preview_checkpoint(self, info: dict) -> None:
        if not (
            self.preview_thread
            and self.preview_thread.is_alive()
        ):
            return
        checkpoint_run_id = str(
            info.get("preview_run_id") or ""
        )
        if (
            checkpoint_run_id
            and checkpoint_run_id != self.current_preview_run_id
        ):
            return
        try:
            base_index = str(info.get("base_index") or self.current_preview_base_index)
            segment_start = int(info.get("start") or 0)
            word_position = max(0, int(info.get("word_position") or 0))
            word_length = max(1, int(info.get("word_length") or 1))
            adjusted_segment_start = max(
                0,
                segment_start - int(self.preview_deleted_chars),
            )
            point_index = self.text_box.index(
                f"{base_index}+{adjusted_segment_start + word_position}c"
            )
            absolute_offset = self._text_offset_from_index(point_index)
        except (tk.TclError, TypeError, ValueError):
            absolute_offset = self.current_preview_segment_start
            word_length = max(1, self.current_preview_word_length or 1)

        self.current_preview_word_length = word_length
        checkpoint_reason = str(
            info.get("reason")
            or "sapi_checkpoint"
        )
        raw_position = info.get("sapi_position_raw")
        accuracy = (
            "exact_word"
            if (
                checkpoint_reason == "pause_inside_segment"
                and raw_position is not None
                and word_length > 0
            )
            else "sentence"
        )
        self.save_current_preview_bookmark(
            reason=checkpoint_reason,
            absolute_offset=absolute_offset,
            word_length=word_length,
            force_save=True,
            log_event=True,
            accuracy=accuracy,
        )
        self.log_text_state(
            "preview_checkpoint",
            force=True,
        )
        self._restore_preview_bookmark_ui()

    def resume_saved_preview(self) -> bool:
        if self.preview_thread and self.preview_thread.is_alive():
            return False
        if not self._bookmark_is_valid():
            if self.preview_bookmark is not None:
                messagebox.showwarning(
                    APP_TITLE,
                    "Сохранённая позиция чтения больше не подходит: "
                    "текст вкладки был изменён. Позиция сброшена.",
                )
                self.clear_saved_preview_bookmark(
                    reason="text_changed",
                    schedule_save=True,
                )
                self.preview_finished_ui()
            return False

        bookmark = dict(self.preview_bookmark or {})
        word_offset = int(bookmark.get("char_offset") or 0)
        paused_sentence_offset = max(
            0,
            int(
                bookmark.get(
                    "sentence_start_offset",
                    word_offset,
                )
                or 0
            ),
        )

        # User-requested behavior: ALWAYS rewind one preview sentence when
        # pressing Continue. This avoids SAPI jumping to the next sentence and
        # also gives a little context before the exact pause point.
        stored_previous = bookmark.get(
            "previous_sentence_start_offset"
        )
        if stored_previous is None:
            # Migration for bookmarks created by version 3.3.
            offset = previous_preview_segment_start(
                self._full_text(),
                paused_sentence_offset,
            )
        else:
            offset = max(0, int(stored_previous))

        saved_chars = max(
            0,
            int(bookmark.get("text_chars") or len(self._full_text())),
        )
        saved_end_offset = max(
            offset + 1,
            int(bookmark.get("end_offset") or saved_chars),
        )
        range_was_to_end = bool(
            bookmark.get(
                "range_was_to_end",
                saved_end_offset >= saved_chars,
            )
        )

        start_index = self._index_from_text_offset(offset)
        if range_was_to_end:
            end_offset = len(self._full_text())
        else:
            end_offset = max(
                offset + 1,
                min(saved_end_offset, len(self._full_text())),
            )
        end_index = self._index_from_text_offset(end_offset)
        raw_text = self.text_box.get(start_index, end_index)
        if not normalize_text(raw_text):
            self.clear_saved_preview_bookmark(
                reason="bookmark_at_end",
                schedule_save=True,
            )
            return False

        self.log_text_state(
            "preview_bookmark_resume",
            force=True,
        )
        self.app.logger.event(
            "preview_bookmark_resumed",
            task_id=self.task_id,
            tab_id=self.workspace_id,
            saved_at=bookmark.get("saved_at"),
            char_offset=word_offset,
            paused_sentence_offset=paused_sentence_offset,
            resume_previous_sentence_offset=offset,
            range_was_to_end=range_was_to_end,
            text_chars=len(self._full_text()),
        )
        self._start_preview_worker(
            raw_text=raw_text,
            preview_base_index=start_index,
            source_mode="saved_bookmark",
            selected_text=False,
            preserve_existing_bookmark=True,
        )
        return True

    def selected_or_all_text(self) -> tuple[str, bool, str]:
        try:
            start_index = self.text_box.index("sel.first")
            selected = self.text_box.get("sel.first", "sel.last")
            if selected.strip():
                return selected, True, start_index
        except tk.TclError:
            pass

        return self.text_box.get("1.0", "end-1c"), False, "1.0"

    def text_from_insert_cursor(self) -> tuple[str, str]:
        cursor_index = self.text_box.index("insert")
        return self.text_box.get(cursor_index, "end-1c"), cursor_index

    def start_preview_from_cursor(self) -> None:
        if self.preview_thread and self.preview_thread.is_alive():
            return

        raw_text, cursor_index = self.text_from_insert_cursor()
        if not normalize_text(raw_text):
            messagebox.showwarning(
                APP_TITLE,
                "После курсора нет текста для воспроизведения.",
            )
            return

        self._start_preview_worker(
            raw_text=raw_text,
            preview_base_index=cursor_index,
            source_mode="cursor",
            selected_text=False,
        )

    def start_preview(self) -> None:
        if self.preview_thread and self.preview_thread.is_alive():
            return

        raw_text, is_selection, preview_base_index = self.selected_or_all_text()

        if not normalize_text(raw_text):
            messagebox.showwarning(
                APP_TITLE,
                "Вставьте текст для воспроизведения.",
            )
            return

        self._start_preview_worker(
            raw_text=raw_text,
            preview_base_index=preview_base_index,
            source_mode="selection" if is_selection else "all",
            selected_text=is_selection,
        )
