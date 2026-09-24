from __future__ import annotations

from .runtime import *
from .problem_logger_compact import compact_log_value, compact_text_state, write_log_json


class ProblemLoggerSummaryMixin:
    def _write_diagnostic_summary(self, status: str) -> None:
        if self.disabled:
            return

        with self.summary_lock:
            with self.state_lock:
                active = {
                    task_id: compact_log_value(dict(info), key="active_task")
                    for task_id, info in self.active_tasks.items()
                }
                stats_snapshot = dict(self.stats)
                completed_snapshot = [
                    dict(item) for item in self.completed_tasks[-3:]
                ]
                last_success_snapshot = (
                    dict(self.last_successful_task)
                    if self.last_successful_task
                    else None
                )
                warnings_snapshot = self._warnings()
                observations_snapshot = self._observations()
                latest_text_state_snapshot = {
                    tab_id: compact_text_state(dict(state))
                    for tab_id, state in self.latest_text_state_by_tab.items()
                }
                source_counts_snapshot = dict(self.global_copy_source_counts)
                error_snapshot = [
                    dict(item) for item in self.error_summaries[-5:]
                ]
                recent_signals_snapshot = [
                    dict(item) for item in self.recent_signal_events[-RECENT_SIGNAL_EVENTS_LIMIT:]
                ]
                suppressed_snapshot = dict(self.suppressed_event_counts)

            capture_count = int(stats_snapshot.get("global_copy_captures") or 0)
            capture_latency_sum = float(
                stats_snapshot.get("global_copy_latency_ms_sum") or 0
            )
            nonzero_stats = {
                key: value
                for key, value in stats_snapshot.items()
                if value not in (0, 0.0, None, False, "")
                and key not in {
                    "global_copy_latency_ms_sum",
                    "event_bytes_written",
                }
            }
            health = (
                "error"
                if error_snapshot
                else "warning"
                if warnings_snapshot
                else "ok"
            )
            primary_problem = error_snapshot[-1] if error_snapshot else None
            first_failure = error_snapshot[0] if error_snapshot else None
            first_failure_ref = None
            if first_failure:
                first_failure_ref = {
                    "error_id": first_failure.get("error_id"),
                    "error_code": first_failure.get("error_code"),
                    "stage": first_failure.get("stage"),
                    "file": first_failure.get("file"),
                }

            listening_summary = {
                "global_copy_captures": capture_count,
                "global_copy_chars": int(
                    stats_snapshot.get("global_copy_captured_chars") or 0
                ),
                "global_copy_failures": int(
                    stats_snapshot.get("global_copy_failures") or 0
                ),
                "average_copy_latency_ms": (
                    round(capture_latency_sum / capture_count, 1)
                    if capture_count
                    else 0
                ),
                "max_copy_latency_ms": float(
                    stats_snapshot.get("global_copy_latency_ms_max") or 0
                ),
                "source_process_counts": source_counts_snapshot,
                "deleted_segments": int(
                    stats_snapshot.get("read_deleted_segments") or 0
                ),
                "deleted_chars": int(
                    stats_snapshot.get("read_deleted_chars") or 0
                ),
                "latest_text_state_by_tab": latest_text_state_snapshot,
            }

            next_files: list[str] = []
            if primary_problem and primary_problem.get("file"):
                next_files.append(str(primary_problem["file"]))
            run_id = (primary_problem or {}).get("run_id")
            if run_id:
                next_files.append(f"task_{run_id}.json")
            next_files.extend(list(self.event_files))
            next_files = list(dict.fromkeys(next_files))[:6]

            ai_payload = {
                "schema": 1,
                "read_this_first": True,
                "generated_at": now_iso(),
                "session_id": self.session_id,
                "status": status,
                "health": health,
                "app": {
                    "version": APP_VERSION,
                    "python": sys.version.split()[0],
                    "pywin32": get_package_version("pywin32"),
                    "platform": platform.platform(),
                },
                "problem_count": int(stats_snapshot.get("errors") or 0),
                "unique_problem_count": int(stats_snapshot.get("unique_errors") or 0),
                "primary_problem": primary_problem,
                "first_failure": first_failure_ref,
                "warnings": warnings_snapshot[:8],
                "observations": observations_snapshot[:8],
                "tasks": {
                    "started": self.tasks_started,
                    "success": self.tasks_success,
                    "failed": self.tasks_failed,
                    "cancelled": self.tasks_cancelled,
                    "active": active,
                    "last_successful": last_success_snapshot,
                    "recent_completed": completed_snapshot,
                },
                "key_stats": nonzero_stats,
                "listening": listening_summary,
                "recent_signals": recent_signals_snapshot[-6:],
                "previous_unclean_sessions": self.previous_unclean_sessions[-2:],
                "log_volume": {
                    "events_written": int(stats_snapshot.get("events_written") or 0),
                    "events_suppressed": int(stats_snapshot.get("events_suppressed") or 0),
                    "event_bytes": self.event_bytes_written,
                    "suppressed_by_type": suppressed_snapshot,
                    "soft_limit_bytes": EVENTS_SESSION_SOFT_LIMIT_BYTES,
                },
                "open_next_if_needed": next_files,
                "ai_instruction": (
                    "Ищи first divergence по stage/error_code/recent_signals. "
                    "Сначала открой primary_problem.file. Не читай весь events.jsonl, "
                    "пока summary/error JSON не недостаточны."
                ),
            }
            write_log_json(self.ai_read_first_file, ai_payload)

            # Compatibility file for older instructions/tools. Keep it tiny so a
            # session does not pay for two near-identical summaries.
            compatibility_payload = {
                "schema": 3,
                "session_id": self.session_id,
                "session_status": status,
                "health": health,
                "read_first": self.ai_read_first_file.name,
                "primary_problem": primary_problem,
                "warnings": warnings_snapshot[:5],
                "event_files": list(self.event_files),
                "note": (
                    "Полная AI-friendly сводка находится в AI_READ_FIRST.json; "
                    "events*.jsonl нужен только для точечного drill-down."
                ),
            }
            write_log_json(self.diagnostic_summary_file, compatibility_payload)
