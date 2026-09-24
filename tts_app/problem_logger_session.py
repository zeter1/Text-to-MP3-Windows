from __future__ import annotations

from .runtime import *
from .problem_logger_compact import write_log_json

class ProblemLoggerSessionMixin:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.state_lock = threading.RLock()
        self.session_write_lock = threading.Lock()
        self.summary_lock = threading.Lock()
        self.disabled = False

        self.session_id = (
            datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            + "_"
            + uuid.uuid4().hex[:6]
        )
        self.session_dir = LOGS_DIR / f"session_{self.session_id}"
        self.errors_dir = self.session_dir / "errors"
        self.session_file = self.session_dir / "session.json"
        self.diagnostic_summary_file = (
            self.session_dir / "diagnostic_summary.json"
        )
        self.ai_read_first_file = self.session_dir / "AI_READ_FIRST.json"

        self.event_file_index = 1
        self.events_file = self.session_dir / "events.jsonl"
        self.event_files = [self.events_file.name]

        self.started_perf = time.perf_counter()
        self.started_at = now_iso()
        self.last_live_summary_perf = self.started_perf
        self.event_bytes_written = 0
        self.last_event_write: dict[str, tuple[str, float]] = {}
        self.suppressed_event_counts: dict[str, int] = {}
        self.recent_signal_events: list[dict] = []
        self.error_records: dict[str, dict] = {}
        self.error_summaries: list[dict] = []

        self.tasks_started = 0
        self.tasks_success = 0
        self.tasks_failed = 0
        self.tasks_cancelled = 0

        self.active_tasks: dict[str, dict] = {}
        self.previous_unclean_sessions: list[dict] = []
        self.completed_tasks: list[dict] = []
        self.last_successful_task: dict | None = None
        self.latest_text_state_by_tab: dict[str, dict] = {}
        self.global_copy_source_counts: dict[str, int] = {}

        self.stats = {
            "errors": 0,
            "sapi_retries": 0,
            "slow_chunks": 0,
            "task_progress_events": 0,
            "preview_started": 0,
            "preview_pause_requests": 0,
            "preview_resume_requests": 0,
            "preview_progress_events": 0,
            "audio_output_mismatches": 0,
            "low_disk_warnings": 0,
            "forced_shutdowns": 0,
            "output_validation_warnings": 0,
            "recovery_jobs_found": 0,
            "recovery_jobs_resumed": 0,
            "recovery_jobs_discarded": 0,

            # Listening / global-copy diagnostics (3.7+).
            "global_copy_captures": 0,
            "global_copy_captured_chars": 0,
            "global_copy_failures": 0,
            "global_copy_duplicates_skipped": 0,
            "global_copy_latency_ms_sum": 0.0,
            "global_copy_latency_ms_max": 0.0,
            "read_deleted_batches": 0,
            "read_deleted_segments": 0,
            "read_deleted_chars": 0,
            "appended_tail_continuations": 0,
            "text_state_events": 0,
            "workspace_slow_saves": 0,
            "preview_finish_completed": 0,
            "preview_finish_user_stop": 0,
            "preview_finish_resume_rewind": 0,
            "preview_finish_another_tab": 0,
            "preview_finish_app_exit": 0,
            "preview_finish_other": 0,
            "events_written": 0,
            "events_suppressed": 0,
            "event_bytes_written": 0,
            "unique_errors": 0,
        }

        try:
            # Detect crashes before retention cleanup so even an old unclean
            # session can be summarized into the new diagnostic report.
            self.previous_unclean_sessions = (
                self._detect_unclean_previous_sessions()
            )
            self._cleanup_old_sessions()

            self.errors_dir.mkdir(parents=True, exist_ok=True)
            self._write_session("running")

            self.event(
                "app_start",
                app_version=APP_VERSION,
                pid=os.getpid(),
                python=sys.version.split()[0],
                pywin32_version=get_package_version("pywin32"),
                tkinter_tcl_version=str(tk.TclVersion),
                tkinter_tk_version=str(tk.TkVersion),
                platform=platform.platform(),
                executable=sys.executable,
                frozen=bool(getattr(sys, "frozen", False)),
                storage_base=str(STORAGE_BASE_DIR),
                settings_dir=str(SETTINGS_DIR),
                logs_dir=str(LOGS_DIR),
            )

            for item in self.previous_unclean_sessions:
                self.event(
                    "previous_session_unclean_shutdown",
                    **item,
                )

            self._write_diagnostic_summary("running")
        except Exception:
            self.disabled = True

    def _cleanup_old_sessions(self) -> None:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        cutoff = datetime.now() - timedelta(days=LOG_RETENTION_DAYS)

        sessions: list[tuple[float, Path]] = []
        for path in LOGS_DIR.glob("session_*"):
            if not path.is_dir():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue

            if datetime.fromtimestamp(stat.st_mtime) < cutoff:
                shutil.rmtree(path, ignore_errors=True)
                continue

            sessions.append((stat.st_mtime, path))

        sessions.sort(reverse=True)
        # Keep room for the session being created now.
        for _, path in sessions[max(0, LOG_MAX_SESSIONS - 1):]:
            shutil.rmtree(path, ignore_errors=True)

    def _detect_unclean_previous_sessions(self) -> list[dict]:
        detected: list[dict] = []

        for session_dir in sorted(
            LOGS_DIR.glob("session_*"),
            key=lambda path: path.stat().st_mtime if path.exists() else 0,
            reverse=True,
        ):
            session_file = session_dir / "session.json"
            if not session_file.exists():
                continue

            try:
                payload = json.loads(
                    session_file.read_text(encoding="utf-8")
                )
            except Exception:
                continue

            if payload.get("status") != "running":
                continue

            environment = payload.get("environment")
            if not isinstance(environment, dict):
                environment = {}

            pid = environment.get("pid")
            if pid is None:
                # Sessions from versions before 2.8 do not contain a PID.
                # We cannot safely distinguish a crash from another live instance.
                continue

            try:
                pid_int = int(pid)
            except Exception:
                continue

            if process_is_running(pid_int):
                continue

            started_ids: set[str] = set()
            finished_ids: set[str] = set()
            last_event = None

            for event_path in sorted(session_dir.glob("events*.jsonl")):
                try:
                    with event_path.open(
                        "r",
                        encoding="utf-8",
                        errors="replace",
                    ) as file:
                        for line in file:
                            try:
                                event = json.loads(line)
                            except Exception:
                                continue
                            last_event = event
                            task_id = str(event.get("task_id") or "")
                            event_type = str(event.get("event") or "")
                            if event_type == "task_started" and task_id:
                                started_ids.add(task_id)
                            if (
                                event_type
                                in {
                                    "task_success",
                                    "task_failed",
                                    "task_cancelled",
                                }
                                and task_id
                            ):
                                finished_ids.add(task_id)
                except OSError:
                    pass

            for task_file in session_dir.glob("task_*.json"):
                try:
                    task_payload = json.loads(
                        task_file.read_text(encoding="utf-8")
                    )
                    task_id = str(task_payload.get("task_id") or "")
                    status = str(task_payload.get("status") or "")
                    if task_id and status in {
                        "success",
                        "failed",
                        "cancelled",
                    }:
                        finished_ids.add(task_id)
                except Exception:
                    pass

            unfinished = sorted(started_ids - finished_ids)

            item = {
                "previous_session_id": str(
                    payload.get("session_id") or session_dir.name
                ),
                "previous_pid": pid_int,
                "unfinished_task_ids": unfinished,
                "last_event": last_event,
            }
            detected.append(item)

            try:
                payload["status"] = "unclean_shutdown_detected"
                payload["unclean_shutdown_detected_at"] = now_iso()
                payload["unfinished_task_ids"] = unfinished
                write_log_json(session_file, payload)
            except Exception:
                pass

        return detected

    def _session_payload(self, status: str) -> dict:
        with self.state_lock:
            active_tasks = {
                task_id: dict(info)
                for task_id, info in self.active_tasks.items()
            }
            tasks_started = self.tasks_started
            tasks_success = self.tasks_success
            tasks_failed = self.tasks_failed
            tasks_cancelled = self.tasks_cancelled

        return {
            "schema": 2,
            "app": {"name": APP_TITLE, "version": APP_VERSION},
            "session_id": self.session_id,
            "status": status,
            "started_at": self.started_at,
            "updated_at": now_iso(),
            "duration_sec": round(
                time.perf_counter() - self.started_perf,
                3,
            ),
            "environment": {
                "pid": os.getpid(),
                "python": sys.version,
                "pywin32_version": get_package_version("pywin32"),
                "tkinter_tcl_version": str(tk.TclVersion),
                "tkinter_tk_version": str(tk.TkVersion),
                "platform": platform.platform(),
                "executable": sys.executable,
                "frozen": bool(getattr(sys, "frozen", False)),
                "storage_base": str(STORAGE_BASE_DIR),
                "settings_dir": str(SETTINGS_DIR),
                "logs_dir": str(LOGS_DIR),
            },
            "logging": {
                "event_files": list(self.event_files),
                "event_file_limit_bytes": EVENTS_MAX_BYTES,
                "session_soft_limit_bytes": EVENTS_SESSION_SOFT_LIMIT_BYTES,
                "event_bytes_written": self.event_bytes_written,
                "suppressed_events": dict(self.suppressed_event_counts),
                "ai_read_first": self.ai_read_first_file.name,
            },
            "summary": {
                "tasks_started": tasks_started,
                "tasks_success": tasks_success,
                "tasks_failed": tasks_failed,
                "tasks_cancelled": tasks_cancelled,
                "active_tasks": active_tasks,
            },
        }

    def _write_session(self, status: str) -> None:
        if self.disabled:
            return

        with self.session_write_lock:
            write_log_json(
                self.session_file,
                self._session_payload(status),
            )

    def close(self, status: str = "closed") -> None:
        if self.disabled:
            return

        self.event("app_exit", status=status)
        try:
            self._write_session(status)
            self._write_diagnostic_summary(status)
        except Exception:
            pass
