from __future__ import annotations

from .runtime import *

class ProblemLoggerInsightsMixin:
    def _warnings(self) -> list[str]:
        warnings: list[str] = []
        stats = dict(self.stats)

        if self.previous_unclean_sessions:
            warnings.append(
                "Обнаружены предыдущие сессии, завершившиеся аварийно."
            )
        if stats["errors"]:
            warnings.append(
                f"Зафиксировано ошибок: {stats['errors']}."
            )
        if stats["sapi_retries"]:
            warnings.append(
                f"SAPI потребовал повторных попыток: {stats['sapi_retries']}."
            )
        if stats["slow_chunks"]:
            warnings.append(
                f"Медленных фрагментов SAPI: {stats['slow_chunks']}."
            )
        if stats["audio_output_mismatches"]:
            warnings.append(
                "Запрошенное и фактическое устройство воспроизведения "
                "хотя бы один раз не совпали."
            )
        if stats["low_disk_warnings"]:
            warnings.append(
                "Во время обработки было мало свободного места на диске."
            )
        if stats["forced_shutdowns"]:
            warnings.append(
                "Программа была вынуждена закрыться до остановки всех потоков."
            )
        if stats["output_validation_warnings"]:
            warnings.append(
                "Не все готовые MP3 удалось полноценно проверить после кодирования."
            )
        if int(stats.get("global_copy_failures") or 0):
            warnings.append(
                "Есть неудачные попытки глобального копирования: "
                f"{int(stats.get('global_copy_failures') or 0)}."
            )

        unresolved_recovery = max(
            0,
            int(stats["recovery_jobs_found"])
            - int(stats["recovery_jobs_resumed"])
            - int(stats["recovery_jobs_discarded"]),
        )
        if unresolved_recovery:
            warnings.append(
                "Есть отложенные данные восстановления: "
                f"{unresolved_recovery}."
            )

        with self.state_lock:
            if self.active_tasks:
                warnings.append(
                    "Есть задачи без финального статуса; при аварийном завершении "
                    "смотрите их last_progress."
                )
            if any(
                bool(item.get("recovery_preserved"))
                for item in self.completed_tasks
            ):
                warnings.append(
                    "После ошибки сохранены WAV/исходный текст для восстановления "
                    "при следующем запуске."
                )

        return warnings

    def _compact_task_summary(self, summary: dict) -> dict:
        duration = float(summary.get("duration_sec") or 0)
        audio_duration = float(summary.get("audio_duration_sec") or 0)
        sapi_duration = float(summary.get("sapi_duration_sec") or 0)
        ffmpeg_duration = float(summary.get("ffmpeg_duration_sec") or 0)
        output_size = int(summary.get("output_size_bytes") or 0)
        temp_wav = int(summary.get("temp_wav_bytes") or 0)

        return {
            "run_id": summary.get("run_id") or summary.get("task_id"),
            "tab_id": summary.get("tab_id"),
            "tab_title": summary.get("tab_title"),
            "visible_tab_index": summary.get(
                "visible_tab_index"
            ),
            "status": summary.get("status"),
            "voice": summary.get("voice"),
            "rate": summary.get("rate"),
            "pitch": summary.get("pitch"),
            "bitrate": summary.get("bitrate"),
            "output_path": summary.get("output_path"),
            "chars": summary.get("chars"),
            "chunks": summary.get("chunks"),
            "duration_sec": round(duration, 3),
            "sapi_duration_sec": round(sapi_duration, 3),
            "ffmpeg_duration_sec": round(ffmpeg_duration, 3),
            "audio_duration_sec": round(audio_duration, 3),
            "retry_count": summary.get("retry_count"),
            "temp_wav_bytes": temp_wav,
            "output_size_bytes": output_size,
            "ffmpeg_share_percent": (
                round(ffmpeg_duration / duration * 100.0, 2)
                if duration > 0
                else 0
            ),
            "overall_realtime_factor": (
                round(audio_duration / duration, 2)
                if duration > 0 and audio_duration > 0
                else 0
            ),
            "sapi_realtime_factor": (
                round(audio_duration / sapi_duration, 2)
                if (
                    sapi_duration > 0
                    and audio_duration > 0
                    and not int(
                        summary.get("resumed_existing_chunks")
                        or 0
                    )
                )
                else 0
            ),
            "temp_wav_to_mp3_ratio": (
                round(temp_wav / output_size, 2)
                if output_size > 0
                else 0
            ),
            "output_validation": summary.get("output_validation"),
            "chunk_timing_sec": summary.get("chunk_timing_sec"),
            "disk_forecast": summary.get("disk_forecast"),
            "output_before": summary.get("output_before"),
            "output_after": summary.get("output_after"),
            "resumed_existing_chunks": summary.get(
                "resumed_existing_chunks",
                0,
            ),
            "recovery_preserved": summary.get(
                "recovery_preserved",
                False,
            ),
            "recovery_dir": summary.get(
                "recovery_dir",
                "",
            ),
        }

    def _observations(self) -> list[str]:
        observations: list[str] = []
        stats = dict(self.stats)

        captures = int(stats.get("global_copy_captures") or 0)
        captured_chars = int(
            stats.get("global_copy_captured_chars") or 0
        )
        capture_failures = int(
            stats.get("global_copy_failures") or 0
        )
        duplicate_skips = int(
            stats.get("global_copy_duplicates_skipped") or 0
        )
        deleted_segments = int(
            stats.get("read_deleted_segments") or 0
        )
        deleted_chars = int(
            stats.get("read_deleted_chars") or 0
        )
        tails = int(
            stats.get("appended_tail_continuations") or 0
        )
        pauses = int(
            stats.get("preview_pause_requests") or 0
        )
        resumes = int(
            stats.get("preview_resume_requests") or 0
        )

        if captures:
            observations.append(
                "Глобальная горячая клавиша успешно захватила "
                f"{captures} фрагм.; добавлено "
                f"{captured_chars:,} символов.".replace(",", " ")
            )

            latency_sum = float(
                stats.get("global_copy_latency_ms_sum") or 0
            )
            latency_max = float(
                stats.get("global_copy_latency_ms_max") or 0
            )
            if latency_sum > 0:
                observations.append(
                    "Средняя задержка получения нового буфера обмена: "
                    f"{latency_sum / captures:.0f} мс; "
                    f"максимальная: {latency_max:.0f} мс."
                )

        if capture_failures:
            observations.append(
                "Неудачных попыток глобального копирования: "
                f"{capture_failures}."
            )
        if duplicate_skips:
            observations.append(
                "Защита от двойного срабатывания пропустила повторов: "
                f"{duplicate_skips}."
            )
        if deleted_segments or deleted_chars:
            observations.append(
                "Автоудаление прочитанного: "
                f"{deleted_segments} предложений / "
                f"{deleted_chars:,} символов.".replace(",", " ")
            )
        if tails:
            observations.append(
                "Автоматически продолжено чтение добавленного хвоста: "
                f"{tails} раз."
            )
        if pauses or resumes:
            observations.append(
                f"Паузы/продолжения чтения: {pauses}/{resumes}."
            )

        finish_total = sum(
            int(stats.get(key) or 0)
            for key in (
                "preview_finish_completed",
                "preview_finish_user_stop",
                "preview_finish_resume_rewind",
                "preview_finish_another_tab",
                "preview_finish_app_exit",
                "preview_finish_other",
            )
        )
        if finish_total:
            observations.append(
                "Причины завершения preview: "
                f"completed={stats.get('preview_finish_completed', 0)}, "
                f"user_stop={stats.get('preview_finish_user_stop', 0)}, "
                f"resume_rewind={stats.get('preview_finish_resume_rewind', 0)}, "
                f"another_tab={stats.get('preview_finish_another_tab', 0)}, "
                f"app_exit={stats.get('preview_finish_app_exit', 0)}, "
                f"other={stats.get('preview_finish_other', 0)}."
            )

        task = self.last_successful_task
        if not task:
            return observations

        chars = int(task.get("chars") or 0)
        audio_sec = float(task.get("audio_duration_sec") or 0)
        temp_bytes = int(task.get("temp_wav_bytes") or 0)
        ffmpeg_share = float(task.get("ffmpeg_share_percent") or 0)
        retries = int(task.get("retry_count") or 0)
        resumed = int(
            task.get("resumed_existing_chunks") or 0
        )

        if chars >= 500_000:
            observations.append(
                f"Обработан очень большой текст: {chars:,} символов.".replace(
                    ",",
                    " ",
                )
            )
        if audio_sec >= 4 * 3600:
            observations.append(
                "Создано очень длинное аудио: "
                f"{audio_sec / 3600:.2f} ч."
            )
        if temp_bytes >= 1024 ** 3:
            observations.append(
                "Пиковый объём временных WAV превысил 1 ГБ: "
                f"{temp_bytes / 1024 ** 3:.2f} ГБ."
            )
        if ffmpeg_share >= 20:
            observations.append(
                "FFmpeg занял заметную долю общего времени: "
                f"{ffmpeg_share:.1f}%."
            )
        if retries == 0:
            observations.append(
                "Последняя успешная задача завершилась без повторных попыток SAPI."
            )
        if resumed:
            observations.append(
                "Последняя задача была продолжена после сбоя: "
                f"повторно использовано WAV-фрагментов: {resumed}."
            )

        output_before = task.get("output_before")
        if (
            isinstance(output_before, dict)
            and output_before.get("exists") is True
        ):
            observations.append(
                "Целевой MP3 существовал до запуска и был заменён новым файлом."
            )

        validation = task.get("output_validation")
        if isinstance(validation, dict):
            if validation.get("duration_matches") is True:
                observations.append(
                    "Длительность итогового MP3 совпала с ожидаемой в пределах допуска."
                )
            elif validation.get("validated") is False:
                observations.append(
                    "Готовый MP3 создан, но полноценная metadata-проверка была недоступна."
                )

        return observations
