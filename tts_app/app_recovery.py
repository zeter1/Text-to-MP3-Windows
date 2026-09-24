from __future__ import annotations

from .runtime import *
from .problem_logger import ProblemLogger
from .text_processing import *
from .sapi import *
from .ffmpeg_tools import *
from .windows_tools import *
from .widgets import *

class AppRecoveryMixin:
    def _discard_recovery_job(
        self,
        manifest: dict,
        reason: str,
    ) -> None:
        raw_dir = str(manifest.get("_job_dir") or "").strip()
        work_dir = Path(raw_dir) if raw_dir else None
        try:
            output_text = str(
                manifest.get("output_path") or ""
            ).strip()
            if output_text:
                output_path = Path(output_text)
                partial_output = output_path.with_name(
                    output_path.stem + ".part.mp3"
                )
                try:
                    partial_output.unlink(missing_ok=True)
                except OSError:
                    pass

            if (
                work_dir is not None
                and work_dir.exists()
                and work_dir.name.startswith("job_")
                and work_dir.resolve().parent
                == RECOVERY_DIR.resolve()
            ):
                shutil.rmtree(
                    work_dir,
                    ignore_errors=True,
                )
        finally:
            self.logger.event(
                "recovery_job_discarded",
                run_id=manifest.get("run_id"),
                tab_id=manifest.get("tab_id"),
                workspace_id=manifest.get("workspace_id"),
                reason=reason,
            )

    def _offer_recovery_jobs(self) -> None:
        if self.shutdown_in_progress:
            return

        jobs, removed = scan_recovery_jobs()
        if removed:
            self.logger.event(
                "recovery_cleanup",
                removed_dirs=removed,
                retention_days=RECOVERY_RETENTION_DAYS,
            )

        if not jobs:
            return

        self.logger.event(
            "recovery_jobs_found",
            count=len(jobs),
            run_ids=[
                str(job.get("run_id") or "")
                for job in jobs
            ],
        )

        for manifest in jobs:
            if self.shutdown_in_progress:
                return

            run_id = str(manifest.get("run_id") or "")
            workspace_id = str(
                manifest.get("workspace_id") or ""
            )
            work_dir = Path(
                str(manifest.get("_job_dir") or "")
            )
            chunks_total = clamp_int(
                manifest.get("chunks_total"),
                0,
                10_000_000,
                0,
            )
            expected_hash = str(
                manifest.get("text_sha256") or ""
            )

            source_name = Path(
                str(
                    manifest.get("source_text_file")
                    or "source_text.txt"
                )
            ).name
            source_path = work_dir / source_name
            recovery_text = ""
            recovery_source_valid = False
            try:
                if source_path.exists():
                    recovery_text = source_path.read_text(
                        encoding="utf-8"
                    )
                    recovery_source_valid = (
                        hashlib.sha256(
                            recovery_text.encode("utf-8")
                        ).hexdigest()
                        == expected_hash
                    )
            except Exception:
                recovery_text = ""
                recovery_source_valid = False

            task = self._task_by_workspace_id(
                workspace_id
            )
            task_matches = False
            if task is not None:
                current_text = normalize_text(
                    task.text_box.get("1.0", "end-1c")
                )
                task_matches = (
                    bool(expected_hash)
                    and hashlib.sha256(
                        current_text.encode("utf-8")
                    ).hexdigest()
                    == expected_hash
                )

            if (
                task is not None
                and task_matches
                and task.conversion_running()
            ):
                self.logger.event(
                    "recovery_job_deferred",
                    run_id=run_id,
                    tab_id=task.workspace_id,
                    workspace_id=workspace_id,
                    reason="tab_busy",
                )
                continue

            if not task_matches and not recovery_source_valid:
                reason = (
                    "исходная вкладка больше не существует"
                    if task is None
                    else "текст исходной вкладки изменился"
                )
                delete = messagebox.askyesno(
                    APP_TITLE,
                    "Найдена незавершённая конвертация, но "
                    f"{reason}.\n\n"
                    "Точная копия текста этой задачи также недоступна, "
                    "поэтому безопасно продолжить нельзя.\n\n"
                    "Удалить данные восстановления?",
                )
                if delete:
                    self._discard_recovery_job(
                        manifest,
                        "source_text_unavailable",
                    )
                continue

            valid_chunks, _, _, _ = (
                count_valid_recovery_wavs(
                    work_dir,
                    chunks_total,
                )
            )

            recovery_state = str(
                manifest.get("state") or ""
            )
            reason_text = (
                "Найдена конвертация, завершившаяся ошибкой."
                if recovery_state == "failed"
                else "Найдена аварийно оборванная конвертация."
            )

            original_title = str(
                manifest.get("tab_title")
                or (
                    task.tab_title()
                    if task is not None
                    else "Восстановление"
                )
            )
            source_note = ""
            if not task_matches and recovery_source_valid:
                source_note = (
                    "\nИсходная вкладка изменилась/удалена, но "
                    "сохранена точная копия текста задачи. "
                    "Для неё будет создана отдельная вкладка восстановления.\n"
                )

            answer = messagebox.askyesnocancel(
                APP_TITLE,
                reason_text + "\n\n"
                f"Вкладка: {original_title}\n"
                f"Готово фрагментов: {valid_chunks}/{chunks_total}\n"
                f"Файл: {manifest.get('output_path', '')}\n"
                + source_note
                + "\nДа — продолжить с готовых WAV.\n"
                "Нет — удалить данные восстановления.\n"
                "Отмена — оставить данные и решить позже.",
            )

            if answer is None:
                self.logger.event(
                    "recovery_job_deferred",
                    run_id=run_id,
                    tab_id=(
                        task.task_id
                        if task is not None
                        else manifest.get("tab_id")
                    ),
                    workspace_id=workspace_id,
                    recovered_chunks=valid_chunks,
                    chunks_total=chunks_total,
                    reason="user_deferred",
                )
                break

            if answer is False:
                self._discard_recovery_job(
                    manifest,
                    "user_discarded",
                )
                continue

            target_task = task if task_matches else None
            if target_task is None:
                target_task = self.new_tab(
                    title=(
                        f"{original_title} — восстановление"
                    ),
                    restored_text=recovery_text,
                    custom_title=True,
                    save_workspace=True,
                )
                self.save_workspace()

            if target_task.resume_recovery_job(
                manifest,
                work_dir,
            ):
                continue

            messagebox.showwarning(
                APP_TITLE,
                "Не удалось запустить восстановление этой задачи. "
                "Данные оставлены в папке восстановления.",
            )

    def toggle_windows_autostart(self) -> None:
        desired = bool(self.autostart_windows_var.get())

        if os.name != "nt":
            self.autostart_windows_var.set(False)
            self.settings["autostart_windows"] = False
            messagebox.showwarning(
                APP_TITLE,
                "Автозапуск этой программы поддерживается только в Windows.",
            )
            return

        try:
            command = set_windows_autostart(desired)
            self.settings["autostart_windows"] = desired
            self.logger.event(
                "windows_autostart_changed",
                enabled=desired,
                launch_command=command if desired else "",
            )
            # Запись реестра уже изменена сразу; JSON сохраняем без задержки,
            # чтобы состояние не потерялось при немедленном закрытии программы.
            self.save_settings()
        except Exception as exc:
            actual = windows_autostart_is_enabled()
            self.autostart_windows_var.set(actual)
            self.settings["autostart_windows"] = actual
            self.logger.event(
                "windows_autostart_change_failed",
                requested_enabled=desired,
                actual_enabled=actual,
                error=f"{type(exc).__name__}: {exc}",
            )
            messagebox.showerror(
                APP_TITLE,
                "Не удалось изменить автозапуск с Windows.\n\n"
                f"{exc}",
            )
