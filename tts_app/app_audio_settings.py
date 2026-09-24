from __future__ import annotations

from .runtime import *
from .problem_logger import ProblemLogger
from .text_processing import *
from .sapi import *
from .ffmpeg_tools import *
from .windows_tools import *
from .widgets import ClosableNotebook
from .task_tab import TaskTab

class AppAudioSettingsMixin:
    def _load_voices_async(self) -> None:
        current = self.current_tab()
        if current:
            current.status_var.set(
                "Ищу установленные голоса Windows SAPI…"
            )

        def worker() -> None:
            try:
                voices = get_sapi_voices()
                self.events.put(
                    ("", "voices_loaded", voices)
                )
            except Exception as exc:
                self.events.put(
                    (
                        "",
                        "voices_error",
                        {
                            "exc": exc,
                            "traceback": traceback.format_exc(),
                        },
                    )
                )

        threading.Thread(
            target=worker,
            name="voice-discovery",
            daemon=True,
        ).start()

    def _load_audio_outputs_async(self) -> None:
        def worker() -> None:
            try:
                outputs, current = get_sapi_audio_outputs()
                self.events.put(
                    (
                        "",
                        "audio_outputs_loaded",
                        {
                            "outputs": outputs,
                            "current": current,
                        },
                    )
                )
            except Exception as exc:
                self.events.put(
                    (
                        "",
                        "audio_outputs_error",
                        {
                            "exc": exc,
                            "traceback": traceback.format_exc(),
                        },
                    )
                )

        threading.Thread(
            target=worker,
            name="audio-output-discovery",
            daemon=True,
        ).start()

    def refresh_audio_outputs(self) -> None:
        self.logger.event("audio_outputs_refresh_requested")
        self._load_audio_outputs_async()

    def stop_other_previews(self, active: TaskTab) -> None:
        for task in list(self.tabs.values()):
            if task is not active:
                task.stop_preview(
                    preserve_bookmark=True,
                    reason="another_tab_started_preview",
                )

    def output_is_used_by_other_task(
        self,
        active: TaskTab,
        output_path: Path,
    ) -> bool:
        try:
            wanted = os.path.normcase(
                os.path.abspath(str(output_path))
            )
        except Exception:
            wanted = str(output_path)

        for task in self.tabs.values():
            if task is active or not task.conversion_running():
                continue

            if task.current_output_path is None:
                continue

            try:
                other_path = os.path.normcase(
                    os.path.abspath(str(task.current_output_path))
                )
            except Exception:
                other_path = str(task.current_output_path)

            if other_path == wanted:
                return True

        return False

    def open_logs(self) -> None:
        self._open_folder_safe(
            LOGS_DIR,
            "open_logs_folder",
        )

    def open_settings(self) -> None:
        self._open_folder_safe(
            SETTINGS_DIR,
            "open_settings_folder",
        )

    def _open_folder_safe(
        self,
        path: Path,
        stage: str,
    ) -> None:
        try:
            open_folder(path)
        except Exception as exc:
            self.log_ui_error(
                task_id=(
                    self.current_tab().task_id
                    if self.current_tab()
                    else "app"
                ),
                stage=stage,
                exc=exc,
                context={"path": str(path)},
            )
            messagebox.showerror(
                APP_TITLE,
                f"Не удалось открыть папку:\n{path}\n\n{exc}",
            )

    def _on_global_rate_changed(self, *_args) -> None:
        if self._global_rate_syncing:
            return

        try:
            rate = clamp_int(
                self.global_rate_var.get(),
                RATE_MIN,
                RATE_MAX,
                0,
            )
        except tk.TclError:
            return

        # Normalize out-of-range/manually typed values without recursive work.
        try:
            current = int(self.global_rate_var.get())
        except Exception:
            current = rate

        if current != rate:
            self._global_rate_syncing = True
            try:
                self.global_rate_var.set(rate)
            finally:
                self._global_rate_syncing = False

        self.settings["rate"] = rate

        # All sliders already share global_rate_var. Only the small numeric
        # label belongs to each tab, so update those labels explicitly.
        for task in list(self.tabs.values()):
            task.rate_value_var.set(str(rate))

        self.logger.event(
            "global_rate_changed",
            rate=rate,
        )
        self.schedule_settings_save()
        self.schedule_workspace_save()

    def _on_global_pitch_changed(self, *_args) -> None:
        if self._global_pitch_syncing:
            return

        try:
            pitch = clamp_int(
                self.global_pitch_var.get(),
                PITCH_MIN,
                PITCH_MAX,
                0,
            )
        except tk.TclError:
            return

        # Normalize out-of-range/manually typed values without recursive work.
        try:
            current = int(self.global_pitch_var.get())
        except Exception:
            current = pitch

        if current != pitch:
            self._global_pitch_syncing = True
            try:
                self.global_pitch_var.set(pitch)
            finally:
                self._global_pitch_syncing = False

        self.settings["pitch"] = pitch

        # Each tab owns only the little numeric label; all scales themselves
        # already share self.global_pitch_var.
        for task in list(self.tabs.values()):
            task.pitch_value_var.set(str(pitch))

        self.schedule_settings_save()
        self.schedule_workspace_save()

    def _on_global_audio_output_changed(self, *_args) -> None:
        selected = (
            self.global_audio_output_var.get().strip()
            or DEFAULT_AUDIO_OUTPUT_LABEL
        )
        self.settings["audio_output"] = selected
        self.logger.event(
            "audio_output_selected",
            audio_output=selected,
        )
        self.schedule_settings_save()
        self.schedule_workspace_save()

    def schedule_settings_save(
        self,
        source_tab: TaskTab | None = None,
    ) -> None:
        if source_tab is not None:
            self.settings["voice"] = source_tab.voice_var.get().strip()
            self.settings["rate"] = clamp_int(
                self.global_rate_var.get(),
                RATE_MIN,
                RATE_MAX,
                0,
            )
            self.settings["pitch"] = clamp_int(
                self.global_pitch_var.get(),
                PITCH_MIN,
                PITCH_MAX,
                0,
            )
            self.settings["audio_output"] = (
                self.global_audio_output_var.get().strip()
                or DEFAULT_AUDIO_OUTPUT_LABEL
            )
            self.settings["volume"] = clamp_int(
                source_tab.volume_var.get(),
                0,
                100,
                100,
            )
            self.settings["bitrate"] = (
                source_tab.bitrate_var.get().strip()
                or "96k"
            )

        if self.settings_after_id:
            try:
                self.root.after_cancel(
                    self.settings_after_id
                )
            except Exception:
                pass

        self.settings_after_id = self.root.after(
            400,
            self.save_settings,
        )

    def save_settings(self) -> None:
        self.settings_after_id = None

        try:
            self.settings["window_geometry"] = self.root.geometry()

            # Since 4.0 hotkeys live in workspace.json PER TAB. Keep old global
            # setting disabled so an old value can never leak into a new tab.
            self.settings["global_copy_enabled"] = False

            self.settings["autostart_windows"] = bool(
                self.autostart_windows_var.get()
            )
            atomic_write_json(
                SETTINGS_FILE,
                normalize_settings(self.settings),
            )
        except Exception as exc:
            self.logger.event(
                "settings_save_failed",
                error=f"{type(exc).__name__}: {exc}",
            )

    def log_ui_error(
        self,
        *,
        task_id: str,
        stage: str,
        exc: BaseException,
        context: dict,
    ) -> None:
        self.logger.error(
            task_id=task_id,
            stage=stage,
            exc=exc,
            traceback_text=traceback.format_exc(),
            context=context,
        )
