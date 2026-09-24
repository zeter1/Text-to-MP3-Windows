from __future__ import annotations

from .runtime import *
from .problem_logger_compact import (
    classify_error,
    compact_event_fields,
    compact_log_value,
    compact_signal_record,
    compact_traceback_text,
    extract_traceback_locations,
    make_error_fingerprint,
    write_log_json,
)

class ProblemLoggerEventsMixin:
    def _rotate_events_if_needed(self, incoming_bytes: int) -> None:
        current_size = (
            self.events_file.stat().st_size
            if self.events_file.exists()
            else 0
        )

        if current_size <= 0:
            return
        if current_size + incoming_bytes <= EVENTS_MAX_BYTES:
            return

        self.event_file_index += 1
        self.events_file = (
            self.session_dir
            / f"events_{self.event_file_index:03d}.jsonl"
        )
        self.event_files.append(self.events_file.name)

    def _update_stats(self, event_type: str, fields: dict) -> None:
        with self.state_lock:
            if event_type in {"error", "error_repeat"}:
                self.stats["errors"] += 1
            elif event_type == "sapi_retry":
                self.stats["sapi_retries"] += 1
            elif event_type == "slow_chunk":
                self.stats["slow_chunks"] += 1
            elif event_type == "task_progress":
                self.stats["task_progress_events"] += 1
            elif event_type == "preview_started":
                self.stats["preview_started"] += 1
            elif event_type == "preview_pause_requested":
                self.stats["preview_pause_requests"] += 1
            elif event_type == "preview_resume_requested":
                self.stats["preview_resume_requests"] += 1
            elif event_type == "preview_progress":
                self.stats["preview_progress_events"] += 1
            elif event_type == "low_disk_warning":
                self.stats["low_disk_warnings"] += 1
            elif event_type == "forced_shutdown":
                self.stats["forced_shutdowns"] += 1
            elif event_type == "output_validation_warning":
                self.stats["output_validation_warnings"] += 1
            elif event_type == "recovery_jobs_found":
                self.stats["recovery_jobs_found"] += int(
                    fields.get("count") or 0
                )
            elif event_type == "recovery_job_resumed":
                self.stats["recovery_jobs_resumed"] += 1
            elif event_type == "recovery_job_discarded":
                self.stats["recovery_jobs_discarded"] += 1
            elif event_type == "global_copy_text_captured":
                self.stats["global_copy_captures"] += 1
                self.stats["global_copy_captured_chars"] += int(
                    fields.get("chars") or 0
                )
                latency = float(fields.get("copy_latency_ms") or 0)
                self.stats["global_copy_latency_ms_sum"] += latency
                self.stats["global_copy_latency_ms_max"] = max(
                    float(self.stats["global_copy_latency_ms_max"]),
                    latency,
                )
                source = str(
                    fields.get("source_process") or "unknown"
                ).strip() or "unknown"
                self.global_copy_source_counts[source] = (
                    self.global_copy_source_counts.get(
                        source,
                        0,
                    )
                    + 1
                )
            elif event_type in {
                "global_copy_clipboard_timeout",
                "global_copy_clipboard_read_failed",
            }:
                self.stats["global_copy_failures"] += 1
            elif event_type == "global_copy_duplicate_skipped":
                self.stats["global_copy_duplicates_skipped"] += 1
            elif event_type == "preview_read_text_deleted_batch":
                self.stats["read_deleted_batches"] += 1
                self.stats["read_deleted_segments"] += int(
                    fields.get("deleted_segments") or 0
                )
                self.stats["read_deleted_chars"] += int(
                    fields.get("deleted_chars") or 0
                )
            elif event_type == "preview_appended_tail_continued":
                self.stats["appended_tail_continuations"] += 1
            elif event_type == "text_state":
                self.stats["text_state_events"] += 1
                tab_id = str(
                    fields.get("tab_id") or ""
                )
                if tab_id:
                    self.latest_text_state_by_tab[tab_id] = {
                        "ts": now_iso(),
                        **dict(fields),
                    }
            elif event_type == "workspace_save_slow":
                self.stats["workspace_slow_saves"] += 1
            elif event_type == "preview_finished":
                requested = str(
                    fields.get("requested_audio_output") or ""
                )
                actual = str(
                    fields.get("actual_audio_output") or ""
                )
                if (
                    requested
                    and actual
                    and requested != DEFAULT_AUDIO_OUTPUT_LABEL
                    and requested != actual
                ):
                    self.stats["audio_output_mismatches"] += 1

                finish_reason = str(
                    fields.get("finish_reason") or "other"
                )
                finish_key = {
                    "completed": "preview_finish_completed",
                    "user_stop": "preview_finish_user_stop",
                    "resume_rewind": "preview_finish_resume_rewind",
                    "another_tab": "preview_finish_another_tab",
                    "app_exit": "preview_finish_app_exit",
                }.get(
                    finish_reason,
                    "preview_finish_other",
                )
                self.stats[finish_key] += 1

    def _event_dedup_window(self, event_type: str) -> float:
        if event_type == "text_state":
            return EVENT_DEDUP_TEXT_STATE_SEC
        if event_type in {
            "audio_outputs_refresh_requested",
            "audio_outputs_loaded",
            "voices_loaded",
            "tab_copy_hotkey_applied",
            "workspace_save_slow",
        }:
            return 10.0
        return EVENT_DEDUP_DEFAULT_SEC

    def _is_high_value_event(self, event_type: str) -> bool:
        return (
            event_type in {
                "error",
                "error_repeat",
                "app_start",
                "app_exit",
                "previous_session_unclean_shutdown",
                "forced_shutdown",
                "low_disk_warning",
                "output_validation_failed",
                "output_validation_warning",
                "sapi_retry",
                "recovery_job_resumed",
                "recovery_job_deferred",
                "recovery_jobs_found",
                "global_copy_clipboard_timeout",
                "global_copy_clipboard_read_failed",
                "workspace_tab_file_delete_failed",
                "workspace_save_lock_recovered",
            }
            or event_type.startswith("task_")
        )

    def _remember_signal(self, record: dict) -> None:
        event_type = str(record.get("event") or "")
        if not (
            self._is_high_value_event(event_type)
            or event_type in {
                "preview_started",
                "preview_finished",
                "preview_pause_requested",
                "preview_resume_requested",
                "preview_stop_requested",
                "audio_output_preference_rebound",
                "disk_space_warning_decision",
            }
        ):
            return
        with self.state_lock:
            self.recent_signal_events.append(compact_signal_record(record))
            self.recent_signal_events = self.recent_signal_events[-RECENT_SIGNAL_EVENTS_LIMIT:]

    def _suppress_event(self, event_type: str, compact_fields: dict) -> bool:
        if self._is_high_value_event(event_type):
            return False

        now_perf = time.perf_counter()
        signature_fields = dict(compact_fields)
        event_key = event_type
        dedup_window = self._event_dedup_window(event_type)

        if event_type == "text_state":
            # Full text-state snapshots are kept in diagnostic_summary/AI_READ_FIRST.
            # The timeline only needs occasional checkpoints or meaningful flag
            # transitions; otherwise text_state dominates log size on long reads.
            event_key = f"text_state:{compact_fields.get('tab_id', '')}"
            signature_fields = {
                key: compact_fields.get(key)
                for key in (
                    "tab_id",
                    "delete_read_text",
                    "preview_running",
                    "paused",
                )
            }
            dedup_window = EVENT_DEDUP_TEXT_STATE_SEC

        signature_payload = json.dumps(
            {"event": event_type, **signature_fields},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        signature = hashlib.sha256(
            signature_payload.encode("utf-8", errors="replace")
        ).hexdigest()[:16]
        with self.state_lock:
            previous = self.last_event_write.get(event_key)
            if previous and previous[0] == signature:
                if now_perf - previous[1] < dedup_window:
                    self.suppressed_event_counts[event_type] = (
                        self.suppressed_event_counts.get(event_type, 0) + 1
                    )
                    self.stats["events_suppressed"] += 1
                    return True

            if self.event_bytes_written >= EVENTS_SESSION_SOFT_LIMIT_BYTES:
                self.suppressed_event_counts[event_type] = (
                    self.suppressed_event_counts.get(event_type, 0) + 1
                )
                self.stats["events_suppressed"] += 1
                return True

            self.last_event_write[event_key] = (signature, now_perf)
            return False

    def event(self, event_type: str, **fields) -> None:
        if self.disabled:
            return

        self._update_stats(event_type, fields)
        compact_fields = compact_event_fields(event_type, fields)
        record = {"ts": now_iso(), "event": event_type, **compact_fields}
        self._remember_signal(record)

        if self._suppress_event(event_type, compact_fields):
            return

        try:
            line = json.dumps(
                record,
                ensure_ascii=False,
                separators=(",", ":"),
            ) + "\n"
            encoded_size = len(line.encode("utf-8"))

            with self.lock:
                self._rotate_events_if_needed(encoded_size)
                with self.events_file.open(
                    "a",
                    encoding="utf-8",
                ) as file:
                    file.write(line)

            with self.state_lock:
                self.event_bytes_written += encoded_size
                self.stats["events_written"] += 1
                self.stats["event_bytes_written"] = self.event_bytes_written

            if event_type in {
                "global_copy_text_captured",
                "global_copy_clipboard_timeout",
                "global_copy_clipboard_read_failed",
                "global_copy_duplicate_skipped",
                "preview_read_text_deleted_batch",
                "preview_appended_tail_continued",
                "preview_pause_requested",
                "preview_resume_requested",
                "preview_finished",
                "text_state",
                "workspace_save_slow",
            }:
                now_perf = time.perf_counter()
                if (
                    now_perf - self.last_live_summary_perf
                    >= LIVE_DIAGNOSTIC_SUMMARY_INTERVAL_SEC
                ):
                    self.last_live_summary_perf = now_perf
                    try:
                        self._write_diagnostic_summary("running")
                    except Exception:
                        pass
        except Exception:
            self.disabled = True

    def task_started(self, task_id: str, payload: dict) -> None:
        with self.state_lock:
            self.tasks_started += 1
            self.active_tasks[task_id] = {
                "started_at": now_iso(),
                "context": dict(payload),
                "last_progress": None,
            }

        self.event("task_started", task_id=task_id, **payload)

        try:
            self._write_session("running")
            self._write_diagnostic_summary("running")
        except Exception:
            pass

    def task_progress(self, task_id: str, **fields) -> None:
        progress = {
            "ts": now_iso(),
            **fields,
        }

        with self.state_lock:
            task_info = self.active_tasks.setdefault(
                task_id,
                {
                    "started_at": now_iso(),
                    "context": {},
                    "last_progress": None,
                },
            )
            task_info["last_progress"] = progress

        self.event(
            "task_progress",
            task_id=task_id,
            run_id=task_id,
            **fields,
        )

        try:
            self._write_session("running")
            self._write_diagnostic_summary("running")
        except Exception:
            pass

    def task_finished(
        self,
        task_id: str,
        status: str,
        summary: dict,
    ) -> None:
        with self.state_lock:
            if status == "success":
                self.tasks_success += 1
            elif status == "failed":
                self.tasks_failed += 1
            elif status == "cancelled":
                self.tasks_cancelled += 1
            self.active_tasks.pop(task_id, None)

            compact = self._compact_task_summary(summary)
            self.completed_tasks.append(compact)
            self.completed_tasks = self.completed_tasks[-10:]
            if status == "success":
                self.last_successful_task = compact

        try:
            task_payload = (
                summary
                if status == "failed"
                else {
                    "schema": 3,
                    "task_id": task_id,
                    **self._compact_task_summary(summary),
                }
            )
            write_log_json(
                self.session_dir / f"task_{task_id}.json",
                compact_log_value(task_payload, key="task_summary"),
            )
        except Exception:
            pass

        self.event(
            f"task_{status}",
            task_id=task_id,
            run_id=task_id,
            tab_id=summary.get("tab_id"),
            duration_sec=summary.get("duration_sec"),
            chunks=summary.get("chunks"),
            retries=summary.get("retry_count"),
            output_size_bytes=summary.get("output_size_bytes"),
            stage=summary.get("stage"),
        )
        try:
            self._write_session("running")
            self._write_diagnostic_summary("running")
        except Exception:
            pass

    def error(
        self,
        *,
        task_id: str,
        stage: str,
        exc: BaseException,
        traceback_text: str,
        context: dict,
        failed_chunk: str | None = None,
    ) -> str:
        if self.disabled:
            return ""

        try:
            message = str(exc)
            fingerprint = make_error_fingerprint(stage, exc, message)
            classification = classify_error(stage, exc, message)
            compact_context = compact_log_value(context, key="context")
            compact_traceback = compact_traceback_text(
                traceback_text,
                max_lines=ERROR_TRACEBACK_MAX_LINES,
                max_chars=ERROR_TRACEBACK_MAX_CHARS,
            )
            code_locations = extract_traceback_locations(traceback_text)
            existing = self.error_records.get(fingerprint)

            if existing:
                existing["occurrences"] = int(existing.get("occurrences") or 1) + 1
                existing["last_seen"] = now_iso()
                existing["last_context"] = compact_context
                json_path = self.errors_dir / f"{existing['error_id']}.json"
                write_log_json(json_path, existing)

                for summary in self.error_summaries:
                    if summary.get("fingerprint") == fingerprint:
                        summary["occurrences"] = existing["occurrences"]
                        summary["last_seen"] = existing["last_seen"]
                        break

                self.event(
                    "error_repeat",
                    task_id=task_id,
                    run_id=context.get("run_id"),
                    tab_id=context.get("tab_id"),
                    preview_run_id=context.get("preview_run_id"),
                    stage=stage,
                    error_id=existing["error_id"],
                    error_code=classification["error_code"],
                    exception_type=type(exc).__name__,
                    occurrences=existing["occurrences"],
                    message=message[:500],
                )
                self._write_diagnostic_summary("running")
                return str(json_path)

            error_id = (
                datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                + "_"
                + uuid.uuid4().hex[:6]
            )
            json_path = self.errors_dir / f"{error_id}.json"
            ts = now_iso()
            payload = {
                "schema": 3,
                "ts": ts,
                "first_seen": ts,
                "last_seen": ts,
                "occurrences": 1,
                "error_id": error_id,
                "fingerprint": fingerprint,
                "error_code": classification["error_code"],
                "severity": classification["severity"],
                "likely_cause": classification["likely_cause"],
                "owner_module": classification["owner_module"],
                "code_locations": code_locations,
                "task_id": task_id,
                "run_id": context.get("run_id"),
                "tab_id": context.get("tab_id"),
                "preview_run_id": context.get("preview_run_id"),
                "stage": stage,
                "exception_type": type(exc).__name__,
                "message": compact_log_value(message, key="message"),
                "traceback": compact_traceback,
                "context": compact_context,
                "suggested_checks": classification["suggested_checks"],
            }

            if failed_chunk:
                failed_text = str(failed_chunk)
                failed_path = self.errors_dir / f"{error_id}_failed_chunk.txt"
                excerpt = failed_text[: min(DEFAULT_CHUNK_SIZE + 200, 2800)]
                failed_path.write_text(excerpt, encoding="utf-8")
                payload["failed_chunk"] = {
                    "file": failed_path.name,
                    "chars": len(failed_text),
                    "saved_chars": len(excerpt),
                    "sha256_16": hashlib.sha256(
                        failed_text.encode("utf-8", errors="replace")
                    ).hexdigest()[:16],
                }

            write_log_json(json_path, payload)
            self.error_records[fingerprint] = payload
            summary = {
                "ts": ts,
                "last_seen": ts,
                "occurrences": 1,
                "error_id": error_id,
                "fingerprint": fingerprint,
                "error_code": classification["error_code"],
                "stage": stage,
                "exception_type": type(exc).__name__,
                "message": compact_log_value(message, key="message"),
                "likely_cause": classification["likely_cause"],
                "owner_module": classification["owner_module"],
                "code_locations": code_locations,
                "task_id": task_id,
                "run_id": context.get("run_id"),
                "preview_run_id": context.get("preview_run_id"),
                "file": f"errors/{json_path.name}",
            }
            self.error_summaries.append(summary)
            self.error_summaries = self.error_summaries[-8:]
            with self.state_lock:
                self.stats["unique_errors"] += 1

            self.event(
                "error",
                task_id=task_id,
                run_id=context.get("run_id"),
                tab_id=context.get("tab_id"),
                preview_run_id=context.get("preview_run_id"),
                stage=stage,
                error_id=error_id,
                error_code=classification["error_code"],
                owner_module=classification["owner_module"],
                exception_type=type(exc).__name__,
                message=message[:500],
            )
            self._write_diagnostic_summary("running")
            return str(json_path)
        except Exception:
            self.disabled = True
            return ""
