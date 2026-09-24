from __future__ import annotations

from .runtime import *
from .problem_logger import ProblemLogger
from .text_processing import *
from .sapi import *
from .ffmpeg_tools import *
from .windows_tools import *

class TaskConversionWorkerMixin:
    def _job_worker(
        self,
        run_id: str,
        text: str,
        text_metrics: dict,
        tab_title_snapshot: str,
        visible_tab_index_snapshot: int | None,
        output_path: Path,
        voice: str,
        rate: int,
        pitch: int,
        volume: int,
        bitrate: str,
        recovery_work_dir: Path | None = None,
    ) -> None:
        # CODEX-REGION: worker-bootstrap
        try:
            import pythoncom  # type: ignore
        except Exception as exc:
            self.app.events.put(
                (
                    self.task_id,
                    "job_error",
                    {
                        "user_message": (
                            "Не установлен pywin32.\n\n"
                            "Установите:\n"
                            "py -m pip install pywin32"
                        ),
                        "error_log": "",
                        "exc": exc,
                    },
                )
            )
            return

        pythoncom.CoInitialize()

        started_at = now_iso()
        started_perf = time.perf_counter()
        last_heartbeat = started_perf
        stage = "prepare"
        self.current_job_stage = stage

        work_dir: Path | None = None
        recovery_manifest_path: Path | None = None
        chunks: list[str] = []
        current_chunk: str | None = None
        retry_count = 0
        chunk_durations: list[float] = []
        chunk_timings: list[dict] = []
        chunk_char_lengths: list[int] = []
        adaptive_slow_count = 0
        ffmpeg_duration = 0.0
        ffmpeg_audio_duration = 0.0
        ffmpeg_version = ""
        first_wav_sample: dict = {}
        temp_wav_bytes = 0
        max_wav_bytes = 0
        sampled_audio_sec = 0.0
        disk_forecast: dict = {}
        output_validation: dict = {}
        resumed_existing_chunks = 0
        temp_free_before: int | None = None
        temp_free_before_ffmpeg: int | None = None
        output_free_before: int | None = None
        output_free_after: int | None = None
        preserve_recovery_on_exit = False

        text_hash_full = hashlib.sha256(
            text.encode("utf-8")
        ).hexdigest()
        text_hash = text_hash_full[:16]

        output_snapshot_before = file_snapshot(output_path)

        base_context = {
            "run_id": run_id,
            "tab_id": self.workspace_id,
            "ui_task_id": self.task_id,
            "tab_title": tab_title_snapshot,
            "workspace_id": self.workspace_id,
            "visible_tab_index": visible_tab_index_snapshot,
            "voice": voice,
            "rate": rate,
            "pitch": pitch,
            "volume": volume,
            "bitrate": bitrate,
            "chars": len(text),
            "text_sha256_16": text_hash,
            "text_sha256": text_hash_full,
            "text_metrics": text_metrics,
            "mp3_text_backup_path": (
                str(self.current_mp3_text_backup_path)
                if self.current_mp3_text_backup_path
                else ""
            ),
            "mp3_text_backup_retention_days": (
                MP3_TEXT_BACKUP_RETENTION_DAYS
            ),
            "output_path": str(output_path),
            "output_before": output_snapshot_before,
        }

        self.app.logger.task_started(
            run_id,
            base_context,
        )

        # CODEX-REGION: split-output-recovery
        try:
            stage = "split_text"
            self.current_job_stage = stage
            chunks = split_text(text)
            if not chunks:
                raise RuntimeError(
                    "После подготовки текста не осталось данных для озвучки."
                )

            chunk_char_lengths = [len(chunk) for chunk in chunks]

            stage = "prepare_output"
            self.current_job_stage = stage
            output_path.parent.mkdir(parents=True, exist_ok=True)

            output_free_before = safe_disk_free_bytes(
                output_path.parent
            )

            ffmpeg = find_ffmpeg()
            ffmpeg_version = get_ffmpeg_version(ffmpeg)

            is_recovery_resume = recovery_work_dir is not None
            if recovery_work_dir is not None:
                work_dir = Path(recovery_work_dir)
                work_dir.mkdir(parents=True, exist_ok=True)
            else:
                work_dir = RECOVERY_DIR / f"job_{run_id}"
                work_dir.mkdir(parents=True, exist_ok=False)

            recovery_manifest_path = work_dir / "recovery.json"
            recovery_source_path = work_dir / "source_text.txt"

            # Recovery data is NOT a diagnostic log. Keeping the exact normalized
            # source snapshot here makes crash recovery safe even if the user
            # edits/deletes the original tab before the next launch.
            if not recovery_source_path.exists():
                atomic_write_text(
                    recovery_source_path,
                    text,
                )

            def write_recovery_state(
                state: str,
                completed_chunks: int,
            ) -> None:
                if recovery_manifest_path is None:
                    return
                payload = {
                    "schema": 1,
                    "run_id": run_id,
                    "owner_pid": os.getpid(),
                    "tab_id": self.workspace_id,
                    "workspace_id": self.workspace_id,
                    "tab_title": tab_title_snapshot,
                    "created_at": started_at,
                    "updated_at": now_iso(),
                    "state": state,
                    "text_sha256": text_hash_full,
                    "text_sha256_16": text_hash,
                    "source_text_file": (
                        recovery_source_path.name
                    ),
                    "chars": len(text),
                    "chunks_total": len(chunks),
                    "completed_chunks": completed_chunks,
                    "output_path": str(output_path),
                    "voice": voice,
                    "rate": rate,
                    "pitch": pitch,
                    "volume": volume,
                    "bitrate": bitrate,
                    "temp_wav_bytes": temp_wav_bytes,
                    "sampled_audio_sec": round(
                        sampled_audio_sec,
                        3,
                    ),
                }
                atomic_write_json(recovery_manifest_path, payload)

            if is_recovery_resume:
                (
                    resumed_existing_chunks,
                    temp_wav_bytes,
                    sampled_audio_sec,
                    first_wav_sample,
                ) = count_valid_recovery_wavs(
                    work_dir,
                    len(chunks),
                )
                wav_files = [
                    work_dir / f"chunk_{index:05d}.wav"
                    for index in range(
                        1,
                        resumed_existing_chunks + 1,
                    )
                ]
                max_wav_bytes = max(
                    (
                        path.stat().st_size
                        for path in wav_files
                        if path.exists()
                    ),
                    default=0,
                )
                self.app.logger.event(
                    "recovery_job_resumed",
                    task_id=run_id,
                    run_id=run_id,
                    tab_id=self.workspace_id,
                    recovered_chunks=resumed_existing_chunks,
                    chunks_total=len(chunks),
                    work_dir=str(work_dir),
                )
                self.app.events.put(
                    (
                        self.task_id,
                        "progress",
                        {
                            "pct": (
                                resumed_existing_chunks
                                / len(chunks)
                                * 92.0
                            ),
                            "text": (
                                "Восстановление: найдено "
                                f"{resumed_existing_chunks}/{len(chunks)} "
                                "готовых фрагментов."
                            ),
                        },
                    )
                )
            else:
                wav_files: list[Path] = []

            write_recovery_state(
                "sapi",
                resumed_existing_chunks,
            )

            temp_free_before = safe_disk_free_bytes(work_dir)

            self.app.logger.task_progress(
                run_id,
                stage="prepared",
                chunks_total=len(chunks),
                overall_progress_percent=0.0,
                elapsed_sec=round(
                    time.perf_counter() - started_perf,
                    3,
                ),
                ffmpeg_path=ffmpeg,
                ffmpeg_version=ffmpeg_version,
                temp_dir=str(work_dir),
                temp_drive_free_bytes=temp_free_before,
                output_drive_free_bytes=output_free_before,
                chunk_chars_min=min(chunk_char_lengths),
                chunk_chars_avg=round(
                    sum(chunk_char_lengths)
                    / len(chunk_char_lengths),
                    2,
                ),
                chunk_chars_max=max(chunk_char_lengths),
                resumed_from_recovery=is_recovery_resume,
                resumed_existing_chunks=resumed_existing_chunks,
                output_existed_before=output_snapshot_before.get("exists"),
                output_size_before_bytes=output_snapshot_before.get("size_bytes"),
            )

            for label, free_bytes in (
                ("temp", temp_free_before),
                ("output", output_free_before),
            ):
                if (
                    free_bytes is not None
                    and free_bytes < LOW_DISK_WARNING_BYTES
                ):
                    self.app.logger.event(
                        "low_disk_warning",
                        task_id=run_id,
                        run_id=run_id,
                        tab_id=self.workspace_id,
                        stage="prepare_output",
                        drive_role=label,
                        free_bytes=free_bytes,
                    )

            # CODEX-REGION: sapi-generation
            sapi_started_perf = time.perf_counter()
            for index in range(
                resumed_existing_chunks + 1,
                len(chunks) + 1,
            ):
                chunk = chunks[index - 1]
                current_chunk = chunk

                if self.cancel_event.is_set():
                    raise InterruptedError(
                        "Операция остановлена пользователем."
                    )

                stage = f"sapi_chunk_{index}"
                self.current_job_stage = "sapi"
                wav_path = work_dir / f"chunk_{index:05d}.wav"
                chunk_started = time.perf_counter()
                success = False
                last_error = ""
                retry_before_chunk = retry_count

                for attempt in range(1, MAX_RETRIES + 1):
                    if self.cancel_event.is_set():
                        raise InterruptedError(
                            "Операция остановлена пользователем."
                        )

                    try:
                        wav_path.unlink(missing_ok=True)

                        synthesize_wav_once(
                            text=chunk,
                            wav_path=wav_path,
                            voice_description=voice,
                            rate=rate,
                            pitch=pitch,
                            volume=volume,
                        )

                        if (
                            not wav_path.exists()
                            or wav_path.stat().st_size < 128
                        ):
                            raise RuntimeError(
                                "SAPI не создал корректный WAV-файл."
                            )

                        generated_wav_info = get_wav_info(
                            wav_path
                        )
                        if (
                            not generated_wav_info
                            or not generated_wav_info.get(
                                "appears_complete",
                                False,
                            )
                        ):
                            raise RuntimeError(
                                "SAPI создал неполный или повреждённый WAV-файл."
                            )

                        success = True
                        break

                    except Exception as exc:
                        last_error = f"{type(exc).__name__}: {exc}"
                        retry_count += 1

                        self.app.logger.event(
                            "sapi_retry",
                            task_id=run_id,
                            run_id=run_id,
                            tab_id=self.workspace_id,
                            chunk=index,
                            chunks=len(chunks),
                            attempt=attempt,
                            chunk_chars=len(chunk),
                            error=last_error[:800],
                        )

                        if attempt < MAX_RETRIES:
                            time.sleep(0.8 * attempt)

                elapsed = time.perf_counter() - chunk_started

                if not success:
                    raise RuntimeError(
                        f"Не удалось озвучить фрагмент {index} из {len(chunks)} "
                        f"после {MAX_RETRIES} попыток. {last_error}"
                    )

                chunk_durations.append(elapsed)
                chunk_retry_count = retry_count - retry_before_chunk
                chunk_timings.append(
                    {
                        "chunk": index,
                        "duration_sec": round(elapsed, 3),
                        "chars": len(chunk),
                        "retry_count": chunk_retry_count,
                    }
                )

                previous = chunk_durations[:-1]
                baseline_median = (
                    statistics.median(previous)
                    if len(previous) >= 5
                    else 0.0
                )
                relative_threshold = (
                    baseline_median
                    * ADAPTIVE_SLOW_MULTIPLIER
                    if baseline_median > 0
                    else None
                )
                is_slow_absolute = (
                    elapsed >= ADAPTIVE_SLOW_ABSOLUTE_SEC
                )
                is_slow_relative = bool(
                    relative_threshold is not None
                    and elapsed >= relative_threshold
                )
                if is_slow_absolute or is_slow_relative:
                    adaptive_slow_count += 1
                    self.app.logger.event(
                        "slow_chunk",
                        task_id=run_id,
                        run_id=run_id,
                        tab_id=self.workspace_id,
                        chunk=index,
                        chunks=len(chunks),
                        duration_sec=round(elapsed, 3),
                        chunk_chars=len(chunk),
                        absolute_threshold_sec=(
                            ADAPTIVE_SLOW_ABSOLUTE_SEC
                        ),
                        relative_threshold_sec=(
                            round(relative_threshold, 3)
                            if relative_threshold is not None
                            else None
                        ),
                        triggered_by_absolute=(
                            is_slow_absolute
                        ),
                        triggered_by_relative=(
                            is_slow_relative
                        ),
                        previous_median_sec=round(
                            baseline_median,
                            3,
                        ),
                    )

                wav_size = wav_path.stat().st_size
                temp_wav_bytes += wav_size
                max_wav_bytes = max(max_wav_bytes, wav_size)

                wav_info = get_wav_info(wav_path)
                sampled_audio_sec += float(
                    wav_info.get("duration_sec") or 0
                )

                if not first_wav_sample:
                    first_wav_sample = {
                        "sample_chunk_index": index,
                        **wav_info,
                    }
                    self.app.logger.event(
                        "wav_sample_format",
                        task_id=run_id,
                        run_id=run_id,
                        tab_id=self.workspace_id,
                        **first_wav_sample,
                    )

                wav_files.append(wav_path)

                if index % 5 == 0 or index == len(chunks):
                    write_recovery_state("sapi", index)

                generated_count = max(
                    1,
                    index - resumed_existing_chunks,
                )
                sapi_elapsed_now = max(
                    0.001,
                    time.perf_counter() - sapi_started_perf,
                )
                remaining_chunks = max(0, len(chunks) - index)
                eta_sec = (
                    sapi_elapsed_now
                    / generated_count
                    * remaining_chunks
                )

                sapi_pct = (index / len(chunks)) * 92.0
                eta_text = format_eta(eta_sec)
                status_text = (
                    f"Озвучивание: {index}/{len(chunks)} "
                    f"({len(chunk)} символов)"
                )
                if eta_text and remaining_chunks > 0:
                    status_text += f" — осталось {eta_text}"

                self.app.events.put(
                    (
                        self.task_id,
                        "progress",
                        {
                            "pct": sapi_pct,
                            "text": status_text,
                        },
                    )
                )

                forecast_at = min(
                    DISK_FORECAST_SAMPLE_CHUNKS,
                    len(chunks),
                )
                if (
                    not disk_forecast
                    and index >= forecast_at
                    and sampled_audio_sec > 0
                ):
                    disk_forecast = estimate_disk_forecast(
                        processed_chunks=index,
                        total_chunks=len(chunks),
                        temp_wav_bytes=temp_wav_bytes,
                        sampled_audio_sec=sampled_audio_sec,
                        bitrate=bitrate,
                    )
                    free_temp = safe_disk_free_bytes(work_dir)
                    free_output = safe_disk_free_bytes(
                        output_path.parent
                    )
                    same_volume = same_storage_volume(
                        work_dir,
                        output_path.parent,
                    )

                    estimated_temp = int(
                        disk_forecast.get(
                            "estimated_temp_wav_bytes",
                            0,
                        )
                    )
                    estimated_mp3 = int(
                        disk_forecast.get(
                            "estimated_mp3_bytes",
                            0,
                        )
                    )
                    remaining_temp = max(
                        0,
                        estimated_temp - temp_wav_bytes,
                    )

                    if same_volume:
                        required_temp = int(
                            (
                                remaining_temp
                                + estimated_mp3
                            )
                            * (1.0 + DISK_FORECAST_RESERVE_RATIO)
                        )
                        required_output = required_temp
                        insufficient = (
                            free_temp is not None
                            and free_temp < required_temp
                        )
                    else:
                        required_temp = int(
                            remaining_temp
                            * (1.0 + DISK_FORECAST_RESERVE_RATIO)
                        )
                        required_output = int(
                            estimated_mp3
                            * (1.0 + DISK_FORECAST_RESERVE_RATIO)
                        )
                        insufficient = bool(
                            (
                                free_temp is not None
                                and free_temp < required_temp
                            )
                            or (
                                free_output is not None
                                and free_output < required_output
                            )
                        )

                    disk_forecast.update(
                        {
                            "same_volume": same_volume,
                            "temp_free_bytes": free_temp,
                            "output_free_bytes": free_output,
                            "required_temp_free_bytes": required_temp,
                            "required_output_free_bytes": required_output,
                            "insufficient_space": insufficient,
                        }
                    )

                    self.app.logger.event(
                        "disk_space_forecast",
                        task_id=run_id,
                        run_id=run_id,
                        tab_id=self.workspace_id,
                        **disk_forecast,
                    )

                    if insufficient:
                        self.app.logger.event(
                            "low_disk_warning",
                            task_id=run_id,
                            run_id=run_id,
                            tab_id=self.workspace_id,
                            stage="disk_forecast",
                            **disk_forecast,
                        )

                        decision_event = threading.Event()
                        decision: dict = {}
                        self.app.events.put(
                            (
                                self.task_id,
                                "disk_space_warning",
                                {
                                    "run_id": run_id,
                                    "forecast": dict(
                                        disk_forecast
                                    ),
                                    "decision_event": decision_event,
                                    "decision": decision,
                                },
                            )
                        )

                        while not decision_event.wait(0.1):
                            if self.cancel_event.is_set():
                                break

                        if (
                            self.cancel_event.is_set()
                            or not decision.get(
                                "continue",
                                False,
                            )
                        ):
                            raise InterruptedError(
                                "Создание MP3 остановлено из-за "
                                "недостатка свободного места."
                            )

                now_perf = time.perf_counter()
                if (
                    now_perf - last_heartbeat
                    >= TASK_HEARTBEAT_INTERVAL_SEC
                ):
                    free_now = safe_disk_free_bytes(work_dir)
                    self.app.logger.task_progress(
                        run_id,
                        stage="sapi",
                        chunk=index,
                        chunks_total=len(chunks),
                        overall_progress_percent=round(sapi_pct, 2),
                        elapsed_sec=round(
                            now_perf - started_perf,
                            3,
                        ),
                        eta_sec=round(eta_sec, 1),
                        retry_count=retry_count,
                        temp_wav_bytes=temp_wav_bytes,
                        max_wav_bytes=max_wav_bytes,
                        temp_drive_free_bytes=free_now,
                        disk_forecast=(
                            dict(disk_forecast)
                            if disk_forecast
                            else None
                        ),
                    )
                    write_recovery_state("sapi", index)
                    last_heartbeat = now_perf

                    if (
                        free_now is not None
                        and free_now < LOW_DISK_WARNING_BYTES
                    ):
                        self.app.logger.event(
                            "low_disk_warning",
                            task_id=run_id,
                            run_id=run_id,
                            tab_id=self.workspace_id,
                            stage="sapi",
                            free_bytes=free_now,
                            chunk=index,
                        )


            if self.cancel_event.is_set():
                raise InterruptedError(
                    "Операция остановлена пользователем."
                )

            # CODEX-REGION: ffmpeg-assembly
            stage = "ffmpeg"
            self.current_job_stage = stage
            temp_free_before_ffmpeg = safe_disk_free_bytes(
                work_dir
            )
            write_recovery_state("ffmpeg", len(chunks))

            if not disk_forecast and sampled_audio_sec > 0:
                disk_forecast = estimate_disk_forecast(
                    processed_chunks=len(chunks),
                    total_chunks=len(chunks),
                    temp_wav_bytes=temp_wav_bytes,
                    sampled_audio_sec=sampled_audio_sec,
                    bitrate=bitrate,
                )
                same_volume = same_storage_volume(
                    work_dir,
                    output_path.parent,
                )
                free_output_now = safe_disk_free_bytes(
                    output_path.parent
                )
                estimated_mp3 = int(
                    disk_forecast.get(
                        "estimated_mp3_bytes",
                        0,
                    )
                )
                required_output = int(
                    estimated_mp3
                    * (1.0 + DISK_FORECAST_RESERVE_RATIO)
                )
                insufficient = bool(
                    free_output_now is not None
                    and free_output_now < required_output
                )
                disk_forecast.update(
                    {
                        "same_volume": same_volume,
                        "temp_free_bytes": (
                            temp_free_before_ffmpeg
                        ),
                        "output_free_bytes": free_output_now,
                        "required_output_free_bytes": (
                            required_output
                        ),
                        "insufficient_space": insufficient,
                    }
                )
                self.app.logger.event(
                    "disk_space_forecast",
                    task_id=run_id,
                    run_id=run_id,
                    tab_id=self.workspace_id,
                    **disk_forecast,
                )

                if insufficient:
                    self.app.logger.event(
                        "low_disk_warning",
                        task_id=run_id,
                        run_id=run_id,
                        tab_id=self.workspace_id,
                        stage="ffmpeg_preflight",
                        **disk_forecast,
                    )
                    decision_event = threading.Event()
                    decision: dict = {}
                    self.app.events.put(
                        (
                            self.task_id,
                            "disk_space_warning",
                            {
                                "run_id": run_id,
                                "forecast": dict(
                                    disk_forecast
                                ),
                                "decision_event": decision_event,
                                "decision": decision,
                            },
                        )
                    )
                    while not decision_event.wait(0.1):
                        if self.cancel_event.is_set():
                            break
                    if (
                        self.cancel_event.is_set()
                        or not decision.get(
                            "continue",
                            False,
                        )
                    ):
                        raise InterruptedError(
                            "Создание MP3 остановлено из-за "
                            "недостатка места для итогового файла."
                        )

            self.app.logger.task_progress(
                run_id,
                stage="ffmpeg_start",
                chunks_total=len(chunks),
                overall_progress_percent=94.0,
                elapsed_sec=round(
                    time.perf_counter() - started_perf,
                    3,
                ),
                retry_count=retry_count,
                temp_wav_bytes=temp_wav_bytes,
                max_wav_bytes=max_wav_bytes,
                temp_drive_free_bytes=temp_free_before_ffmpeg,
                disk_forecast=(
                    dict(disk_forecast)
                    if disk_forecast
                    else None
                ),
            )

            self.app.events.put(
                (
                    self.task_id,
                    "progress",
                    {
                        "pct": 94.0,
                        "text": "Кодирую итоговый MP3…",
                    },
                )
            )

            last_ffmpeg_heartbeat = [time.perf_counter()]

            def report_ffmpeg_progress(info: dict) -> None:
                fraction = info.get("fraction")
                ffmpeg_eta_sec = None
                if isinstance(fraction, (int, float)):
                    fraction_value = float(fraction)
                    ui_pct = 94.0 + fraction_value * 5.5
                    ffmpeg_percent = round(
                        fraction_value * 100.0,
                        1,
                    )
                    ffmpeg_elapsed = float(
                        info.get("elapsed_sec") or 0
                    )
                    if (
                        fraction_value > 0.005
                        and fraction_value < 1.0
                    ):
                        ffmpeg_eta_sec = (
                            ffmpeg_elapsed
                            * (1.0 - fraction_value)
                            / fraction_value
                        )
                    status = (
                        f"Кодирование MP3: {ffmpeg_percent:.1f}% "
                        f"— прошло {ffmpeg_elapsed:.1f} сек"
                    )
                    eta_text = format_eta(ffmpeg_eta_sec)
                    if eta_text:
                        status += f" — осталось {eta_text}"
                else:
                    ui_pct = 94.0
                    ffmpeg_percent = None
                    status = (
                        "Кодирование MP3… "
                        f"прошло {info.get('elapsed_sec', 0):.1f} сек"
                    )

                self.app.events.put(
                    (
                        self.task_id,
                        "progress",
                        {
                            "pct": min(99.5, ui_pct),
                            "text": status,
                        },
                    )
                )

                now_perf = time.perf_counter()
                if (
                    now_perf - last_ffmpeg_heartbeat[0]
                    >= TASK_HEARTBEAT_INTERVAL_SEC
                ):
                    self.app.logger.task_progress(
                        run_id,
                        stage="ffmpeg",
                        overall_progress_percent=round(
                            min(99.5, ui_pct),
                            2,
                        ),
                        ffmpeg_progress_percent=ffmpeg_percent,
                        elapsed_sec=round(
                            now_perf - started_perf,
                            3,
                        ),
                        ffmpeg_elapsed_sec=info.get(
                            "elapsed_sec"
                        ),
                        ffmpeg_eta_sec=(
                            round(ffmpeg_eta_sec, 1)
                            if ffmpeg_eta_sec is not None
                            else None
                        ),
                        ffmpeg_out_time_sec=info.get(
                            "out_time_sec"
                        ),
                        ffmpeg_audio_total_sec=info.get(
                            "total_audio_sec"
                        ),
                        output_part_bytes=info.get(
                            "output_part_bytes"
                        ),
                        temp_wav_bytes=temp_wav_bytes,
                    )
                    last_ffmpeg_heartbeat[0] = now_perf

            ffmpeg_result = run_ffmpeg_concat(
                ffmpeg=ffmpeg,
                wav_files=wav_files,
                output_mp3=output_path,
                bitrate=bitrate,
                work_dir=work_dir,
                cancel_event=self.cancel_event,
                progress_callback=report_ffmpeg_progress,
            )
            ffmpeg_duration = float(
                ffmpeg_result.get("duration_sec") or 0
            )
            ffmpeg_audio_duration = float(
                ffmpeg_result.get("audio_duration_sec") or 0
            )

            # CODEX-REGION: validate-success
            stage = "validate_output"
            self.current_job_stage = stage

            if (
                not output_path.exists()
                or output_path.stat().st_size == 0
            ):
                raise RuntimeError(
                    "Итоговый MP3 не найден после завершения обработки."
                )

            self.app.events.put(
                (
                    self.task_id,
                    "progress",
                    {
                        "pct": 99.7,
                        "text": "Проверяю готовый MP3…",
                    },
                )
            )

            output_validation = validate_final_mp3(
                ffmpeg,
                output_path,
                ffmpeg_audio_duration,
            )
            if output_validation.get("fatal_error"):
                self.app.logger.event(
                    "output_validation_failed",
                    task_id=run_id,
                    run_id=run_id,
                    tab_id=self.workspace_id,
                    output_path=str(output_path),
                    validation=output_validation,
                )
                raise RuntimeError(
                    str(output_validation["fatal_error"])
                )

            if not output_validation.get("validated"):
                self.app.logger.event(
                    "output_validation_warning",
                    task_id=run_id,
                    run_id=run_id,
                    tab_id=self.workspace_id,
                    output_path=str(output_path),
                    validation=output_validation,
                )
            else:
                self.app.logger.event(
                    "output_validated",
                    task_id=run_id,
                    run_id=run_id,
                    tab_id=self.workspace_id,
                    output_path=str(output_path),
                    validation=output_validation,
                )

            duration = time.perf_counter() - started_perf
            output_size = output_path.stat().st_size
            output_free_after = safe_disk_free_bytes(
                output_path.parent
            )
            output_snapshot_after = file_snapshot(output_path)

            timing_values = [
                float(item["duration_sec"])
                for item in chunk_timings
            ]
            top_slowest = sorted(
                chunk_timings,
                key=lambda item: float(
                    item.get("duration_sec") or 0
                ),
                reverse=True,
            )[:5]

            sapi_duration = sum(chunk_durations)
            performance = {
                "ffmpeg_share_percent": (
                    round(
                        ffmpeg_duration / duration * 100.0,
                        2,
                    )
                    if duration > 0
                    else 0
                ),
                "overall_realtime_factor": (
                    round(
                        ffmpeg_audio_duration / duration,
                        2,
                    )
                    if (
                        duration > 0
                        and ffmpeg_audio_duration > 0
                    )
                    else 0
                ),
                "sapi_realtime_factor": (
                    round(
                        ffmpeg_audio_duration
                        / sapi_duration,
                        2,
                    )
                    if (
                        sapi_duration > 0
                        and ffmpeg_audio_duration > 0
                        and not resumed_existing_chunks
                    )
                    else None
                ),
                "temp_wav_to_mp3_ratio": (
                    round(
                        temp_wav_bytes / output_size,
                        2,
                    )
                    if output_size > 0
                    else 0
                ),
            }

            summary = {
                "schema": 3,
                "task_id": run_id,
                "run_id": run_id,
                "tab_id": self.workspace_id,
                "status": "success",
                "started_at": started_at,
                "finished_at": now_iso(),
                "duration_sec": round(duration, 3),
                **base_context,
                "chunks": len(chunks),
                "resumed_existing_chunks": resumed_existing_chunks,
                "chunk_chars": {
                    "min": min(chunk_char_lengths),
                    "avg": round(
                        sum(chunk_char_lengths)
                        / len(chunk_char_lengths),
                        2,
                    ),
                    "max": max(chunk_char_lengths),
                },
                "retry_count": retry_count,
                "sapi_duration_sec": round(
                    sapi_duration,
                    3,
                ),
                "sapi_duration_scope": (
                    "generated_only"
                    if resumed_existing_chunks
                    else "all_chunks"
                ),
                "average_chunk_sec": (
                    round(
                        sum(chunk_durations)
                        / len(chunk_durations),
                        3,
                    )
                    if chunk_durations
                    else 0
                ),
                "slowest_chunk_sec": (
                    round(max(chunk_durations), 3)
                    if chunk_durations
                    else 0
                ),
                "chunk_timing_sec": {
                    "sample_count": len(timing_values),
                    "p50": round(
                        percentile(timing_values, 50),
                        3,
                    ),
                    "p95": round(
                        percentile(timing_values, 95),
                        3,
                    ),
                    "p99": round(
                        percentile(timing_values, 99),
                        3,
                    ),
                    "max": round(
                        max(timing_values)
                        if timing_values
                        else 0,
                        3,
                    ),
                    "adaptive_slow_count": (
                        adaptive_slow_count
                    ),
                    "slow_absolute_threshold_sec": (
                        ADAPTIVE_SLOW_ABSOLUTE_SEC
                    ),
                    "slow_multiplier": (
                        ADAPTIVE_SLOW_MULTIPLIER
                    ),
                    "top_5_slowest": top_slowest,
                },
                "first_wav_sample": first_wav_sample,
                "temp_wav_bytes": temp_wav_bytes,
                "max_wav_bytes": max_wav_bytes,
                "disk_forecast": disk_forecast,
                "temp_drive_free_before_bytes": temp_free_before,
                "temp_drive_free_before_ffmpeg_bytes": (
                    temp_free_before_ffmpeg
                ),
                "output_drive_free_before_bytes": output_free_before,
                "output_drive_free_after_bytes": output_free_after,
                "ffmpeg_path": ffmpeg,
                "ffmpeg_version": ffmpeg_version,
                "ffmpeg_duration_sec": round(
                    ffmpeg_duration,
                    3,
                ),
                "audio_duration_sec": round(
                    ffmpeg_audio_duration,
                    3,
                ),
                "output_validation": output_validation,
                "performance": performance,
                "output_before": output_snapshot_before,
                "output_after": output_snapshot_after,
                "output_size_bytes": output_size,
            }

            self.app.logger.task_finished(
                run_id,
                "success",
                summary,
            )

            self.app.events.put(
                (
                    self.task_id,
                    "progress",
                    {"pct": 100.0, "text": "Готово."},
                )
            )
            self.app.events.put(
                (
                    self.task_id,
                    "done",
                    {
                        "output": str(output_path),
                        "run_id": run_id,
                    },
                )
            )

        # CODEX-REGION: cancellation
        except InterruptedError as exc:
            duration = time.perf_counter() - started_perf

            summary = {
                "schema": 3,
                "task_id": run_id,
                "run_id": run_id,
                "tab_id": self.workspace_id,
                "status": "cancelled",
                "started_at": started_at,
                "finished_at": now_iso(),
                "duration_sec": round(duration, 3),
                **base_context,
                "chunks": len(chunks),
                "resumed_existing_chunks": resumed_existing_chunks,
                "retry_count": retry_count,
                "stage": stage,
                "sapi_duration_sec": round(
                    sum(chunk_durations),
                    3,
                ),
                "first_wav_sample": first_wav_sample,
                "disk_forecast": disk_forecast,
                "temp_wav_bytes": temp_wav_bytes,
                "max_wav_bytes": max_wav_bytes,
                "ffmpeg_duration_sec": round(
                    ffmpeg_duration,
                    3,
                ),
                "output_after": file_snapshot(output_path),
                "output_size_bytes": (
                    output_path.stat().st_size
                    if output_path.exists()
                    else 0
                ),
            }

            self.app.logger.task_finished(
                run_id,
                "cancelled",
                summary,
            )

            self.app.events.put(
                (
                    self.task_id,
                    "cancelled",
                    {
                        "message": str(exc),
                        "run_id": run_id,
                    },
                )
            )

        # CODEX-REGION: failure-recovery
        except Exception as exc:
            tb = traceback.format_exc()
            duration = time.perf_counter() - started_perf

            # Keep already generated WAVs after a real failure so the next
            # launch can retry from the checkpoint instead of starting over.
            if work_dir is not None and work_dir.exists():
                preserve_recovery_on_exit = True
                try:
                    if "write_recovery_state" in locals():
                        completed_for_recovery = (
                            resumed_existing_chunks
                            + len(chunk_durations)
                        )
                        write_recovery_state(
                            "failed",
                            min(
                                len(chunks),
                                completed_for_recovery,
                            ),
                        )
                except Exception:
                    pass

            context = {
                **base_context,
                "stage": stage,
                "chunks": len(chunks),
                "completed_chunks": (
                    resumed_existing_chunks
                    + len(chunk_durations)
                ),
                "retry_count": retry_count,
                "last_chunk_duration_sec": (
                    round(chunk_durations[-1], 3)
                    if chunk_durations
                    else None
                ),
                "temp_wav_bytes": temp_wav_bytes,
                "max_wav_bytes": max_wav_bytes,
                "first_wav_sample": first_wav_sample,
                "disk_forecast": disk_forecast,
                "output_validation": output_validation,
                "recovery_preserved": preserve_recovery_on_exit,
                "recovery_dir": (
                    str(work_dir)
                    if preserve_recovery_on_exit
                    and work_dir is not None
                    else ""
                ),
                "temp_drive_free_bytes": (
                    safe_disk_free_bytes(work_dir)
                    if work_dir is not None
                    else None
                ),
                "output_drive_free_bytes": safe_disk_free_bytes(
                    output_path.parent
                ),
                "ffmpeg_path": (
                    ffmpeg
                    if "ffmpeg" in locals()
                    else ""
                ),
                "ffmpeg_version": ffmpeg_version,
                "ffmpeg_error": (
                    getattr(exc, "stderr", "")[:6000]
                    if isinstance(exc, FfmpegError)
                    else ""
                ),
            }

            error_log = self.app.logger.error(
                task_id=run_id,
                stage=stage,
                exc=exc,
                traceback_text=tb,
                context=context,
                failed_chunk=(
                    current_chunk
                    if stage.startswith("sapi_chunk_")
                    else None
                ),
            )

            summary = {
                "schema": 3,
                "task_id": run_id,
                "run_id": run_id,
                "tab_id": self.workspace_id,
                "status": "failed",
                "started_at": started_at,
                "finished_at": now_iso(),
                "duration_sec": round(duration, 3),
                **base_context,
                "chunks": len(chunks),
                "resumed_existing_chunks": resumed_existing_chunks,
                "retry_count": retry_count,
                "stage": stage,
                "sapi_duration_sec": round(
                    sum(chunk_durations),
                    3,
                ),
                "first_wav_sample": first_wav_sample,
                "disk_forecast": disk_forecast,
                "output_validation": output_validation,
                "recovery_preserved": preserve_recovery_on_exit,
                "recovery_dir": (
                    str(work_dir)
                    if preserve_recovery_on_exit
                    and work_dir is not None
                    else ""
                ),
                "error_type": type(exc).__name__,
                "error": str(exc),
                "error_log": error_log,
                "temp_wav_bytes": temp_wav_bytes,
                "max_wav_bytes": max_wav_bytes,
                "output_after": file_snapshot(output_path),
                "output_size_bytes": (
                    output_path.stat().st_size
                    if output_path.exists()
                    else 0
                ),
            }

            self.app.logger.task_finished(
                run_id,
                "failed",
                summary,
            )

            self.app.events.put(
                (
                    self.task_id,
                    "job_error",
                    {
                        "user_message": str(exc),
                        "error_log": error_log,
                        "run_id": run_id,
                        "recovery_preserved": (
                            preserve_recovery_on_exit
                        ),
                        "recovery_dir": (
                            str(work_dir)
                            if preserve_recovery_on_exit
                            and work_dir is not None
                            else ""
                        ),
                        "exc": exc,
                    },
                )
            )

        # CODEX-REGION: final-cleanup
        finally:
            self.current_job_stage = "cleanup"
            if (
                work_dir is not None
                and not preserve_recovery_on_exit
            ):
                shutil.rmtree(work_dir, ignore_errors=True)
            self.current_job_stage = "idle"
            pythoncom.CoUninitialize()
