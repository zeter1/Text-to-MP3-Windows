"""Low-noise, privacy-bounded evidence for Tk UI stalls and uncaught crashes.

The watchdog never calls Tk or the ordinary logger from its background thread.
A frozen event loop therefore cannot prevent an independent stack snapshot.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import threading
import time
import traceback
import uuid


UI_STALL_THRESHOLD_SEC = 3.0
UI_WATCHDOG_POLL_SEC = 0.5
UI_WATCHDOG_PULSE_MS = 250
MAX_STALL_SNAPSHOTS = 5
MAX_STACK_THREADS = 8
MAX_FRAMES_PER_THREAD = 16


def _stack_evidence(main_ident: int) -> list[dict]:
    """Only module basename / function / line. No source, locals or user text."""
    frames = sys._current_frames()
    names = {item.ident: item.name[:72] for item in threading.enumerate()}
    identifiers = [main_ident] + [ident for ident in frames if ident != main_ident]
    snapshots = []
    for ident in identifiers[:MAX_STACK_THREADS]:
        frame = frames.get(ident)
        if frame is None:
            continue
        try:
            stack = traceback.extract_stack(frame, limit=MAX_FRAMES_PER_THREAD)
            snapshots.append({
                "role": "tk_main" if ident == main_ident else "worker",
                "thread_name": names.get(ident, "unknown"),
                "frames": [
                    {"module": Path(item.filename).name,
                     "function": item.name[:120], "line": item.lineno}
                    for item in stack
                ],
            })
        except Exception:
            continue
    return snapshots


class UiStallMonitor:
    def __init__(self, root, logger, *, threshold_sec: float = UI_STALL_THRESHOLD_SEC):
        self.root = root
        self.logger = logger
        self.threshold_sec = threshold_sec
        self.main_ident = threading.get_ident()
        self.last_pulse = time.monotonic()
        self.stage = "app_startup"
        self.stage_context: dict = {}
        self._stop = threading.Event()
        self._stall: dict | None = None
        self._reported_pulse = False
        self._snapshots = 0
        self._thread: threading.Thread | None = None
        self._slow_operation_last: dict[str, float] = {}
        self._slow_operation_counts: dict[str, int] = {}
        self._slow_operation_max_ms: dict[str, float] = {}

    def start(self) -> None:
        self.root.after(UI_WATCHDOG_PULSE_MS, self._heartbeat)
        self._thread = threading.Thread(
            target=self._watch, name="TkUiStallWatchdog", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _heartbeat(self) -> None:
        if self._stop.is_set():
            return
        self.last_pulse = time.monotonic()
        stall = self._stall
        if stall is not None:
            # Emit from Tk thread after recovery; the snapshot is already on disk.
            self._stall = None
            self.logger.event(
                "ui_stall_recovered",
                stall_code="UI_MAIN_THREAD_STALL",
                stall_duration_ms=round((self.last_pulse - stall["pulse_perf"]) * 1000),
                stage=stall["stage"],
                snapshot_file=stall.get("snapshot_file"),
                stack_captured=stall.get("stack_captured", False),
            )
        self.root.after(UI_WATCHDOG_PULSE_MS, self._heartbeat)

    def _watch(self) -> None:
        while not self._stop.wait(UI_WATCHDOG_POLL_SEC):
            pulse = self.last_pulse
            if (time.monotonic() - pulse < self.threshold_sec or
                    self._stall is not None or
                    self._snapshots >= MAX_STALL_SNAPSHOTS):
                continue
            self._snapshots += 1
            detected = time.monotonic()
            stage = self.stage
            filename = f"hang_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}.json"
            snapshot = {
                "schema": 1, "error_code": "UI_MAIN_THREAD_STALL",
                "detected_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "app_session_id": self.logger.session_id,
                "app_version": _app_version(),
                "delay_ms_at_capture": round((detected - pulse) * 1000),
                "operation": stage,
                "operation_context": dict(self.stage_context),
                "evidence_limitations": (
                    "Stack frames identify Python calls but not CPU use in native Tk/SAPI; "
                    "a blocked GIL can delay this watchdog. No source lines, locals or user text stored."
                ),
                "threads": _stack_evidence(self.main_ident),
            }
            path = self.logger.session_dir / "hangs" / filename
            saved = False
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                # A dedicated file; no ordinary logger lock, no overwriting evidence.
                with path.open("x", encoding="utf-8") as file:
                    json.dump(snapshot, file, ensure_ascii=False, separators=(",", ":"))
                    file.write("\n")
                saved = True
            except OSError as exc:
                try:
                    if sys.stderr is not None:
                        print(f"UI_STALL_SNAPSHOT_WRITE_FAILED:{type(exc).__name__}", file=sys.stderr)
                except Exception:
                    pass
            self._stall = {
                "pulse_perf": pulse, "stage": stage,
                "snapshot_file": f"hangs/{filename}" if saved else None,
                "stack_captured": saved,
            }

    @contextmanager
    def operation(self, name: str, **safe_fields):
        """Annotate the main-thread operation and measure its total wall time."""
        previous_stage, previous_context = self.stage, self.stage_context
        self.stage = name
        # Callers must pass only bounded metadata, never text, full paths or tokens.
        self.stage_context = {
            key: value for key, value in safe_fields.items()
            if isinstance(value, (bool, int, float, type(None)))
        }
        started = time.monotonic()
        try:
            yield
        finally:
            elapsed_ms = round((time.monotonic() - started) * 1000, 1)
            self.stage, self.stage_context = previous_stage, previous_context
            if elapsed_ms >= 700:
                self._slow_operation_counts[name] = self._slow_operation_counts.get(name, 0) + 1
                self._slow_operation_max_ms[name] = max(
                    elapsed_ms, self._slow_operation_max_ms.get(name, 0),
                )
                now = time.monotonic()
                if now - self._slow_operation_last.get(name, float("-inf")) >= 10:
                    count = self._slow_operation_counts.pop(name)
                    maximum = self._slow_operation_max_ms.pop(name)
                    self._slow_operation_last[name] = now
                    self.logger.event(
                        "ui_operation_slow", operation=name,
                        duration_ms=elapsed_ms, occurrences_since_previous=count,
                        max_duration_ms=maximum,
                        **self.stage_context_for_event(safe_fields),
                    )

    @staticmethod
    def stage_context_for_event(fields):
        return {key: value for key, value in fields.items()
                if isinstance(value, (bool, int, float, type(None)))}


def _app_version() -> str:
    # Avoid importing the full runtime from the watchdog thread.
    from .runtime_core import APP_VERSION
    return APP_VERSION


def ui_operation(app, name: str, **safe_fields):
    monitor = getattr(app, "ui_watchdog", None)
    return monitor.operation(name, **safe_fields) if monitor else nullcontext()


class ExceptionHooks:
    def __init__(self, root, logger):
        self.root = root
        self.logger = logger
        self.previous_tk = root.report_callback_exception
        self.previous_thread = threading.excepthook
        self.previous_sys = sys.excepthook
        root.report_callback_exception = self.tk_exception
        threading.excepthook = self.thread_exception
        sys.excepthook = self.sys_exception

    def _record(self, stage: str, exc_type, exc_value, exc_tb, **context):
        if exc_value is None:
            return
        try:
            self.logger.error(
                task_id="app", stage=stage, exc=exc_value,
                traceback_text="".join(traceback.format_exception(exc_type, exc_value, exc_tb)),
                context=context,
            )
        except Exception:
            pass  # Preserve normal Python exception reporting.

    def tk_exception(self, exc_type, exc_value, exc_tb):
        self._record("tk_callback_uncaught", exc_type, exc_value, exc_tb,
                     error_code="TK_CALLBACK_UNCAUGHT")
        self.previous_tk(exc_type, exc_value, exc_tb)

    def thread_exception(self, args):
        if args.exc_type is not SystemExit:
            self._record("worker_thread_uncaught", args.exc_type,
                         args.exc_value, args.exc_traceback,
                         thread_name=str(getattr(args.thread, "name", "unknown"))[:72],
                         error_code="WORKER_THREAD_UNCAUGHT")
        self.previous_thread(args)

    def sys_exception(self, exc_type, exc_value, exc_tb):
        self._record("process_uncaught", exc_type, exc_value, exc_tb,
                     error_code="PROCESS_UNCAUGHT")
        self.previous_sys(exc_type, exc_value, exc_tb)

    def uninstall(self):
        if self.root.report_callback_exception == self.tk_exception:
            self.root.report_callback_exception = self.previous_tk
        if threading.excepthook == self.thread_exception:
            threading.excepthook = self.previous_thread
        if sys.excepthook == self.sys_exception:
            sys.excepthook = self.previous_sys


def acquire_single_instance():
    """Hold a Windows per-login-session named mutex until process exit.

    Return (handle, already_running). Non-Windows has no Windows mutex.
    """
    if os.name != "nt":
        return None, False
    import ctypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
    kernel.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel.CreateMutexW(None, 0, r"Local\ZeTer_TextToMp3_SingleInstance")
    if not handle:
        return None, False  # Fail open when Windows denies the mutex.
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel.CloseHandle(handle)
        return None, True
    return (kernel, handle), False


def release_single_instance(lock) -> None:
    if lock is not None:
        kernel, handle = lock
        kernel.CloseHandle(handle)
