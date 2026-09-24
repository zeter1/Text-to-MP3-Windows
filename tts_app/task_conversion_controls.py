from __future__ import annotations

from .runtime import *
from .problem_logger import ProblemLogger
from .text_processing import *
from .sapi import *
from .ffmpeg_tools import *
from .windows_tools import *

class TaskConversionControlsMixin:
    def ask_output_path(self) -> Path | None:
        kwargs = {}

        last_dir = self.app.settings.get("last_output_dir")
        if last_dir and Path(last_dir).is_dir():
            kwargs["initialdir"] = last_dir

        title = self.tab_title().strip()
        if title and not title.startswith("Задача "):
            safe_name = re.sub(r'[<>:"/\\|?*]+', "_", title).strip(" .")
            if safe_name:
                kwargs["initialfile"] = safe_name + ".mp3"

        chosen = filedialog.asksaveasfilename(
            title=f"Сохранить MP3 — {self.tab_title()}",
            defaultextension=".mp3",
            filetypes=[("MP3", "*.mp3")],
            **kwargs,
        )
        if not chosen:
            return None

        output_path = Path(chosen)
        if output_path.suffix.lower() != ".mp3":
            output_path = output_path.with_suffix(".mp3")

        self.app.settings["last_output_dir"] = str(output_path.parent)
        self.app.schedule_settings_save(self)
        return output_path

    def start_job(self) -> None:
        if self.worker and self.worker.is_alive():
            return

        raw_text = self.text_box.get("1.0", "end-1c")
        text = normalize_text(raw_text)
        if not text:
            messagebox.showwarning(
                APP_TITLE,
                "Вставьте текст.",
            )
            return

        voice = self.voice_var.get().strip()
        if not voice:
            messagebox.showwarning(APP_TITLE, "Выберите голос.")
            return

        output_path = self.ask_output_path()
        if output_path is None:
            return

        if self.app.output_is_used_by_other_task(self, output_path):
            messagebox.showwarning(
                APP_TITLE,
                "Другая вкладка уже создаёт MP3 по этому же пути.\n\n"
                "Выберите другое имя файла, чтобы задачи не перезаписали друг друга.",
            )
            return

        # The save dialog can stay open for a while. Capture the definitive
        # source only after it closes, immediately before tracking and backup.
        raw_text = self.text_box.get("1.0", "end-1c")
        text = normalize_text(raw_text)
        if not text:
            messagebox.showwarning(
                APP_TITLE,
                "В тексте больше нет данных для создания MP3.",
            )
            return

        rate = clamp_int(self.rate_var.get(), RATE_MIN, RATE_MAX, 0)
        pitch = clamp_int(self.pitch_var.get(), PITCH_MIN, PITCH_MAX, 0)
        volume = clamp_int(self.volume_var.get(), 0, 100, 100)
        bitrate = self.bitrate_var.get().strip() or "96k"
        text_metrics = analyze_text_for_logging(
            raw_text,
            text,
        )
        tab_title_snapshot = self.tab_title()
        visible_tab_index_snapshot = self.app.visible_tab_index(self)

        run_id = make_run_id("mp3")
        if not self.track_mp3_source_snapshot(
            run_id,
            raw_text,
        ):
            self.app.logger.event(
                "mp3_source_snapshot_track_failed",
                task_id=run_id,
                run_id=run_id,
                tab_id=self.workspace_id,
                source_chars=len(raw_text),
            )
            messagebox.showerror(
                APP_TITLE,
                "Не удалось зафиксировать точный снимок текста. "
                "Создание MP3 не запущено, чтобы не удалить неверный текст.",
            )
            return

        try:
            backup_path = create_mp3_text_backup(
                run_id,
                tab_title_snapshot,
                raw_text,
            )
        except Exception as exc:
            self.discard_mp3_source_snapshot(
                run_id,
                reason="text_backup_failed",
            )
            self.app.log_ui_error(
                task_id=run_id,
                stage="create_mp3_text_backup",
                exc=exc,
                context={
                    "backup_dir": str(
                        MP3_TEXT_BACKUPS_DIR
                    ),
                    "source_chars": len(raw_text),
                },
            )
            messagebox.showerror(
                APP_TITLE,
                "Не удалось создать обязательный бэкап текста. "
                "Создание MP3 не запущено.\n\n"
                f"{exc}",
            )
            return

        self.current_mp3_text_backup_path = backup_path
        self.app.logger.event(
            "mp3_text_backup_created",
            task_id=run_id,
            run_id=run_id,
            tab_id=self.workspace_id,
            backup_path=str(backup_path),
            source_chars=len(raw_text),
            source_sha256_16=hashlib.sha256(
                raw_text.encode("utf-8")
            ).hexdigest()[:16],
            retention_days=(
                MP3_TEXT_BACKUP_RETENTION_DAYS
            ),
        )

        self.stop_preview(
            preserve_bookmark=True,
            reason="mp3_creation_started",
        )
        self.cancel_event.clear()
        self.current_run_id = run_id
        self.current_output_path = output_path
        self.current_job_stage = "queued"

        self.progress["value"] = 0
        self.start_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.status_var.set(f"Подготовка: {output_path.name}")

        self.worker = threading.Thread(
            target=self._job_worker,
            args=(
                run_id,
                text,
                text_metrics,
                tab_title_snapshot,
                visible_tab_index_snapshot,
                output_path,
                voice,
                rate,
                pitch,
                volume,
                bitrate,
            ),
            name=f"tts-{run_id}",
            daemon=True,
        )
        self.worker.start()

    def resume_recovery_job(
        self,
        manifest: dict,
        work_dir: Path,
    ) -> bool:
        if self.worker and self.worker.is_alive():
            return False

        raw_text = self.text_box.get("1.0", "end-1c")
        text = normalize_text(raw_text)
        if not text:
            return False

        expected_hash = str(manifest.get("text_sha256") or "")
        actual_hash = hashlib.sha256(
            text.encode("utf-8")
        ).hexdigest()
        if not expected_hash or expected_hash != actual_hash:
            return False

        run_id = str(manifest.get("run_id") or "").strip()
        if not run_id:
            return False

        output_path_text = str(
            manifest.get("output_path") or ""
        ).strip()
        if not output_path_text:
            return False
        output_path = Path(output_path_text)

        if self.app.output_is_used_by_other_task(
            self,
            output_path,
        ):
            messagebox.showwarning(
                APP_TITLE,
                "Нельзя продолжить восстановленную задачу: "
                "другая вкладка уже пишет в тот же MP3.",
            )
            return False

        voice = str(
            manifest.get("voice")
            or self.voice_var.get().strip()
        )
        rate = clamp_int(
            manifest.get("rate"),
            RATE_MIN,
            RATE_MAX,
            self.rate_var.get(),
        )
        pitch = clamp_int(
            manifest.get("pitch"),
            PITCH_MIN,
            PITCH_MAX,
            self.pitch_var.get(),
        )
        volume = clamp_int(
            manifest.get("volume"),
            0,
            100,
            self.volume_var.get(),
        )
        bitrate = str(
            manifest.get("bitrate")
            or self.bitrate_var.get().strip()
            or "96k"
        )

        # Reflect the exact saved recovery parameters in the UI. Pitch remains
        # global, so all tabs immediately show the same recovered Pitch value.
        self.voice_var.set(voice)
        self.app.global_rate_var.set(rate)
        self.app.global_pitch_var.set(pitch)
        self.volume_var.set(volume)
        self.bitrate_var.set(bitrate)

        text_metrics = analyze_text_for_logging(
            raw_text,
            text,
        )
        tab_title_snapshot = self.tab_title()
        visible_tab_index_snapshot = (
            self.app.visible_tab_index(self)
        )

        if not self.track_mp3_source_snapshot(
            run_id,
            raw_text,
        ):
            self.app.logger.event(
                "mp3_source_snapshot_track_failed",
                task_id=run_id,
                run_id=run_id,
                tab_id=self.workspace_id,
                source_chars=len(raw_text),
                recovery_resume=True,
            )
            return False

        try:
            backup_path = create_mp3_text_backup(
                run_id,
                tab_title_snapshot,
                raw_text,
            )
        except Exception as exc:
            self.discard_mp3_source_snapshot(
                run_id,
                reason="recovery_text_backup_failed",
            )
            self.app.log_ui_error(
                task_id=run_id,
                stage="create_recovery_mp3_text_backup",
                exc=exc,
                context={
                    "backup_dir": str(
                        MP3_TEXT_BACKUPS_DIR
                    ),
                    "source_chars": len(raw_text),
                },
            )
            messagebox.showerror(
                APP_TITLE,
                "Не удалось создать обязательный бэкап текста. "
                "Восстановление MP3 не запущено.\n\n"
                f"{exc}",
            )
            return False

        self.current_mp3_text_backup_path = backup_path
        self.app.logger.event(
            "mp3_text_backup_created",
            task_id=run_id,
            run_id=run_id,
            tab_id=self.workspace_id,
            backup_path=str(backup_path),
            source_chars=len(raw_text),
            source_sha256_16=hashlib.sha256(
                raw_text.encode("utf-8")
            ).hexdigest()[:16],
            retention_days=(
                MP3_TEXT_BACKUP_RETENTION_DAYS
            ),
            recovery_resume=True,
        )

        self.stop_preview(
            preserve_bookmark=True,
            reason="mp3_recovery_started",
        )
        self.cancel_event.clear()
        self.current_run_id = run_id
        self.current_output_path = output_path
        self.current_job_stage = "queued"

        self.progress["value"] = 0
        self.start_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.status_var.set(
            "Продолжаю аварийно оборванную задачу…"
        )

        self.worker = threading.Thread(
            target=self._job_worker,
            args=(
                run_id,
                text,
                text_metrics,
                tab_title_snapshot,
                visible_tab_index_snapshot,
                output_path,
                voice,
                rate,
                pitch,
                volume,
                bitrate,
                work_dir,
            ),
            name=f"tts-{run_id}-resume",
            daemon=True,
        )
        self.worker.start()
        return True

    def cancel_job(self) -> None:
        if self.worker and self.worker.is_alive():
            self.app.logger.event(
                "task_cancel_requested",
                task_id=self.current_run_id or self.task_id,
                run_id=self.current_run_id,
                tab_id=self.workspace_id,
                stage=self.current_job_stage,
                output_path=(
                    str(self.current_output_path)
                    if self.current_output_path
                    else ""
                ),
            )
            self.cancel_event.set()

            if self.current_job_stage == "ffmpeg":
                self.status_var.set(
                    "Останавливаю FFmpeg и удаляю незавершённый .part.mp3…"
                )
            else:
                self.status_var.set(
                    "Остановка запрошена. Текущий фрагмент будет закончен."
                )

    def set_job_idle(self) -> None:
        self.start_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")
        self.current_output_path = None
        self.current_mp3_text_backup_path = None
        self.current_run_id = None
        self.current_job_stage = "idle"

    def conversion_running(self) -> bool:
        return bool(self.worker and self.worker.is_alive())
