from __future__ import annotations

from .runtime import *
from .problem_logger import ProblemLogger
from .text_processing import *
from .sapi import *
from .ffmpeg_tools import *
from .windows_tools import *

class TaskUiStateMixin:
    def _settings_changed(self, *_args) -> None:
        self.app.schedule_settings_save(self)
        self.app.schedule_workspace_save()

    def apply_voice_list(self, voices: list[str]) -> None:
        self.voice_combo["values"] = voices

        current = self.voice_var.get().strip()
        for voice in voices:
            if current and voice.casefold() == current.casefold():
                self.voice_var.set(voice)
                self.status_var.set(f"Голос выбран: {voice}")
                return

        preferred = str(self.app.settings.get("voice") or "").strip()
        for voice in voices:
            if preferred and voice.casefold() == preferred.casefold():
                self.voice_var.set(voice)
                self.status_var.set(f"Голос выбран: {voice}")
                return

        for voice in voices:
            if DEFAULT_VOICE_HINT.casefold() in voice.casefold():
                self.voice_var.set(voice)
                self.status_var.set(f"Голос выбран: {voice}")
                return

        if voices:
            self.voice_var.set(voices[0])
            self.status_var.set(f"Голос выбран: {voices[0]}")
        else:
            self.voice_var.set("")
            self.status_var.set("SAPI-голоса не найдены.")

    def tab_title(self) -> str:
        try:
            return str(self.app.notebook.tab(self.frame, "text"))
        except tk.TclError:
            return self.restored_title

    def set_tab_title(
        self,
        title: str,
        *,
        custom: bool = True,
        schedule_save: bool = True,
    ) -> None:
        title = title.strip() or f"Вкладка {self.number}"
        if len(title) > 60:
            title = title[:57] + "..."
        self.custom_title = bool(custom)
        self.restored_title = title
        self.app.notebook.tab(self.frame, text=title)
        if schedule_save:
            self.app.schedule_workspace_save()

    def apply_copy_hotkey(
        self,
        *,
        silent: bool = False,
    ) -> bool:
        return self.app.apply_tab_copy_hotkey(
            self,
            silent=silent,
        )

    def workspace_record(self) -> dict:
        return {
            "workspace_id": self.workspace_id,
            "title": self.tab_title(),
            "custom_title": self.custom_title,
            "text_file": f"{self.workspace_id}.txt",
            "voice": self.voice_var.get().strip(),
            "rate": clamp_int(self.app.global_rate_var.get(), RATE_MIN, RATE_MAX, 0),
            "pitch": clamp_int(self.app.global_pitch_var.get(), PITCH_MIN, PITCH_MAX, 0),
            "audio_output": self.app.global_audio_output_var.get().strip()
            or DEFAULT_AUDIO_OUTPUT_LABEL,
            "volume": clamp_int(self.volume_var.get(), 0, 100, 100),
            "bitrate": self.bitrate_var.get().strip() or "96k",
            "delete_read_text": bool(self.delete_read_text_var.get()),
            "auto_read_hotkey_text": bool(
                self.auto_read_hotkey_var.get()
            ),
            "copy_hotkey": (
                self.applied_copy_hotkey
                if self.applied_copy_hotkey
                else TAB_HOTKEY_NONE_LABEL
            ),
            "preview_bookmark": (
                dict(self.preview_bookmark)
                if isinstance(self.preview_bookmark, dict)
                else None
            ),
        }

    def _reset_mp3_source_tracking(self) -> None:
        for mark_name in (
            self.mp3_source_start_mark,
            self.mp3_source_end_mark,
        ):
            try:
                self.text_box.mark_unset(mark_name)
            except tk.TclError:
                pass

        self.mp3_source_run_id = None
        self.mp3_source_chars = 0
        self.mp3_source_sha256 = ""
        self.mp3_source_separator_pending = False
