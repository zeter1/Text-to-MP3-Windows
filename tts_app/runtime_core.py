from __future__ import annotations

import bisect
import ctypes
import hashlib
import html
import importlib.metadata
import json
import os
import platform
import queue
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import unicodedata
import uuid
import wave
from datetime import datetime, timedelta
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk


APP_TITLE = "Текст → MP3 (Windows SAPI)"
APP_VERSION = "4.2.2 FULL"

DEFAULT_VOICE_HINT = "Microsoft Irina Desktop"
DEFAULT_AUDIO_OUTPUT_LABEL = "Системное устройство по умолчанию"
TAB_HOTKEY_NONE_LABEL = "Нет"

# Автозапуск текущей копии программы для текущего пользователя Windows.
AUTOSTART_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_REG_VALUE = "TextToMp3Irina"
DEFAULT_CHUNK_SIZE = 2600
MAX_RETRIES = 3

# Расширенные пользовательские диапазоны.
#
# Нативное свойство SAPI SpVoice.Rate официально рассчитано на -10..+10.
# Значения скорости выше этого диапазона реализуются комбинацией:
#   SpVoice.Rate (-10..+10) + относительный XML <rate speed="...">.
#
# Для Pitch используем вложенные относительные XML <pitch middle="...">,
# каждый шаг которых остаётся в стандартном диапазоне -10..+10.
RATE_MIN = -20
RATE_MAX = 20
PITCH_MIN = -24
PITCH_MAX = 24
SAPI_NATIVE_RATE_MIN = -10
SAPI_NATIVE_RATE_MAX = 10
SAPI_XML_ADJUSTMENT_MIN = -10
SAPI_XML_ADJUSTMENT_MAX = 10

SETTINGS_DIR_NAME = "Настройки программы"
SETTINGS_FILE_NAME = "settings.json"
WORKSPACE_FILE_NAME = "workspace.json"
WORKSPACE_TEXT_DIR_NAME = "Вкладки"
LOGS_DIR_NAME = "Логи проблем"
RECOVERY_DIR_NAME = "Незавершённые задачи"
MP3_TEXT_BACKUPS_DIR_NAME = "Бэкапы текста MP3"

LOG_RETENTION_DAYS = 10
LOG_MAX_SESSIONS = 12
EVENTS_MAX_BYTES = 512 * 1024
EVENTS_SESSION_SOFT_LIMIT_BYTES = 768 * 1024
EVENT_DEDUP_DEFAULT_SEC = 3.0
EVENT_DEDUP_TEXT_STATE_SEC = 45.0
RECENT_SIGNAL_EVENTS_LIMIT = 12
ERROR_TRACEBACK_MAX_LINES = 30
ERROR_TRACEBACK_MAX_CHARS = 7000
TASK_HEARTBEAT_INTERVAL_SEC = 15.0
PREVIEW_HEARTBEAT_INTERVAL_SEC = 15.0
SHUTDOWN_GRACE_SEC = 10.0
FFMPEG_TERMINATE_TIMEOUT_SEC = 3.0
LOW_DISK_WARNING_BYTES = 1024 * 1024 * 1024
DISK_FORECAST_SAMPLE_CHUNKS = 8
DISK_FORECAST_RESERVE_RATIO = 0.20
RECOVERY_RETENTION_DAYS = 7
MP3_TEXT_BACKUP_RETENTION_DAYS = 45
ADAPTIVE_SLOW_ABSOLUTE_SEC = 3.0
ADAPTIVE_SLOW_MULTIPLIER = 4.0

# Preview/log/UI throttling. These keep long listening sessions diagnosable
# without producing thousands of repetitive events or excessive disk writes.
READ_DELETE_LOG_FLUSH_INTERVAL_SEC = 15.0
TEXT_STATE_LOG_INTERVAL_SEC = 15.0
WORKSPACE_READING_SAVE_INTERVAL_SEC = 15.0
WORKSPACE_SLOW_SAVE_SEC = 0.20
QUEUE_STATUS_REFRESH_MS = 700

# Windows may temporarily deny replacement while antivirus, indexing or another
# reader holds the current state file without delete sharing. Retry only that
# narrow publication step; other filesystem errors must remain visible.
ATOMIC_REPLACE_RETRY_DELAYS_SEC = (0.05, 0.10, 0.20, 0.40, 0.80)
WORKSPACE_SAVE_RETRY_DELAYS_MS = (2_000, 5_000, 15_000, 30_000)
_ATOMIC_WRITE_LOCK = threading.RLock()

# Global-copy reliability: wait for a real Windows clipboard sequence change
# instead of assuming that Ctrl+C completed after a fixed delay.
CLIPBOARD_COPY_TIMEOUT_SEC = 1.5
CLIPBOARD_POLL_INTERVAL_MS = 30
LIVE_DIAGNOSTIC_SUMMARY_INTERVAL_SEC = 10.0

# Reader 3.9: speak several sentences as one SAPI stream instead of
# restarting Speak() on every sentence. This avoids audible level/envelope
# resets on some real audio devices while status polling still highlights
# the sentence that is actually being spoken.
PREVIEW_SAPI_BLOCK_MAX_CHARS = 6000

AUTO_TAB_TITLE_RE = re.compile(r"^(?:Задача|Вкладка)\s+\d+$", re.IGNORECASE)
HOTKEY_CAPTURE_DISALLOWED_RE = re.compile(
    r"[^А-Яа-яЁё0-9., ]+"
)


def is_automatic_tab_title(title: str) -> bool:
    """Recognize old/new automatically generated tab names for migration."""
    return bool(AUTO_TAB_TITLE_RE.fullmatch((title or "").strip()))


def sanitize_hotkey_capture_text(text: str) -> str:
    """
    Keep only characters explicitly allowed for global-hotkey reading.

    Disallowed runs become spaces instead of disappearing completely, so
    words separated by a dash, slash, emoji or HTML fragment never stick
    together. All whitespace is normalized to an ordinary single space.
    """
    if not isinstance(text, str):
        return ""

    normalized = unicodedata.normalize("NFKC", text)
    normalized = re.sub(r"\s+", " ", normalized)
    normalized = HOTKEY_CAPTURE_DISALLOWED_RE.sub(
        " ",
        normalized,
    )
    normalized = re.sub(r" +", " ", normalized)
    return normalized.strip()


# SAPI constants
SSFM_CREATE_FOR_WRITE = 3
SVSF_DEFAULT = 0
SVS_FLAGS_ASYNC = 1
SVSFPURGE_BEFORE_SPEAK = 2
SVSF_IS_XML = 8


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def clamp_int(value, minimum: int, maximum: int, fallback: int) -> int:
    try:
        return max(minimum, min(maximum, int(float(value))))
    except Exception:
        return fallback


def make_run_id(prefix: str) -> str:
    return (
        f"{prefix}_"
        + datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        + "_"
        + uuid.uuid4().hex[:6]
    )


def percentile(values: list[float], percent: float) -> float:
    """Linear-interpolated percentile without external dependencies."""
    if not values:
        return 0.0

    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]

    percent = max(0.0, min(100.0, float(percent)))
    position = (len(ordered) - 1) * (percent / 100.0)
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    fraction = position - lower
    return (
        ordered[lower] * (1.0 - fraction)
        + ordered[upper] * fraction
    )


def format_eta(seconds: float | int | None) -> str:
    try:
        total = max(0, int(round(float(seconds))))
    except Exception:
        return ""

    if total < 60:
        return f"~{total} сек"

    minutes, sec = divmod(total, 60)
    if minutes < 60:
        return f"~{minutes} мин {sec:02d} сек"

    hours, minutes = divmod(minutes, 60)
    return f"~{hours} ч {minutes:02d} мин"


def format_bytes(value: int | float | None) -> str:
    try:
        size = max(0.0, float(value))
    except Exception:
        return "неизвестно"

    units = ("Б", "КБ", "МБ", "ГБ", "ТБ")
    unit_index = 0
    while size >= 1024.0 and unit_index < len(units) - 1:
        size /= 1024.0
        unit_index += 1

    if unit_index == 0:
        return f"{int(size)} {units[unit_index]}"
    return f"{size:.2f} {units[unit_index]}"


def bitrate_to_bps(value: str) -> int:
    match = re.fullmatch(r"\s*(\d+)\s*[kK]\s*", str(value or ""))
    if not match:
        return 96_000
    return max(8_000, int(match.group(1)) * 1000)


def file_snapshot(path: Path) -> dict:
    try:
        stat = path.stat()
        return {
            "exists": True,
            "size_bytes": int(stat.st_size),
            "modified_at": datetime.fromtimestamp(
                stat.st_mtime
            ).astimezone().isoformat(timespec="seconds"),
        }
    except FileNotFoundError:
        return {
            "exists": False,
            "size_bytes": 0,
            "modified_at": None,
        }
    except Exception as exc:
        return {
            "exists": None,
            "size_bytes": None,
            "modified_at": None,
            "snapshot_error": f"{type(exc).__name__}: {exc}",
        }


def same_storage_volume(first: Path, second: Path) -> bool:
    """Best-effort check whether two paths consume free space on one volume."""
    try:
        if os.name == "nt":
            first_drive = os.path.splitdrive(
                os.path.abspath(str(first))
            )[0].casefold()
            second_drive = os.path.splitdrive(
                os.path.abspath(str(second))
            )[0].casefold()
            return bool(first_drive) and first_drive == second_drive

        return first.resolve().anchor == second.resolve().anchor
    except Exception:
        return False


def get_package_version(distribution: str) -> str:
    try:
        return importlib.metadata.version(distribution)
    except Exception:
        return ""


def process_is_running(pid: int) -> bool:
    """Best-effort process liveness check used for unclean-session detection."""
    try:
        pid = int(pid)
    except Exception:
        return False

    if pid <= 0:
        return False
    if pid == os.getpid():
        return True

    if os.name == "nt":
        try:
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION,
                False,
                pid,
            )
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                ok = kernel32.GetExitCodeProcess(
                    handle,
                    ctypes.byref(exit_code),
                )
                return bool(ok) and exit_code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False

    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def safe_disk_free_bytes(path: Path) -> int | None:
    try:
        return int(shutil.disk_usage(path).free)
    except Exception:
        return None
