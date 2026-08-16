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
APP_VERSION = "4.2 FULL"

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

LOG_RETENTION_DAYS = 14
LOG_MAX_SESSIONS = 20
EVENTS_MAX_BYTES = 2 * 1024 * 1024
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


def analyze_text_for_logging(raw_text: str, normalized_text: str) -> dict:
    """Compact structural diagnostics without storing the user's full text."""
    unusual_controls = 0
    for ch in raw_text:
        if ch in "\n\t\r":
            continue
        if unicodedata.category(ch) in {"Cc", "Cf", "Cs", "Co", "Cn"}:
            unusual_controls += 1

    lines = raw_text.splitlines()
    line_lengths = [len(line) for line in lines]
    nonempty_lengths = [len(line) for line in lines if line.strip()]
    blank_lines = sum(1 for line in lines if not line.strip())
    blank_line_blocks = len(
        [part for part in re.split(r"\n{2,}", raw_text) if part.strip()]
    )
    sentence_endings = len(re.findall(r"[.!?…]+(?=\s|$)", raw_text))

    return {
        "raw_chars": len(raw_text),
        "normalized_chars": len(normalized_text),
        "normalization_char_delta": len(normalized_text) - len(raw_text),
        "control_or_format_chars": unusual_controls,
        "line_breaks": raw_text.count("\n"),
        "lines_total": len(lines),
        "nonempty_lines": len(nonempty_lengths),
        "blank_lines": blank_lines,
        "blank_line_blocks": blank_line_blocks,
        "line_chars_avg": (
            round(sum(line_lengths) / len(line_lengths), 2)
            if line_lengths
            else 0
        ),
        "nonempty_line_chars_avg": (
            round(sum(nonempty_lengths) / len(nonempty_lengths), 2)
            if nonempty_lengths
            else 0
        ),
        "max_line_chars": max(line_lengths) if line_lengths else 0,
        "very_long_lines_over_2000_chars": sum(
            1 for length in line_lengths if length > 2000
        ),
        "sentence_endings_approx": sentence_endings,
    }


def get_wav_info(path: Path) -> dict:
    try:
        file_size = int(path.stat().st_size)
        with wave.open(str(path), "rb") as wav:
            frame_rate = int(wav.getframerate())
            frames = int(wav.getnframes())
            channels = int(wav.getnchannels())
            sample_width_bytes = int(wav.getsampwidth())
            expected_pcm_bytes = (
                frames
                * channels
                * sample_width_bytes
            )
            # Standard PCM WAV has at least ~44 bytes of headers. Some SAPI WAVs
            # use a larger header, so check only that the declared PCM payload can
            # physically fit in the file.
            appears_complete = bool(
                expected_pcm_bytes <= max(0, file_size - 36)
            )
            return {
                "channels": channels,
                "sample_rate_hz": frame_rate,
                "sample_width_bits": sample_width_bytes * 8,
                "frames": frames,
                "duration_sec": (
                    round(frames / frame_rate, 3)
                    if frame_rate > 0
                    else 0
                ),
                "file_size_bytes": file_size,
                "expected_pcm_data_bytes": expected_pcm_bytes,
                "appears_complete": appears_complete,
            }
    except Exception:
        return {}


def wav_total_duration_seconds(wav_files: list[Path]) -> float:
    total = 0.0
    for path in wav_files:
        info = get_wav_info(path)
        try:
            total += float(info.get("duration_sec") or 0)
        except Exception:
            pass
    return total


def get_ffmpeg_version(ffmpeg: str) -> str:
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]

    try:
        result = subprocess.run(
            [ffmpeg, "-version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            creationflags=creationflags,
        )
        first_line = (result.stdout or result.stderr or "").splitlines()
        return first_line[0].strip() if first_line else ""
    except Exception:
        return ""


def find_ffprobe(ffmpeg: str) -> str | None:
    system_ffprobe = shutil.which("ffprobe")
    if system_ffprobe:
        return system_ffprobe

    ffmpeg_path = Path(ffmpeg)
    candidates = [
        ffmpeg_path.with_name("ffprobe.exe"),
        ffmpeg_path.with_name("ffprobe"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def probe_audio_file(ffmpeg: str, path: Path) -> dict:
    """
    Fast post-encode validation.

    Prefer ffprobe. If ffprobe is not installed, use FFmpeg itself only to read
    container/stream metadata; this does NOT decode the whole multi-hour MP3.
    """
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]

    ffprobe = find_ffprobe(ffmpeg)
    if ffprobe:
        try:
            result = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-select_streams",
                    "a:0",
                    "-show_entries",
                    (
                        "format=duration,bit_rate,format_name:"
                        "stream=codec_name,sample_rate,channels,bit_rate"
                    ),
                    "-of",
                    "json",
                    str(path),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                creationflags=creationflags,
            )
            if result.returncode == 0:
                payload = json.loads(result.stdout or "{}")
                streams = payload.get("streams") or []
                stream = streams[0] if streams else {}
                fmt = payload.get("format") or {}
                codec_name = str(
                    stream.get("codec_name") or ""
                )
                duration_value = float(
                    fmt.get("duration") or 0
                )
                return {
                    "tool": "ffprobe",
                    "tool_path": ffprobe,
                    "validated": bool(
                        codec_name and duration_value > 0
                    ),
                    "codec": codec_name,
                    "sample_rate_hz": clamp_int(
                        stream.get("sample_rate"),
                        0,
                        1_000_000,
                        0,
                    ),
                    "channels": clamp_int(
                        stream.get("channels"),
                        0,
                        64,
                        0,
                    ),
                    "bit_rate_bps": clamp_int(
                        stream.get("bit_rate")
                        or fmt.get("bit_rate"),
                        0,
                        10_000_000,
                        0,
                    ),
                    "duration_sec": duration_value,
                    "format_name": str(fmt.get("format_name") or ""),
                }
        except Exception:
            pass

    # Metadata-only fallback. "-t 0" exits immediately after input probing.
    try:
        result = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-i",
                str(path),
                "-t",
                "0",
                "-f",
                "null",
                os.devnull,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            creationflags=creationflags,
        )
        output = (result.stderr or "") + "\n" + (result.stdout or "")

        duration_sec = 0.0
        duration_match = re.search(
            r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)",
            output,
        )
        if duration_match:
            duration_sec = (
                float(duration_match.group(1)) * 3600
                + float(duration_match.group(2)) * 60
                + float(duration_match.group(3))
            )

        audio_match = re.search(
            r"Audio:\s*([^,]+),\s*(\d+)\s*Hz,\s*([^,]+)",
            output,
            re.IGNORECASE,
        )
        codec = ""
        sample_rate = 0
        channels = 0
        if audio_match:
            codec = audio_match.group(1).strip().split()[0]
            sample_rate = int(audio_match.group(2))
            channel_text = audio_match.group(3).strip().casefold()
            if "stereo" in channel_text:
                channels = 2
            elif "mono" in channel_text:
                channels = 1

        bitrate_match = re.search(
            r"bitrate:\s*(\d+)\s*kb/s",
            output,
            re.IGNORECASE,
        )
        bitrate = (
            int(bitrate_match.group(1)) * 1000
            if bitrate_match
            else 0
        )

        return {
            "tool": "ffmpeg_metadata_fallback",
            "tool_path": ffmpeg,
            "validated": bool(duration_sec > 0 and codec),
            "codec": codec,
            "sample_rate_hz": sample_rate,
            "channels": channels,
            "bit_rate_bps": bitrate,
            "duration_sec": duration_sec,
            "format_name": "mp3" if codec.casefold() == "mp3" else "",
        }
    except Exception as exc:
        return {
            "tool": "unavailable",
            "tool_path": "",
            "validated": False,
            "probe_error": f"{type(exc).__name__}: {exc}",
            "codec": "",
            "sample_rate_hz": 0,
            "channels": 0,
            "bit_rate_bps": 0,
            "duration_sec": 0.0,
            "format_name": "",
        }


def validate_final_mp3(
    ffmpeg: str,
    path: Path,
    expected_duration_sec: float,
) -> dict:
    result = probe_audio_file(ffmpeg, path)
    result["expected_duration_sec"] = round(
        float(expected_duration_sec or 0),
        3,
    )

    codec = str(result.get("codec") or "").casefold()
    if result.get("validated") and codec and codec != "mp3":
        result["fatal_error"] = (
            "Проверка готового файла обнаружила неожиданный кодек: "
            f"{codec}."
        )
        return result

    actual = float(result.get("duration_sec") or 0)
    expected = float(expected_duration_sec or 0)
    if actual > 0 and expected > 0:
        delta = abs(actual - expected)
        tolerance = max(3.0, expected * 0.0001)
        result["duration_delta_sec"] = round(delta, 3)
        result["duration_tolerance_sec"] = round(tolerance, 3)
        result["duration_matches"] = bool(delta <= tolerance)
        if delta > tolerance:
            result["fatal_error"] = (
                "Итоговый MP3, похоже, обрезан или имеет неверную длительность: "
                f"ожидалось около {expected:.1f} сек, получено {actual:.1f} сек."
            )
    else:
        result["duration_delta_sec"] = None
        result["duration_tolerance_sec"] = None
        result["duration_matches"] = None

    return result


def estimate_disk_forecast(
    *,
    processed_chunks: int,
    total_chunks: int,
    temp_wav_bytes: int,
    sampled_audio_sec: float,
    bitrate: str,
) -> dict:
    if processed_chunks <= 0 or total_chunks <= 0:
        return {}

    ratio = total_chunks / processed_chunks
    estimated_temp_wav = int(temp_wav_bytes * ratio)
    estimated_audio_sec = max(0.0, sampled_audio_sec * ratio)
    estimated_mp3 = int(
        estimated_audio_sec
        * bitrate_to_bps(bitrate)
        / 8.0
        * 1.03
    )
    return {
        "sampled_chunks": processed_chunks,
        "chunks_total": total_chunks,
        "estimated_temp_wav_bytes": estimated_temp_wav,
        "estimated_audio_duration_sec": round(
            estimated_audio_sec,
            3,
        ),
        "estimated_mp3_bytes": estimated_mp3,
        "reserve_ratio": DISK_FORECAST_RESERVE_RATIO,
    }


def cleanup_legacy_temp_workdirs() -> int:
    """Delete old pre-2.9 temp folders that can no longer be resumed safely."""
    removed = 0
    cutoff = time.time() - RECOVERY_RETENTION_DAYS * 86400
    temp_root = Path(tempfile.gettempdir())
    try:
        candidates = list(temp_root.glob("text_to_mp3_*"))
    except Exception:
        return 0

    for path in candidates:
        try:
            if path.is_dir() and path.stat().st_mtime < cutoff:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        except Exception:
            pass
    return removed


def scan_recovery_jobs() -> tuple[list[dict], int]:
    """
    Return resumable job manifests and remove expired/corrupt recovery folders.
    Live jobs owned by any running process are never touched.
    """
    RECOVERY_DIR.mkdir(parents=True, exist_ok=True)
    jobs: list[dict] = []
    removed = 0
    cutoff = time.time() - RECOVERY_RETENTION_DAYS * 86400

    for job_dir in RECOVERY_DIR.glob("job_*"):
        if not job_dir.is_dir():
            continue

        manifest_path = job_dir / "recovery.json"
        try:
            payload = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )
            if not isinstance(payload, dict):
                raise ValueError("recovery.json is not an object")

            run_id = str(payload.get("run_id") or "").strip()
            text_sha256 = str(
                payload.get("text_sha256") or ""
            ).strip()
            chunks_total = clamp_int(
                payload.get("chunks_total"),
                0,
                10_000_000,
                0,
            )
            output_text = str(
                payload.get("output_path") or ""
            ).strip()

            if (
                not run_id
                or not text_sha256
                or chunks_total <= 0
                or not output_text
            ):
                raise ValueError(
                    "recovery.json misses required fields"
                )

            owner_pid = clamp_int(
                payload.get("owner_pid"),
                0,
                2_147_483_647,
                0,
            )
            if owner_pid and process_is_running(owner_pid):
                continue

            if job_dir.stat().st_mtime < cutoff:
                output_path = Path(output_text)
                partial_output = output_path.with_name(
                    output_path.stem + ".part.mp3"
                )
                try:
                    partial_output.unlink(missing_ok=True)
                except OSError:
                    pass
                shutil.rmtree(job_dir, ignore_errors=True)
                removed += 1
                continue

            payload["_job_dir"] = str(job_dir)
            payload["_manifest_path"] = str(manifest_path)
            jobs.append(payload)

        except Exception:
            shutil.rmtree(job_dir, ignore_errors=True)
            removed += 1

    jobs.sort(
        key=lambda item: str(item.get("updated_at") or ""),
        reverse=True,
    )
    return jobs, removed


def count_valid_recovery_wavs(
    work_dir: Path,
    chunks_total: int,
) -> tuple[int, int, float, dict]:
    """
    Count only a consecutive valid prefix: chunk_00001.wav ... chunk_N.wav.
    Any broken/later stale files are removed so a resume cannot mix old data.
    """
    valid_count = 0
    total_bytes = 0
    total_audio_sec = 0.0
    first_sample: dict = {}

    for index in range(1, chunks_total + 1):
        path = work_dir / f"chunk_{index:05d}.wav"
        try:
            if not path.exists() or path.stat().st_size < 128:
                break
            info = get_wav_info(path)
            if not info or not info.get(
                "appears_complete",
                False,
            ):
                break
            valid_count = index
            total_bytes += int(path.stat().st_size)
            total_audio_sec += float(info.get("duration_sec") or 0)
            if not first_sample:
                first_sample = {
                    "sample_chunk_index": index,
                    **info,
                }
        except Exception:
            break

    for path in work_dir.glob("chunk_*.wav"):
        match = re.fullmatch(r"chunk_(\d{5})\.wav", path.name)
        if not match:
            continue
        if int(match.group(1)) > valid_count:
            try:
                path.unlink()
            except OSError:
                pass

    return valid_count, total_bytes, total_audio_sec, first_sample


# Windows virtual-key codes for the physical Latin shortcut keys.
# They remain the same when the keyboard layout is switched to Russian:
# Ctrl+C -> Ctrl+С, Ctrl+V -> Ctrl+М, Ctrl+X -> Ctrl+Ч, etc.
WINDOWS_CTRL_SHORTCUT_KEYCODES = {
    65: "select_all",  # A / Ф
    67: "copy",        # C / С
    86: "paste",       # V / М
    88: "cut",         # X / Ч
    89: "redo",        # Y / Н
    90: "undo",        # Z / Я
}


def ctrl_shortcut_action_from_keycode(keycode: int) -> str | None:
    """Return a layout-independent Ctrl shortcut action for Windows."""
    try:
        return WINDOWS_CTRL_SHORTCUT_KEYCODES.get(int(keycode))
    except Exception:
        return None


def handle_text_ctrl_shortcut(widget: tk.Text, event) -> str | None:
    """
    Handle Ctrl+C/V/X/A/Z/Y by physical Windows key code instead of keysym.

    Tk's standard Text bindings may depend on the active keyboard layout.
    With a Russian layout the physical V key becomes "м", C becomes "с", etc.,
    so Ctrl+V/C can stop matching the standard Latin shortcuts.  Windows
    virtual-key codes remain stable, so this handler works in both EN and RU.
    """
    action = ctrl_shortcut_action_from_keycode(
        getattr(event, "keycode", -1)
    )
    if action is None:
        return None

    try:
        if action == "copy":
            widget.event_generate("<<Copy>>")

        elif action == "paste":
            widget.event_generate("<<Paste>>")

        elif action == "cut":
            widget.event_generate("<<Cut>>")

        elif action == "select_all":
            widget.tag_add("sel", "1.0", "end-1c")
            widget.mark_set("insert", "1.0")
            widget.see("insert")

        elif action == "undo":
            try:
                widget.edit_undo()
            except tk.TclError:
                pass

        elif action == "redo":
            try:
                widget.edit_redo()
            except tk.TclError:
                pass

        # Stop Tk's standard class binding so English layout does not execute
        # the same operation a second time.
        return "break"

    except tk.TclError:
        return "break"


def app_code_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    try:
        return Path(__file__).resolve().parent
    except NameError:
        return Path.cwd()


def resolve_storage_dirs() -> tuple[Path, Path, Path]:
    """
    Primary location: next to the .py/.exe.
    Fallback: %LOCALAPPDATA%\\TextToMp3Irina if the primary location is read-only.
    """
    base = app_code_dir()
    settings_dir = base / SETTINGS_DIR_NAME
    logs_dir = base / LOGS_DIR_NAME

    try:
        settings_dir.mkdir(parents=True, exist_ok=True)
        logs_dir.mkdir(parents=True, exist_ok=True)
        probe = settings_dir / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return base, settings_dir, logs_dir
    except Exception:
        local = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
        fallback = local / "TextToMp3Irina"
        settings_dir = fallback / SETTINGS_DIR_NAME
        logs_dir = fallback / LOGS_DIR_NAME
        settings_dir.mkdir(parents=True, exist_ok=True)
        logs_dir.mkdir(parents=True, exist_ok=True)
        return fallback, settings_dir, logs_dir


STORAGE_BASE_DIR, SETTINGS_DIR, LOGS_DIR = resolve_storage_dirs()
SETTINGS_FILE = SETTINGS_DIR / SETTINGS_FILE_NAME
WORKSPACE_FILE = SETTINGS_DIR / WORKSPACE_FILE_NAME
WORKSPACE_TEXT_DIR = SETTINGS_DIR / WORKSPACE_TEXT_DIR_NAME
RECOVERY_DIR = STORAGE_BASE_DIR / RECOVERY_DIR_NAME
MP3_TEXT_BACKUPS_DIR = STORAGE_BASE_DIR / MP3_TEXT_BACKUPS_DIR_NAME


DEFAULT_SETTINGS = {
    "schema": 1,
    "voice": DEFAULT_VOICE_HINT,
    "rate": 0,
    "pitch": 0,
    "audio_output": DEFAULT_AUDIO_OUTPUT_LABEL,
    "volume": 100,
    "bitrate": "96k",
    "window_geometry": "1200x820",
    "last_input_dir": "",
    "last_output_dir": "",
    "global_copy_enabled": False,
    "global_copy_hotkey": "Ctrl+C",
    "autostart_windows": False,
}


def normalize_settings(raw: dict | None) -> dict:
    result = dict(DEFAULT_SETTINGS)
    if isinstance(raw, dict):
        result.update(raw)

    result["voice"] = str(result.get("voice") or DEFAULT_VOICE_HINT)
    result["rate"] = clamp_int(
        result.get("rate"),
        RATE_MIN,
        RATE_MAX,
        0,
    )
    result["pitch"] = clamp_int(
        result.get("pitch"),
        PITCH_MIN,
        PITCH_MAX,
        0,
    )
    result["audio_output"] = str(
        result.get("audio_output") or DEFAULT_AUDIO_OUTPUT_LABEL
    )
    result["volume"] = clamp_int(result.get("volume"), 0, 100, 100)

    bitrate = str(result.get("bitrate") or "96k")
    if bitrate not in {"64k", "96k", "128k", "160k", "192k"}:
        bitrate = "96k"
    result["bitrate"] = bitrate

    result["window_geometry"] = str(result.get("window_geometry") or "1200x820")
    result["last_input_dir"] = str(result.get("last_input_dir") or "")
    result["last_output_dir"] = str(result.get("last_output_dir") or "")
    result["global_copy_enabled"] = bool(
        result.get("global_copy_enabled", False)
    )
    result["global_copy_hotkey"] = str(
        result.get("global_copy_hotkey") or "Ctrl+C"
    ).strip() or "Ctrl+C"
    result["autostart_windows"] = bool(
        result.get("autostart_windows", False)
    )
    return result


def atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def cleanup_old_mp3_text_backups() -> tuple[int, int]:
    """Delete only app-owned MP3 text backups older than the retention limit."""
    MP3_TEXT_BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    cutoff_timestamp = (
        time.time()
        - MP3_TEXT_BACKUP_RETENTION_DAYS * 24 * 60 * 60
    )
    removed = 0
    failed = 0

    for path in MP3_TEXT_BACKUPS_DIR.glob(
        "mp3_text_*.txt"
    ):
        try:
            if (
                path.is_file()
                and path.stat().st_mtime < cutoff_timestamp
            ):
                path.unlink()
                removed += 1
        except OSError:
            failed += 1

    return removed, failed


def create_mp3_text_backup(
    run_id: str,
    tab_title: str,
    raw_text: str,
) -> Path:
    """Persist the exact raw text snapshot before its MP3 task starts."""
    # Repeat retention cleanup for long-running app sessions as well as at
    # startup, so a program left open for months still observes the limit.
    cleanup_old_mp3_text_backups()
    safe_title = re.sub(
        r'[<>:"/\\|?*]+',
        "_",
        str(tab_title or "").strip(),
    ).strip(" .")
    if not safe_title:
        safe_title = "Вкладка"
    safe_title = safe_title[:60].rstrip(" .") or "Вкладка"

    timestamp = datetime.now().strftime(
        "%Y%m%d_%H%M%S_%f"
    )
    run_suffix = re.sub(
        r"[^A-Za-z0-9_-]+",
        "_",
        str(run_id or "")[-24:],
    ).strip("_") or uuid.uuid4().hex[:8]
    backup_path = MP3_TEXT_BACKUPS_DIR / (
        f"mp3_text_{timestamp}_{run_suffix}_{safe_title}.txt"
    )
    atomic_write_text(backup_path, raw_text)
    return backup_path


def load_settings() -> dict:
    if not SETTINGS_FILE.exists():
        return dict(DEFAULT_SETTINGS)

    try:
        return normalize_settings(
            json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        )
    except Exception:
        return dict(DEFAULT_SETTINGS)


def ensure_help_files() -> None:
    SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    WORKSPACE_TEXT_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    RECOVERY_DIR.mkdir(parents=True, exist_ok=True)
    MP3_TEXT_BACKUPS_DIR.mkdir(parents=True, exist_ok=True)

    settings_readme = SETTINGS_DIR / "README.txt"
    settings_readme.write_text(
        "Эта папка хранит постоянные настройки и рабочие вкладки программы.\n\n"
        f"{SETTINGS_FILE_NAME} — общие настройки: голос, скорость, Pitch, громкость, "
        "качество MP3, размер окна и последние рабочие папки.\n"
        f"{WORKSPACE_FILE_NAME} — список открытых вкладок, их имена, параметры "
        "и сохранённая позиция прослушивания каждой вкладки.\n"
        f"{WORKSPACE_TEXT_DIR_NAME}\\*.txt — текст каждой вкладки.\n\n"
        f"Скорость едина для всех вкладок: {RATE_MIN}..{RATE_MAX}. "
        f"Pitch един для всех вкладок: {PITCH_MIN}..{PITCH_MAX}.\n"
        "Во время чтения кнопка «Вставить текст» добавляет текст в самый низ "
        "и не останавливает текущую фразу; добавленный хвост будет дочитан.\n"
        "Горячая клавиша чтения назначается отдельно каждой вкладке. Новая "
        "вкладка всегда создаётся со значением «Нет». Выделите текст в браузере, "
        "Telegram, Word или другом месте и нажмите клавишу конкретной вкладки: "
        "текст будет добавлен именно в эту вкладку, даже если на экране открыта "
        "другая вкладка. Перед добавлением остаются только русские буквы, цифры, "
        "пробелы, точки и запятые. Одинаковую горячую клавишу нельзя назначить "
        "двум вкладкам.\n"
        "Галочка «Сразу читать добавленный текст» сохраняется отдельно для каждой "
        "вкладки. Она определяет, запускать ли чтение автоматически после "
        "добавления горячей клавишей и включать ли новый фрагмент в уже идущую "
        "очередь чтения.\n"
        "Галочка «Удалять прочитанные предложения» сохраняется отдельно "
        "для каждой вкладки. Полностью прочитанные предложения удаляются, "
        "но одно последнее предложение временно остаётся для безопасного "
        "Pause → Continue на предложение назад.\n"
        "Во время создания MP3 горячая клавиша продолжает добавлять новый "
        "текст вниз. После успешной проверки MP3 программа удаляет только тот "
        "снимок текста, который вошёл в этот файл; добавленный позже текст "
        "остаётся. При отмене, ошибке или изменении исходного снимка текст не "
        "удаляется.\n"
        "Текст вкладок и пауза прослушивания сохраняются автоматически. "
        "После перезапуска кнопка «Продолжить» возвращает к сохранённой позиции.\n"
        "Галочка «Автозапуск с Windows» добавляет программу в автозапуск "
        "текущего пользователя Windows без прав администратора.\n",
        encoding="utf-8",
    )

    recovery_readme = RECOVERY_DIR / "README.txt"
    recovery_readme.write_text(
        "Эта папка нужна только для восстановления аварийно оборванных "
        "конвертаций MP3.\n"
        "Не удаляйте свежие папки job_..., если хотите продолжить задачу "
        "после сбоя или выключения компьютера.\n"
        "Внутри job_... временно хранится source_text.txt — точная копия "
        "текста этой конкретной конвертации. Это НЕ диагностический лог и "
        "после успешного завершения/отмены удаляется вместе с WAV.\n"
        f"Данные старше {RECOVERY_RETENTION_DAYS} дней программа удаляет "
        "автоматически.\n",
        encoding="utf-8",
    )

    backups_readme = MP3_TEXT_BACKUPS_DIR / "README.txt"
    backups_readme.write_text(
        "Эта папка хранит точные UTF-8 копии текста, отправленного на создание "
        "MP3. Новый файл mp3_text_*.txt создаётся перед запуском каждой "
        "конвертации.\n"
        f"Бэкапы хранятся {MP3_TEXT_BACKUP_RETENTION_DAYS} дней. Программа "
        "автоматически удаляет только собственные файлы mp3_text_*.txt старше "
        "этого срока; README.txt и другие файлы не удаляются.\n",
        encoding="utf-8",
    )

    logs_readme = LOGS_DIR / "КАК_АНАЛИЗИРОВАТЬ_ЛОГИ.txt"
    logs_readme.write_text(
        "ЛОГИ ДЛЯ АНАЛИЗА CHATGPT / CODEX\n"
        "=================================\n\n"
        "Для диагностики передайте нейросети последнюю папку session_.... целиком.\n\n"
        "1. diagnostic_summary.json — начать анализ с этого короткого файла.\n"
        "2. session.json — среда запуска, PID и активные/завершённые задачи.\n"
        "3. events*.jsonl — события, heartbeat и прогресс; при 2 МБ создаётся новый файл.\n"
        "4. task_<run_id>.json — итог конкретного запуска создания MP3.\n"
        "5. errors/*.json — traceback, этап сбоя и диагностический контекст.\n"
        "6. errors/*_failed_chunk.txt — только проблемный кусок текста.\n\n"
        "Весь исходный большой текст специально НЕ пишется в лог.\n"
        "Обычный успешный результат каждого фрагмента тоже НЕ логируется.\n"
        "tab_id — постоянный ID вкладки (сохраняется между перезапусками). "
        "Каждый запуск создания MP3 имеет отдельный run_id, а каждое "
        "прослушивание — preview_run_id. Они не зависят от видимого номера вкладки.\n"
        "Если предыдущий запуск оборвался аварийно, версия 2.9+ сохраняет "
        "готовые WAV в папке «Незавершённые задачи» и при следующем запуске "
        "предлагает безопасно продолжить с контрольной точки.\n"
        "diagnostic_summary.json содержит warnings, observations, статистику "
        "глобального копирования/автоудаления и краткую сводку последней "
        "успешной MP3-задачи.\n"
        "Удаление прочитанных предложений логируется пакетами примерно раз в "
        "15 секунд, а не отдельной строкой на каждое предложение.\n"
        "preview_finished содержит finish_reason: completed, user_stop, "
        "resume_rewind, another_tab, app_exit и другие причины.\n",
        encoding="utf-8",
    )


class ProblemLogger:
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

        self.event_file_index = 1
        self.events_file = self.session_dir / "events.jsonl"
        self.event_files = [self.events_file.name]

        self.started_perf = time.perf_counter()
        self.started_at = now_iso()
        self.last_live_summary_perf = self.started_perf

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
                atomic_write_json(session_file, payload)
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
            },
            "summary": {
                "tasks_started": tasks_started,
                "tasks_success": tasks_success,
                "tasks_failed": tasks_failed,
                "tasks_cancelled": tasks_cancelled,
                "active_tasks": active_tasks,
            },
        }

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

    def _write_diagnostic_summary(self, status: str) -> None:
        if self.disabled:
            return

        with self.summary_lock:
            with self.state_lock:
                active = {
                    task_id: dict(info)
                    for task_id, info in self.active_tasks.items()
                }
                stats_snapshot = dict(self.stats)
                completed_snapshot = [
                    dict(item)
                    for item in self.completed_tasks[-5:]
                ]
                last_success_snapshot = (
                    dict(self.last_successful_task)
                    if self.last_successful_task
                    else None
                )
                warnings_snapshot = self._warnings()
                observations_snapshot = self._observations()
                latest_text_state_snapshot = {
                    tab_id: dict(state)
                    for tab_id, state
                    in self.latest_text_state_by_tab.items()
                }
                source_counts_snapshot = dict(
                    self.global_copy_source_counts
                )

            capture_count = int(
                stats_snapshot.get("global_copy_captures") or 0
            )
            capture_latency_sum = float(
                stats_snapshot.get(
                    "global_copy_latency_ms_sum"
                )
                or 0
            )

            payload = {
                "schema": 2,
                "generated_at": now_iso(),
                "session_id": self.session_id,
                "session_status": status,
                "app_version": APP_VERSION,
                "environment": {
                    "pid": os.getpid(),
                    "python": sys.version.split()[0],
                    "pywin32_version": get_package_version("pywin32"),
                    "tkinter_tcl_version": str(tk.TclVersion),
                    "tkinter_tk_version": str(tk.TkVersion),
                    "platform": platform.platform(),
                },
                "stats": stats_snapshot,
                "listening": {
                    "global_copy": {
                        "captures": capture_count,
                        "captured_chars": int(
                            stats_snapshot.get(
                                "global_copy_captured_chars"
                            )
                            or 0
                        ),
                        "failures": int(
                            stats_snapshot.get(
                                "global_copy_failures"
                            )
                            or 0
                        ),
                        "duplicates_skipped": int(
                            stats_snapshot.get(
                                "global_copy_duplicates_skipped"
                            )
                            or 0
                        ),
                        "average_copy_latency_ms": (
                            round(
                                capture_latency_sum
                                / capture_count,
                                1,
                            )
                            if capture_count
                            else 0
                        ),
                        "max_copy_latency_ms": float(
                            stats_snapshot.get(
                                "global_copy_latency_ms_max"
                            )
                            or 0
                        ),
                        "source_process_counts": (
                            source_counts_snapshot
                        ),
                    },
                    "auto_delete": {
                        "batches": int(
                            stats_snapshot.get(
                                "read_deleted_batches"
                            )
                            or 0
                        ),
                        "segments": int(
                            stats_snapshot.get(
                                "read_deleted_segments"
                            )
                            or 0
                        ),
                        "chars": int(
                            stats_snapshot.get(
                                "read_deleted_chars"
                            )
                            or 0
                        ),
                    },
                    "appended_tail_continuations": int(
                        stats_snapshot.get(
                            "appended_tail_continuations"
                        )
                        or 0
                    ),
                    "pause_requests": int(
                        stats_snapshot.get(
                            "preview_pause_requests"
                        )
                        or 0
                    ),
                    "resume_requests": int(
                        stats_snapshot.get(
                            "preview_resume_requests"
                        )
                        or 0
                    ),
                    "finish_reasons": {
                        "completed": int(
                            stats_snapshot.get(
                                "preview_finish_completed"
                            )
                            or 0
                        ),
                        "user_stop": int(
                            stats_snapshot.get(
                                "preview_finish_user_stop"
                            )
                            or 0
                        ),
                        "resume_rewind": int(
                            stats_snapshot.get(
                                "preview_finish_resume_rewind"
                            )
                            or 0
                        ),
                        "another_tab": int(
                            stats_snapshot.get(
                                "preview_finish_another_tab"
                            )
                            or 0
                        ),
                        "app_exit": int(
                            stats_snapshot.get(
                                "preview_finish_app_exit"
                            )
                            or 0
                        ),
                        "other": int(
                            stats_snapshot.get(
                                "preview_finish_other"
                            )
                            or 0
                        ),
                    },
                    "latest_text_state_by_tab": (
                        latest_text_state_snapshot
                    ),
                },
                "tasks": {
                    "started": self.tasks_started,
                    "success": self.tasks_success,
                    "failed": self.tasks_failed,
                    "cancelled": self.tasks_cancelled,
                    "active": active,
                },
                "previous_unclean_sessions": (
                    self.previous_unclean_sessions
                ),
                "last_successful_task": last_success_snapshot,
                "recent_completed_tasks": completed_snapshot,
                "warnings": warnings_snapshot,
                "observations": observations_snapshot,
                "event_files": list(self.event_files),
                "how_to_analyze": [
                    "Сначала прочитать warnings, observations и "
                    "last_successful_task в этом diagnostic_summary.json.",
                    "Если warnings не пуст, найти соответствующие события "
                    "в events*.jsonl по run_id/preview_run_id/tab_id.",
                    "Для конкретного запуска MP3 открыть task_<run_id>.json.",
                    "При исключении открыть errors/*.json и failed_chunk, "
                    "если он есть.",
                    "Папка «Незавершённые задачи» не является логом: "
                    "она нужна для восстановления после сбоя.",
                ],
            }
            atomic_write_json(
                self.diagnostic_summary_file,
                payload,
            )

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
            if event_type == "error":
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

    def _write_session(self, status: str) -> None:
        if self.disabled:
            return

        with self.session_write_lock:
            atomic_write_json(
                self.session_file,
                self._session_payload(status),
            )

    def event(self, event_type: str, **fields) -> None:
        if self.disabled:
            return

        self._update_stats(event_type, fields)

        record = {"ts": now_iso(), "event": event_type, **fields}
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
                        self._write_diagnostic_summary(
                            "running"
                        )
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
            atomic_write_json(
                self.session_dir / f"task_{task_id}.json",
                summary,
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
            error_id = (
                datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                + "_"
                + uuid.uuid4().hex[:6]
            )
            json_path = self.errors_dir / f"{error_id}.json"

            payload = {
                "schema": 2,
                "ts": now_iso(),
                "error_id": error_id,
                "task_id": task_id,
                "run_id": context.get("run_id"),
                "tab_id": context.get("tab_id"),
                "preview_run_id": context.get(
                    "preview_run_id"
                ),
                "stage": stage,
                "exception_type": type(exc).__name__,
                "message": str(exc),
                "traceback": traceback_text,
                "context": context,
                "analysis_hints": [
                    "Сначала определить stage сбоя.",
                    "Прочитать diagnostic_summary.json.",
                    "Проверить последние task_progress этой task_id "
                    "в events*.jsonl.",
                    "Если есть sapi_retry — оценить стабильность голоса/SAPI.",
                    "Если stage=ffmpeg — проверить ffmpeg_error/context.",
                    "Если сохранён failed_chunk — проверить именно этот фрагмент.",
                ],
            }
            atomic_write_json(json_path, payload)

            if failed_chunk:
                failed_path = (
                    self.errors_dir
                    / f"{error_id}_failed_chunk.txt"
                )
                failed_path.write_text(
                    failed_chunk[: DEFAULT_CHUNK_SIZE + 500],
                    encoding="utf-8",
                )

            self.event(
                "error",
                task_id=task_id,
                run_id=context.get("run_id"),
                tab_id=context.get("tab_id"),
                preview_run_id=context.get(
                    "preview_run_id"
                ),
                stage=stage,
                error_id=error_id,
                exception_type=type(exc).__name__,
                message=str(exc)[:800],
            )
            self._write_diagnostic_summary("running")
            return str(json_path)
        except Exception:
            self.disabled = True
            return ""

    def close(self, status: str = "closed") -> None:
        if self.disabled:
            return

        self.event("app_exit", status=status)
        try:
            self._write_session(status)
            self._write_diagnostic_summary(status)
        except Exception:
            pass



def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u2028", "\n").replace("\u2029", "\n")
    text = unicodedata.normalize("NFKC", text)

    cleaned: list[str] = []
    for ch in text:
        if ch in "\n\t":
            cleaned.append(ch)
            continue

        category = unicodedata.category(ch)
        if category in {"Cc", "Cf", "Cs", "Co", "Cn"}:
            cleaned.append(" ")
        else:
            cleaned.append(ch)

    text = "".join(cleaned)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


def _split_oversized_piece(piece: str, max_chars: int) -> list[str]:
    result: list[str] = []
    remaining = piece.strip()

    while len(remaining) > max_chars:
        cut_from = max(1, int(max_chars * 0.55))
        window = remaining[: max_chars + 1]

        candidates = [
            window.rfind("\n", cut_from),
            window.rfind(". ", cut_from),
            window.rfind("! ", cut_from),
            window.rfind("? ", cut_from),
            window.rfind("; ", cut_from),
            window.rfind(", ", cut_from),
            window.rfind(" ", cut_from),
        ]

        cut = max(candidates)
        if cut <= 0:
            cut = max_chars
        elif remaining[cut:cut + 2] in {". ", "! ", "? ", "; ", ", "}:
            cut += 1

        part = remaining[:cut].strip()
        if part:
            result.append(part)
        remaining = remaining[cut:].strip()

    if remaining:
        result.append(remaining)

    return result


def split_text(text: str, max_chars: int = DEFAULT_CHUNK_SIZE) -> list[str]:
    text = normalize_text(text)
    if not text:
        return []

    rough_parts = re.split(r"(?<=[.!?…])\s+|\n{2,}", text)
    chunks: list[str] = []
    current = ""

    for raw in rough_parts:
        piece = raw.strip()
        if not piece:
            continue

        pieces = (
            _split_oversized_piece(piece, max_chars)
            if len(piece) > max_chars
            else [piece]
        )

        for item in pieces:
            candidate = f"{current} {item}".strip() if current else item
            if current and len(candidate) > max_chars:
                chunks.append(current.strip())
                current = item
            else:
                current = candidate

    if current.strip():
        chunks.append(current.strip())

    return chunks


def _split_sapi_xml_adjustment(value: int) -> list[int]:
    """
    Split a relative SAPI XML adjustment into standard -10..+10 steps.

    Example:
        24 -> [10, 10, 4]
        -20 -> [-10, -10]
    """
    value = int(value)
    parts: list[int] = []

    while value:
        step = clamp_int(
            value,
            SAPI_XML_ADJUSTMENT_MIN,
            SAPI_XML_ADJUSTMENT_MAX,
            0,
        )
        if step == 0:
            break
        parts.append(step)
        value -= step

    return parts


def split_extended_sapi_rate(rate: int) -> tuple[int, int]:
    """
    Return (native_rate, xml_relative_rate).

    The native SpVoice.Rate part always stays in the official -10..+10 range.
    Any extra requested speed is applied as relative XML rate adjustment.
    """
    requested = clamp_int(
        rate,
        RATE_MIN,
        RATE_MAX,
        0,
    )
    native = clamp_int(
        requested,
        SAPI_NATIVE_RATE_MIN,
        SAPI_NATIVE_RATE_MAX,
        0,
    )
    return native, requested - native


def build_sapi_text(
    text: str,
    pitch: int,
    rate_overflow: int = 0,
) -> tuple[str, int]:
    """
    Build SAPI XML while keeping each individual XML adjustment within
    the standard -10..+10 interval.

    Pitch is relative so values such as +24 can be represented as nested
    +10 +10 +4 adjustments instead of sending one out-of-range attribute.
    """
    pitch = clamp_int(
        pitch,
        PITCH_MIN,
        PITCH_MAX,
        0,
    )
    rate_overflow = clamp_int(
        rate_overflow,
        RATE_MIN,
        RATE_MAX,
        0,
    )

    pitch_steps = _split_sapi_xml_adjustment(pitch)
    rate_steps = _split_sapi_xml_adjustment(rate_overflow)

    if not pitch_steps and not rate_steps:
        return text, SVSF_DEFAULT

    wrapped = html.escape(text, quote=False)

    # Inner relative Pitch adjustments.
    for step in pitch_steps:
        wrapped = (
            f'<pitch middle="{step}">'
            f'{wrapped}'
            '</pitch>'
        )

    # Extra speed beyond the native SpVoice.Rate range.
    for step in rate_steps:
        wrapped = (
            f'<rate speed="{step}">'
            f'{wrapped}'
            '</rate>'
        )

    return wrapped, SVSF_IS_XML


def build_preview_segments(
    text: str,
    max_chars: int = 1200,
) -> list[dict]:
    """
    Split preview text into sentence/line-sized pieces while preserving exact
    character offsets in the original Tk Text content.

    Each item:
        {
            "start": int,
            "end": int,
            "text": str,
        }

    Offsets are used only for UI highlighting. The spoken text is normalized
    separately, so highlighting still points to the original user text.
    """
    if not text:
        return []

    segments: list[dict] = []

    # End a preview segment on:
    # - sentence punctuation followed by whitespace/end;
    # - line break;
    # - end of text.
    pattern = re.compile(
        r".+?(?:[.!?…]+(?=\s|$)|\n+|$)",
        re.DOTALL,
    )

    def append_trimmed(start: int, end: int) -> None:
        raw = text[start:end]
        if not raw:
            return

        left_trim = len(raw) - len(raw.lstrip())
        right_trim = len(raw) - len(raw.rstrip())

        seg_start = start + left_trim
        seg_end = end - right_trim

        if seg_end <= seg_start:
            return

        # Extremely long "sentences" are split near whitespace, while keeping
        # exact offsets for highlighting.
        cursor = seg_start
        while seg_end - cursor > max_chars:
            target_end = min(seg_end, cursor + max_chars)
            search_from = cursor + max(1, int(max_chars * 0.55))

            cut = text.rfind(" ", search_from, target_end + 1)
            if cut <= cursor:
                cut = text.rfind("\t", search_from, target_end + 1)
            if cut <= cursor:
                cut = target_end

            part_end = cut
            while part_end > cursor and text[part_end - 1].isspace():
                part_end -= 1

            if part_end > cursor:
                spoken = normalize_text(text[cursor:part_end])
                if spoken:
                    segments.append(
                        {
                            "start": cursor,
                            "end": part_end,
                            "text": spoken,
                        }
                    )

            cursor = cut
            while cursor < seg_end and text[cursor].isspace():
                cursor += 1

        if cursor < seg_end:
            spoken = normalize_text(text[cursor:seg_end])
            if spoken:
                segments.append(
                    {
                        "start": cursor,
                        "end": seg_end,
                        "text": spoken,
                    }
                )

    for match in pattern.finditer(text):
        append_trimmed(match.start(), match.end())

    # Regex can theoretically miss an unusual trailing fragment; keep a safe
    # fallback so preview never silently drops user text.
    if not segments and text.strip():
        first = len(text) - len(text.lstrip())
        last = len(text.rstrip())
        spoken = normalize_text(text[first:last])
        if spoken:
            segments.append(
                {
                    "start": first,
                    "end": last,
                    "text": spoken,
                }
            )

    return segments


def previous_preview_segment_start(
    text: str,
    current_segment_start: int,
) -> int:
    """
    Return the start offset of the preview segment immediately BEFORE the
    segment containing/currently starting at current_segment_start.

    This uses the exact same segmentation as preview playback/highlighting,
    so "one sentence back" means one visible/read preview sentence back.

    If the current segment is the first one, return the first segment start.
    """
    segments = build_preview_segments(text)
    if not segments:
        return 0

    try:
        current_offset = max(0, int(current_segment_start))
    except Exception:
        current_offset = 0

    current_index = 0

    for index, segment in enumerate(segments):
        start = int(segment.get("start") or 0)
        end = int(segment.get("end") or start)

        if start <= current_offset < max(start + 1, end):
            current_index = index
            break

        if current_offset <= start:
            current_index = index
            break

        current_index = index

    previous_index = max(0, current_index - 1)
    return int(segments[previous_index].get("start") or 0)


def read_text_file(path: Path) -> str:
    data = path.read_bytes()

    for encoding in ("utf-8-sig", "utf-16", "cp1251", "utf-8"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            pass

    return data.decode("utf-8", errors="replace")


def find_ffmpeg() -> str:
    executable = shutil.which("ffmpeg")
    if executable:
        return executable

    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        raise RuntimeError(
            "FFmpeg не найден.\n\n"
            "Установите зависимость:\n"
            "py -m pip install imageio-ffmpeg"
        ) from exc


def get_sapi_voices() -> list[str]:
    import pythoncom  # type: ignore
    import win32com.client  # type: ignore

    pythoncom.CoInitialize()
    try:
        voice = win32com.client.Dispatch("SAPI.SpVoice")
        tokens = voice.GetVoices()
        return [
            tokens.Item(index).GetDescription()
            for index in range(tokens.Count)
        ]
    finally:
        pythoncom.CoUninitialize()


def set_voice_by_description(sp_voice, description: str) -> None:
    tokens = sp_voice.GetVoices()
    wanted = description.strip().casefold()

    for index in range(tokens.Count):
        token = tokens.Item(index)
        if token.GetDescription().strip().casefold() == wanted:
            sp_voice.Voice = token
            return

    for index in range(tokens.Count):
        token = tokens.Item(index)
        if wanted and wanted in token.GetDescription().casefold():
            sp_voice.Voice = token
            return

    raise RuntimeError(f'Голос "{description}" не найден в Windows SAPI.')


def get_sapi_audio_outputs() -> tuple[list[str], str]:
    """Return SAPI playback devices and the output used by a new SpVoice."""
    import pythoncom  # type: ignore
    import win32com.client  # type: ignore

    pythoncom.CoInitialize()
    try:
        voice = win32com.client.Dispatch("SAPI.SpVoice")
        outputs = voice.GetAudioOutputs()

        descriptions: list[str] = []
        for index in range(outputs.Count):
            description = str(outputs.Item(index).GetDescription())
            if description and description not in descriptions:
                descriptions.append(description)

        current = ""
        try:
            current_token = voice.AudioOutput
            if current_token is not None:
                current = str(current_token.GetDescription())
        except Exception:
            current = ""

        return descriptions, current
    finally:
        pythoncom.CoUninitialize()


def audio_output_match_key(description: str) -> str:
    """
    Stable comparison key for SAPI endpoint descriptions.

    Windows can rename a Bluetooth/audio endpoint between boots, for example:
        Наушники (3- HUAWEI FreeBuds Pro 5)
        Наушники (4- HUAWEI FreeBuds Pro 5)

    The numeric instance prefix is not part of the physical device identity.
    Keep the endpoint type/name, but remove only that unstable number.
    """
    value = unicodedata.normalize(
        "NFKC",
        str(description or ""),
    ).strip().casefold()

    # "(3- Device)" / "(12 - Device)" -> "(Device)"
    value = re.sub(
        r"\(\s*\d+\s*-\s*",
        "(",
        value,
    )
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def resolve_audio_output_description(
    requested: str,
    available_descriptions: list[str],
) -> tuple[str | None, str]:
    """
    Resolve a saved SAPI endpoint to the description that exists right now.

    Returns (description, match_mode):
        exact       - exact case-insensitive match;
        stable_name - same endpoint after stripping Windows' changing number;
        missing     - preferred endpoint is currently unavailable.
    """
    requested = str(requested or "").strip()
    if not requested:
        return None, "missing"

    wanted = requested.casefold()
    for item in available_descriptions:
        if str(item).strip().casefold() == wanted:
            return str(item), "exact"

    wanted_key = audio_output_match_key(requested)
    if wanted_key:
        matches = [
            str(item)
            for item in available_descriptions
            if audio_output_match_key(str(item)) == wanted_key
        ]
        if len(matches) == 1:
            return matches[0], "stable_name"

    return None, "missing"


def audio_output_descriptions_equivalent(
    first: str,
    second: str,
) -> bool:
    first = str(first or "").strip()
    second = str(second or "").strip()
    if not first or not second:
        return False
    return (
        first.casefold() == second.casefold()
        or audio_output_match_key(first)
        == audio_output_match_key(second)
    )


def set_audio_output_by_description(sp_voice, description: str) -> str:
    """
    Select a concrete SAPI playback device.

    A saved concrete device is matched both exactly and by a stable description
    that ignores Windows' changing numeric endpoint prefix.  The preference is
    never silently replaced with the system default here.
    """
    requested = (description or "").strip()

    if not requested or requested == DEFAULT_AUDIO_OUTPUT_LABEL:
        try:
            current = sp_voice.AudioOutput
            if current is not None:
                return str(current.GetDescription())
        except Exception:
            pass
        return DEFAULT_AUDIO_OUTPUT_LABEL

    outputs = sp_voice.GetAudioOutputs()
    token_by_description: dict[str, object] = {}
    available: list[str] = []

    for index in range(outputs.Count):
        token = outputs.Item(index)
        actual_description = str(token.GetDescription())
        available.append(actual_description)
        token_by_description[actual_description] = token

    resolved, match_mode = resolve_audio_output_description(
        requested,
        available,
    )
    if resolved is None:
        raise RuntimeError(
            f'Устройство воспроизведения "{description}" сейчас не найдено '
            "в Windows SAPI. Подключите устройство и нажмите «↻ Обновить»."
        )

    token = token_by_description[resolved]
    sp_voice.AudioOutput = token

    try:
        current = sp_voice.AudioOutput
        if current is not None:
            return str(current.GetDescription())
    except Exception:
        pass

    return resolved


def synthesize_wav_once(
    text: str,
    wav_path: Path,
    voice_description: str,
    rate: int,
    pitch: int,
    volume: int,
) -> None:
    import pythoncom  # type: ignore
    import win32com.client  # type: ignore

    sp_voice = win32com.client.Dispatch("SAPI.SpVoice")
    set_voice_by_description(sp_voice, voice_description)

    native_rate, rate_overflow = split_extended_sapi_rate(rate)
    sp_voice.Rate = int(native_rate)
    sp_voice.Volume = int(volume)

    speak_text, speak_flags = build_sapi_text(
        text,
        pitch,
        rate_overflow,
    )

    stream = win32com.client.Dispatch("SAPI.SpFileStream")
    try:
        stream.Open(str(wav_path), SSFM_CREATE_FOR_WRITE, False)
        sp_voice.AudioOutputStream = stream
        sp_voice.Speak(speak_text, speak_flags)
    finally:
        try:
            stream.Close()
        except Exception:
            pass
        try:
            sp_voice.AudioOutputStream = None
        except Exception:
            pass

        del stream
        del sp_voice
        pythoncom.PumpWaitingMessages()


def preview_sapi_text(
    text: str,
    voice_description: str,
    audio_output_description: str,
    rate: int,
    pitch: int,
    volume: int,
    stop_event: threading.Event,
    pause_event: threading.Event,
    progress_callback=None,
    state_callback=None,
    heartbeat_callback=None,
    checkpoint_callback=None,
    segment_done_callback=None,
) -> dict:
    """
    Read text through one persistent SpVoice.

    Version 3.8 called Speak() once PER SENTENCE.  On some hardware endpoints
    (especially Bluetooth) that can sound like the loudness envelope is being
    restarted over and over.  Version 3.9 groups consecutive sentences into
    larger continuous SAPI streams and polls SpVoice.Status to preserve:
      - current-sentence highlighting;
      - pause/bookmark position;
      - delete-after-read callbacks;
      - Stop/Resume behavior.
    """
    import pythoncom  # type: ignore
    import win32com.client  # type: ignore

    pythoncom.CoInitialize()
    started = time.perf_counter()
    stopped = False
    completed_chunks = 0
    actual_audio_output = ""
    sapi_paused = False
    last_heartbeat = started

    segments = build_preview_segments(text)

    def build_blocks() -> list[dict]:
        blocks: list[dict] = []
        current_parts: list[str] = []
        current_segments: list[dict] = []
        current_chars = 0

        def flush() -> None:
            nonlocal current_parts, current_segments, current_chars
            if not current_segments:
                return
            blocks.append(
                {
                    "text": "".join(current_parts),
                    "segments": current_segments,
                }
            )
            current_parts = []
            current_segments = []
            current_chars = 0

        for global_index, segment in enumerate(segments, start=1):
            piece = str(segment.get("text") or "")
            separator = "" if not current_segments else " "

            if (
                current_segments
                and current_chars + len(separator) + len(piece)
                > PREVIEW_SAPI_BLOCK_MAX_CHARS
            ):
                flush()
                separator = ""

            spoken_start = current_chars + len(separator)
            current_parts.append(separator)
            current_parts.append(piece)
            current_chars = spoken_start + len(piece)

            current_segments.append(
                {
                    "global_index": global_index,
                    "spoken_start": spoken_start,
                    "spoken_end": current_chars,
                    "segment": segment,
                }
            )

        flush()
        return blocks

    blocks = build_blocks()

    def make_position_mapper(
        plain_text: str,
        speak_text: str,
        flags: int,
    ):
        if not (flags & SVSF_IS_XML):
            def direct(raw_position) -> int:
                try:
                    value = int(raw_position)
                except Exception:
                    return 0
                return max(0, min(len(plain_text), value))
            return direct

        escaped = html.escape(plain_text, quote=False)
        xml_text_start = speak_text.find(escaped)
        if xml_text_start < 0:
            def fallback(raw_position) -> int:
                try:
                    value = int(raw_position)
                except Exception:
                    return 0
                return max(0, min(len(plain_text), value))
            return fallback

        # escaped_boundaries[i] == offset in escaped XML text after i source chars.
        escaped_boundaries = [0]
        escaped_offset = 0
        for char in plain_text:
            escaped_offset += len(html.escape(char, quote=False))
            escaped_boundaries.append(escaped_offset)

        def mapped(raw_position) -> int:
            try:
                raw = int(raw_position)
            except Exception:
                return 0

            relative = raw - xml_text_start
            if relative <= 0:
                return 0
            if relative >= escaped_boundaries[-1]:
                return len(plain_text)

            source_offset = (
                bisect.bisect_right(
                    escaped_boundaries,
                    relative,
                )
                - 1
            )
            return max(
                0,
                min(len(plain_text), source_offset),
            )

        return mapped

    def locate_segment(block: dict, plain_position: int) -> dict:
        metas = block["segments"]
        if not metas:
            raise RuntimeError("Пустой preview-блок.")

        position = max(0, int(plain_position))
        for meta in metas:
            if position < int(meta["spoken_end"]):
                return meta
        return metas[-1]

    def maybe_heartbeat(
        segment_index: int,
        segments_total: int,
    ) -> None:
        nonlocal last_heartbeat
        if heartbeat_callback is None:
            return
        now_perf = time.perf_counter()
        if (
            now_perf - last_heartbeat
            < PREVIEW_HEARTBEAT_INTERVAL_SEC
        ):
            return
        heartbeat_callback(
            {
                "segment": segment_index,
                "segments_total": segments_total,
                "elapsed_sec": round(
                    now_perf - started,
                    3,
                ),
                "paused": bool(
                    pause_event.is_set()
                    or sapi_paused
                ),
            }
        )
        last_heartbeat = now_perf

    def emit_checkpoint(
        meta: dict,
        *,
        reason: str,
        word_position: int = 0,
        word_length: int = 0,
        sapi_position_raw: int | None = None,
    ) -> None:
        if checkpoint_callback is None:
            return

        segment = meta["segment"]
        checkpoint_callback(
            {
                "segment": int(meta["global_index"]),
                "segments_total": len(segments),
                "start": int(segment.get("start") or 0),
                "end": int(segment.get("end") or 0),
                "word_position": max(0, int(word_position)),
                "word_length": max(0, int(word_length)),
                "sapi_position_raw": sapi_position_raw,
                "reason": reason,
            }
        )

    def emit_progress(meta: dict) -> None:
        if progress_callback is None:
            return
        segment = meta["segment"]
        progress_callback(
            {
                "index": int(meta["global_index"]),
                "total": len(segments),
                "start": int(segment["start"]),
                "end": int(segment["end"]),
                "text": str(segment["text"]),
            }
        )

    def emit_segment_done(meta: dict) -> None:
        nonlocal completed_chunks
        completed_chunks += 1
        if segment_done_callback is None:
            return
        segment = meta["segment"]
        segment_done_callback(
            {
                "index": int(meta["global_index"]),
                "total": len(segments),
                "start": int(segment["start"]),
                "end": int(segment["end"]),
                "text": str(segment["text"]),
            }
        )

    try:
        voice = win32com.client.Dispatch("SAPI.SpVoice")
        set_voice_by_description(voice, voice_description)
        actual_audio_output = set_audio_output_by_description(
            voice,
            audio_output_description,
        )

        native_rate, rate_overflow = split_extended_sapi_rate(rate)
        voice.Rate = int(native_rate)
        voice.Volume = int(volume)

        if not segments:
            return {
                "requested_audio_output": audio_output_description,
                "actual_audio_output": actual_audio_output,
                "chunks": 0,
                "completed_chunks": 0,
                "sapi_stream_blocks": 0,
                "stopped": False,
                "paused_at_finish": bool(pause_event.is_set()),
                "duration_sec": round(
                    time.perf_counter() - started,
                    3,
                ),
            }

        for block in blocks:
            if stop_event.is_set():
                stopped = True
                break

            first_meta = block["segments"][0]

            # Pause exactly between large SAPI streams.
            if pause_event.is_set() and not stop_event.is_set():
                emit_checkpoint(
                    first_meta,
                    reason="pause_between_stream_blocks",
                )
                if state_callback is not None:
                    state_callback("paused")
                while (
                    pause_event.is_set()
                    and not stop_event.is_set()
                ):
                    maybe_heartbeat(
                        int(first_meta["global_index"]),
                        len(segments),
                    )
                    time.sleep(0.05)

            if stop_event.is_set():
                stopped = True
                break

            if state_callback is not None:
                state_callback("running")

            block_text = str(block["text"])
            speak_text, flags = build_sapi_text(
                block_text,
                pitch,
                rate_overflow,
            )
            map_position = make_position_mapper(
                block_text,
                speak_text,
                flags,
            )

            # Reassert the requested level before every large stream.  The voice
            # object itself remains the same for the whole reading session.
            voice.Volume = int(volume)

            last_active_global = None
            done_local_count = 0
            active_meta = first_meta
            emit_progress(active_meta)
            last_active_global = int(active_meta["global_index"])

            voice.Speak(
                speak_text,
                flags | SVS_FLAGS_ASYNC,
            )

            while True:
                raw_word_position = None
                raw_sentence_position = None
                word_length = 0

                try:
                    status = voice.Status
                    raw_word_position = int(
                        getattr(
                            status,
                            "InputWordPosition",
                            0,
                        )
                        or 0
                    )
                    raw_sentence_position = int(
                        getattr(
                            status,
                            "InputSentencePosition",
                            raw_word_position,
                        )
                        or raw_word_position
                    )
                    word_length = max(
                        0,
                        int(
                            getattr(
                                status,
                                "InputWordLength",
                                0,
                            )
                            or 0
                        ),
                    )

                    plain_word_position = map_position(
                        raw_word_position
                    )
                    plain_sentence_position = map_position(
                        raw_sentence_position
                    )

                    # Word position normally gives the best live location.
                    # Sentence position is a safe fallback at boundaries.
                    live_plain_position = plain_word_position
                    if (
                        live_plain_position <= 0
                        and plain_sentence_position > 0
                    ):
                        live_plain_position = (
                            plain_sentence_position
                        )

                    active_meta = locate_segment(
                        block,
                        live_plain_position,
                    )
                    active_global = int(
                        active_meta["global_index"]
                    )

                    # Entering sentence N means every earlier sentence in this
                    # stream has finished and can be deleted/logged safely.
                    active_local_index = block["segments"].index(
                        active_meta
                    )
                    while done_local_count < active_local_index:
                        emit_segment_done(
                            block["segments"][
                                done_local_count
                            ]
                        )
                        done_local_count += 1

                    if active_global != last_active_global:
                        emit_progress(active_meta)
                        last_active_global = active_global

                except Exception:
                    # Status polling is diagnostic/UI assistance.  A temporary
                    # status failure must never stop actual speech.
                    pass

                if stop_event.is_set():
                    stopped = True
                    try:
                        if sapi_paused:
                            voice.Resume()
                            sapi_paused = False
                        voice.Speak(
                            "",
                            SVSFPURGE_BEFORE_SPEAK
                            | SVS_FLAGS_ASYNC,
                        )
                    except Exception:
                        pass
                    break

                if pause_event.is_set() and not sapi_paused:
                    local_word_position = 0

                    try:
                        # Refresh once immediately before Pause so the persisted
                        # bookmark is as close as possible to the audible word.
                        status = voice.Status
                        raw_word_position = int(
                            getattr(
                                status,
                                "InputWordPosition",
                                0,
                            )
                            or 0
                        )
                        word_length = max(
                            0,
                            int(
                                getattr(
                                    status,
                                    "InputWordLength",
                                    0,
                                )
                                or 0
                            ),
                        )
                        plain_word_position = map_position(
                            raw_word_position
                        )
                        active_meta = locate_segment(
                            block,
                            plain_word_position,
                        )
                        local_word_position = max(
                            0,
                            plain_word_position
                            - int(
                                active_meta[
                                    "spoken_start"
                                ]
                            ),
                        )

                        segment_text = str(
                            active_meta["segment"].get(
                                "text"
                            )
                            or ""
                        )
                        local_word_position = min(
                            len(segment_text),
                            local_word_position,
                        )
                        if word_length > len(segment_text):
                            word_length = 0
                    except Exception:
                        raw_word_position = None
                        word_length = 0
                        local_word_position = 0

                    voice.Pause()
                    sapi_paused = True

                    emit_checkpoint(
                        active_meta,
                        reason="pause_inside_stream_block",
                        word_position=local_word_position,
                        word_length=word_length,
                        sapi_position_raw=raw_word_position,
                    )
                    if state_callback is not None:
                        state_callback("paused")

                elif not pause_event.is_set() and sapi_paused:
                    voice.Resume()
                    sapi_paused = False
                    if state_callback is not None:
                        state_callback("running")

                if not sapi_paused and bool(
                    voice.WaitUntilDone(40)
                ):
                    # Anything not observed by status polling is definitely done
                    # once this entire stream reports completion.
                    while done_local_count < len(
                        block["segments"]
                    ):
                        emit_segment_done(
                            block["segments"][
                                done_local_count
                            ]
                        )
                        done_local_count += 1
                    break

                maybe_heartbeat(
                    int(active_meta["global_index"]),
                    len(segments),
                )
                pythoncom.PumpWaitingMessages()
                if sapi_paused:
                    time.sleep(0.03)

            if stopped:
                break

        return {
            "requested_audio_output": audio_output_description,
            "actual_audio_output": actual_audio_output,
            "chunks": len(segments),
            "completed_chunks": completed_chunks,
            "sapi_stream_blocks": len(blocks),
            "stopped": stopped,
            "paused_at_finish": bool(pause_event.is_set()),
            "duration_sec": round(
                time.perf_counter() - started,
                3,
            ),
        }
    finally:
        pythoncom.CoUninitialize()


class FfmpegError(RuntimeError):
    def __init__(self, message: str, stderr: str = "") -> None:
        super().__init__(message)
        self.stderr = stderr


def _parse_ffmpeg_clock(value: str) -> float | None:
    try:
        hours, minutes, seconds = value.strip().split(":")
        return (
            float(hours) * 3600
            + float(minutes) * 60
            + float(seconds)
        )
    except Exception:
        return None


def run_ffmpeg_concat(
    ffmpeg: str,
    wav_files: list[Path],
    output_mp3: Path,
    bitrate: str,
    work_dir: Path,
    cancel_event: threading.Event | None = None,
    progress_callback=None,
) -> dict:
    """
    Concatenate WAV files and encode MP3 with live progress and cancellation.

    FFmpeg runs through Popen instead of blocking subprocess.run, so the user
    can cancel during the final encoding stage as well.
    """
    concat_file = work_dir / "concat.txt"

    def ffmpeg_escape(path: Path) -> str:
        value = path.resolve().as_posix().replace("'", r"'\''")
        return f"file '{value}'"

    concat_file.write_text(
        "\n".join(ffmpeg_escape(path) for path in wav_files),
        encoding="utf-8",
    )

    temporary_output = output_mp3.with_name(
        output_mp3.stem + ".part.mp3"
    )
    temporary_output.unlink(missing_ok=True)

    total_audio_sec = wav_total_duration_seconds(wav_files)

    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_file),
        "-vn",
        "-c:a",
        "libmp3lame",
        "-b:a",
        bitrate,
        "-progress",
        "pipe:1",
        "-nostats",
        str(temporary_output),
    ]

    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]

    started = time.perf_counter()
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=creationflags,
    )

    line_queue: queue.Queue[tuple[str, str]] = queue.Queue()
    stderr_lines: list[str] = []

    def reader(stream, source: str) -> None:
        try:
            if stream is None:
                return
            for line in iter(stream.readline, ""):
                line_queue.put((source, line.rstrip("\r\n")))
        finally:
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass

    stdout_thread = threading.Thread(
        target=reader,
        args=(process.stdout, "stdout"),
        name="ffmpeg-stdout-reader",
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=reader,
        args=(process.stderr, "stderr"),
        name="ffmpeg-stderr-reader",
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()

    last_out_time_sec = 0.0
    last_callback_at = 0.0
    cancelled = False

    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                try:
                    process.terminate()
                    process.wait(
                        timeout=FFMPEG_TERMINATE_TIMEOUT_SEC
                    )
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
                except Exception:
                    try:
                        process.kill()
                    except Exception:
                        pass
                break

            while True:
                try:
                    source, line = line_queue.get_nowait()
                except queue.Empty:
                    break

                if source == "stderr":
                    if line:
                        stderr_lines.append(line)
                        if len(stderr_lines) > 300:
                            stderr_lines = stderr_lines[-300:]
                    continue

                if "=" not in line:
                    continue

                key, value = line.split("=", 1)
                parsed_time = None

                if key in {"out_time_us", "out_time_ms"}:
                    try:
                        parsed_time = float(value) / 1_000_000.0
                    except Exception:
                        parsed_time = None
                elif key == "out_time":
                    parsed_time = _parse_ffmpeg_clock(value)

                if parsed_time is not None:
                    last_out_time_sec = max(
                        last_out_time_sec,
                        parsed_time,
                    )

            now = time.perf_counter()
            if (
                progress_callback is not None
                and now - last_callback_at >= 0.5
            ):
                fraction = None
                if total_audio_sec > 0:
                    fraction = max(
                        0.0,
                        min(
                            1.0,
                            last_out_time_sec / total_audio_sec,
                        ),
                    )

                try:
                    part_size = (
                        temporary_output.stat().st_size
                        if temporary_output.exists()
                        else 0
                    )
                except OSError:
                    part_size = 0

                progress_callback(
                    {
                        "fraction": fraction,
                        "out_time_sec": round(
                            last_out_time_sec,
                            3,
                        ),
                        "total_audio_sec": round(
                            total_audio_sec,
                            3,
                        ),
                        "elapsed_sec": round(
                            now - started,
                            3,
                        ),
                        "output_part_bytes": part_size,
                    }
                )
                last_callback_at = now

            return_code = process.poll()
            if return_code is not None:
                break

            time.sleep(0.05)

        # Drain final output after process termination.
        deadline = time.perf_counter() + 1.0
        while time.perf_counter() < deadline:
            drained = False
            while True:
                try:
                    source, line = line_queue.get_nowait()
                except queue.Empty:
                    break
                drained = True
                if source == "stderr" and line:
                    stderr_lines.append(line)
            if not stdout_thread.is_alive() and not stderr_thread.is_alive():
                break
            if not drained:
                time.sleep(0.02)

        elapsed = time.perf_counter() - started

        if cancelled:
            temporary_output.unlink(missing_ok=True)
            raise InterruptedError(
                "Создание MP3 остановлено пользователем во время FFmpeg."
            )

        return_code = process.returncode
        stderr = "\n".join(stderr_lines)[-6000:].strip()

        if return_code != 0:
            temporary_output.unlink(missing_ok=True)
            raise FfmpegError(
                f"FFmpeg не смог создать MP3. Код выхода: {return_code}",
                stderr=stderr,
            )

        if (
            not temporary_output.exists()
            or temporary_output.stat().st_size == 0
        ):
            temporary_output.unlink(missing_ok=True)
            raise FfmpegError(
                "FFmpeg завершился без ошибки, но временный MP3 пуст или не создан.",
                stderr=stderr,
            )

        if progress_callback is not None:
            try:
                progress_callback(
                    {
                        "fraction": 1.0,
                        "out_time_sec": round(
                            total_audio_sec,
                            3,
                        ),
                        "total_audio_sec": round(
                            total_audio_sec,
                            3,
                        ),
                        "elapsed_sec": round(
                            elapsed,
                            3,
                        ),
                        "output_part_bytes": (
                            temporary_output.stat().st_size
                        ),
                    }
                )
            except Exception:
                pass

        os.replace(temporary_output, output_mp3)

        return {
            "duration_sec": round(elapsed, 3),
            "audio_duration_sec": round(total_audio_sec, 3),
            "stderr_tail": stderr,
        }

    finally:
        if process.poll() is None:
            try:
                process.kill()
            except Exception:
                pass




def windows_autostart_command() -> str:
    """
    Команда, которую Windows запускает при входе текущего пользователя.

    EXE запускается напрямую. Для .py/.pyw используется pythonw.exe, если он
    установлен рядом с текущим Python, чтобы при автозапуске не появлялось
    консольное окно.
    """
    if os.name != "nt":
        return ""

    if getattr(sys, "frozen", False):
        arguments = [str(Path(sys.executable).resolve())]
    else:
        try:
            script_path = Path(__file__).resolve()
        except NameError:
            script_path = Path(sys.argv[0]).resolve()

        python_executable = Path(sys.executable).resolve()
        if python_executable.name.casefold() in {
            "python.exe",
            "python_d.exe",
        }:
            pythonw = python_executable.with_name("pythonw.exe")
            if pythonw.exists():
                python_executable = pythonw

        arguments = [
            str(python_executable),
            str(script_path),
        ]

    return subprocess.list2cmdline(arguments)


def windows_autostart_is_enabled() -> bool:
    """Проверить фактическое наличие записи программы в HKCU\\...\\Run."""
    if os.name != "nt":
        return False

    try:
        import winreg
    except Exception:
        return False

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            AUTOSTART_REG_PATH,
            0,
            winreg.KEY_READ,
        ) as key:
            value, _value_type = winreg.QueryValueEx(
                key,
                AUTOSTART_REG_VALUE,
            )
        return bool(str(value or "").strip())
    except FileNotFoundError:
        return False
    except OSError:
        return False


def set_windows_autostart(enabled: bool) -> str:
    """
    Включить/выключить автозапуск для текущего пользователя.

    Используется HKCU, поэтому права администратора не требуются.
    Возвращает записанную команду при включении и пустую строку при выключении.
    """
    if os.name != "nt":
        raise RuntimeError(
            "Автозапуск этой программы поддерживается только в Windows."
        )

    import winreg

    if enabled:
        command = windows_autostart_command()
        if not command:
            raise RuntimeError(
                "Не удалось определить команду запуска программы."
            )

        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            AUTOSTART_REG_PATH,
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(
                key,
                AUTOSTART_REG_VALUE,
                0,
                winreg.REG_SZ,
                command,
            )
        return command

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            AUTOSTART_REG_PATH,
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            try:
                winreg.DeleteValue(
                    key,
                    AUTOSTART_REG_VALUE,
                )
            except FileNotFoundError:
                pass
    except FileNotFoundError:
        pass

    return ""


def open_folder(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)

    if os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


class GlobalCopyHotkeyMonitor:
    """Глобальный захват выделенного текста для одной назначенной вкладки.

    Принцип работы:
    1. Пользователь выделяет текст в Telegram, браузере, Word или любом другом месте.
    2. Нажимает выбранную горячую клавишу.
    3. Если это не обычное Ctrl+C, программа сама отправляет активному окну Ctrl+C.
    4. Через небольшую паузу программа забирает свежий текст из буфера обмена
       и передаёт его в активную вкладку программы для чтения.
    """

    PRESET_HOTKEYS = (
        "F8",
        "F9",
        "Q+W",
        "Й+Ц",
        "Ctrl+C",
        "Ctrl+Shift+C",
        "Ctrl+Alt+C",
        "Alt+C",
        "Ctrl+Alt+X",
        "F10",
        "F12",
    )

    MODIFIER_ORDER = ("CTRL", "SHIFT", "ALT")

    # Русская раскладка: пользователь может писать Й+Ц, Ф+Ы и т.д.
    # Внутри Windows это те же физические клавиши Q+W, A+S и т.д.
    CYRILLIC_TO_LATIN = {
        "Й": "Q", "Ц": "W", "У": "E", "К": "R", "Е": "T", "Н": "Y", "Г": "U", "Ш": "I", "Щ": "O", "З": "P",
        "Ф": "A", "Ы": "S", "В": "D", "А": "F", "П": "G", "Р": "H", "О": "J", "Л": "K", "Д": "L",
        "Я": "Z", "Ч": "X", "С": "C", "М": "V", "И": "B", "Т": "N", "Ь": "M",
    }

    VK_BY_NAME = {
        **{chr(code): code for code in range(ord("A"), ord("Z") + 1)},
        **{str(num): ord(str(num)) for num in range(10)},
        **{f"F{num}": 0x6F + num for num in range(1, 25)},
        "SPACE": 0x20,
        "ПРОБЕЛ": 0x20,
        "TAB": 0x09,
        "ENTER": 0x0D,
        "ESC": 0x1B,
        "ESCAPE": 0x1B,
        "INSERT": 0x2D,
        "INS": 0x2D,
        "HOME": 0x24,
        "END": 0x23,
        "PAGEUP": 0x21,
        "PAGEDOWN": 0x22,
    }

    def __init__(self, root, callback, hotkey="Ctrl+C"):
        self.root = root
        self.callback = callback
        self.enabled = False
        self.is_windows = (os.name == "nt")
        self._stop_event = threading.Event()
        self._events = queue.Queue()
        self._thread = None
        self._thread_id = None
        self._hook_id = None
        self._hook_callback = None
        self._local_binding_sequence = None
        self._last_trigger_time = 0
        self._last_text = None
        self._last_text_time = 0
        self._pressed_vks = set()
        self._last_trigger_context: dict = {}
        self._last_capture_info: dict = {}
        try:
            self._root_hwnd = int(self.root.winfo_id())
        except Exception:
            self._root_hwnd = None

        self.hotkey_text = "Ctrl+C"
        self.hotkey_modifiers = {"CTRL"}
        self.hotkey_main_keys = ("C",)
        self.hotkey_main = "C"          # оставлено для совместимости со старой логикой
        self.hotkey_vks = {self.VK_BY_NAME["C"]}
        self.hotkey_vk = self.VK_BY_NAME["C"]
        self.set_hotkey(hotkey)

        # Очередь нужна, чтобы обработчик клавиатуры не трогал Tkinter напрямую.
        self.root.after(100, self._poll_events)

        if self.is_windows:
            self._thread = threading.Thread(target=self._windows_keyboard_hook_loop, daemon=True)
            self._thread.start()

    # ─────────────────────────────────────────────────────────────────────
    # Настройка горячей клавиши
    # ─────────────────────────────────────────────────────────────────────

    @classmethod
    def parse_hotkey(cls, hotkey):
        raw = (hotkey or "").strip()
        if not raw:
            raise ValueError("Введите горячую клавишу, например F8, Q+W или Ctrl+Shift+C.")

        prepared = (
            raw.replace("Control", "Ctrl")
               .replace("CONTROL", "Ctrl")
               .replace("control", "Ctrl")
               .replace("контрол", "Ctrl")
               .replace("кнтрл", "Ctrl")
               .replace("альт", "Alt")
               .replace("шифт", "Shift")
               .replace(" ", "")
        )
        parts = [p for p in prepared.split("+") if p]
        if not parts:
            raise ValueError("Не удалось прочитать горячую клавишу.")

        modifiers = set()
        main_keys = []

        aliases = {
            "CTRL": "CTRL",
            "CONTROL": "CTRL",
            "SHIFT": "SHIFT",
            "ALT": "ALT",
            "OPTION": "ALT",
        }

        for part in parts:
            token = part.upper()
            token = cls.CYRILLIC_TO_LATIN.get(token, token)
            if token in aliases:
                modifiers.add(aliases[token])
                continue

            if token not in cls.VK_BY_NAME:
                raise ValueError(
                    "Эта клавиша пока не поддерживается. Используйте буквы, цифры, F1-F24 "
                    "или сочетания вроде Q+W, Й+Ц, Ctrl+Alt+X."
                )
            if token not in main_keys:
                main_keys.append(token)

        if not main_keys:
            raise ValueError("Добавьте основную клавишу: F8, Q+W, C, X и т.д.")

        if len(main_keys) > 3:
            raise ValueError("В горячей клавише можно использовать максимум 3 основные клавиши.")

        normalized_parts = []
        for mod in cls.MODIFIER_ORDER:
            if mod in modifiers:
                normalized_parts.append({"CTRL": "Ctrl", "SHIFT": "Shift", "ALT": "Alt"}[mod])

        def display_key(key):
            if key.startswith("F") and key[1:].isdigit():
                return key
            if key == "SPACE":
                return "Space"
            if key == "TAB":
                return "Tab"
            if key == "ENTER":
                return "Enter"
            return key

        normalized_parts.extend(display_key(k) for k in main_keys)
        normalized = "+".join(normalized_parts)

        return {
            "text": normalized,
            "modifiers": modifiers,
            "main_keys": tuple(main_keys),
            "vks": {cls.VK_BY_NAME[k] for k in main_keys},
        }

    def set_hotkey(self, hotkey):
        parsed = self.parse_hotkey(hotkey)
        self.hotkey_text = parsed["text"]
        self.hotkey_modifiers = parsed["modifiers"]
        self.hotkey_main_keys = parsed["main_keys"]
        self.hotkey_main = self.hotkey_main_keys[0]
        self.hotkey_vks = parsed["vks"]
        self.hotkey_vk = next(iter(self.hotkey_vks))
        self._pressed_vks.clear()

        if not self.is_windows and self.enabled:
            self._enable_local_binding()

        return self.hotkey_text

    def get_hotkey(self):
        return self.hotkey_text

    def enable(self):
        self.enabled = True
        if not self.is_windows:
            self._enable_local_binding()

    def disable(self):
        self.enabled = False
        if not self.is_windows:
            self._disable_local_binding()

    def stop(self):
        self.disable()
        self._stop_event.set()
        if self.is_windows and self._thread_id:
            try:
                import ctypes
                user32 = ctypes.windll.user32
                WM_QUIT = 0x0012
                user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            except Exception:
                pass

    def _is_our_app_focused(self):
        """Нужно, чтобы глобальная автовставка не мешала обычному Ctrl+C/Ctrl+V внутри программы."""
        try:
            return self.root.focus_get() is not None
        except Exception:
            return False

    def _clipboard_sequence_number(self) -> int | None:
        if not self.is_windows:
            return None
        try:
            return int(
                ctypes.windll.user32.GetClipboardSequenceNumber()
            )
        except Exception:
            return None

    @staticmethod
    def _process_name_from_pid(pid: int) -> str:
        if os.name != "nt" or not pid:
            return ""
        try:
            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.OpenProcess.argtypes = [
                ctypes.c_ulong,
                ctypes.c_int,
                ctypes.c_ulong,
            ]
            kernel32.QueryFullProcessImageNameW.argtypes = [
                ctypes.c_void_p,
                ctypes.c_ulong,
                ctypes.POINTER(ctypes.c_wchar),
                ctypes.POINTER(ctypes.c_ulong),
            ]
            kernel32.QueryFullProcessImageNameW.restype = ctypes.c_int
            kernel32.CloseHandle.argtypes = [
                ctypes.c_void_p,
            ]

            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION,
                False,
                int(pid),
            )
            if not handle:
                return ""
            try:
                size = ctypes.c_ulong(32768)
                buffer = ctypes.create_unicode_buffer(
                    size.value
                )
                ok = kernel32.QueryFullProcessImageNameW(
                    handle,
                    0,
                    buffer,
                    ctypes.byref(size),
                )
                if not ok:
                    return ""
                return Path(buffer.value).name
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return ""

    def _foreground_capture_context(self) -> dict:
        # Keep this method very fast because it can run inside WH_KEYBOARD_LL.
        # Process-name resolution is deferred to the Tk thread.
        if not self.is_windows:
            return {
                "source_process": "",
                "source_pid": None,
                "clipboard_sequence_before": None,
                "trigger_perf": time.perf_counter(),
            }

        try:
            user32 = ctypes.windll.user32
            hwnd = int(user32.GetForegroundWindow() or 0)
            pid = ctypes.c_ulong()
            if hwnd:
                user32.GetWindowThreadProcessId(
                    hwnd,
                    ctypes.byref(pid),
                )
            pid_value = int(pid.value or 0)
            return {
                "source_process": "",
                "source_pid": pid_value or None,
                "clipboard_sequence_before": (
                    self._clipboard_sequence_number()
                ),
                "trigger_perf": time.perf_counter(),
            }
        except Exception:
            return {
                "source_process": "",
                "source_pid": None,
                "clipboard_sequence_before": (
                    self._clipboard_sequence_number()
                ),
                "trigger_perf": time.perf_counter(),
            }

    # ─────────────────────────────────────────────────────────────────────
    # Выполнение автокопирования и вставки в программу
    # ─────────────────────────────────────────────────────────────────────

    def _poll_events(self):
        try:
            while True:
                trigger_context = self._events.get_nowait()
                if not isinstance(trigger_context, dict):
                    trigger_context = dict(
                        self._last_trigger_context
                    )

                # A short delay only lets the physical hotkey keys come up.
                # Clipboard readiness itself is detected by sequence number.
                self.root.after(
                    70,
                    lambda context=dict(trigger_context):
                        self._copy_selection_then_run_callback(
                            context
                        ),
                )
        except queue.Empty:
            pass

        if not self._stop_event.is_set():
            self.root.after(100, self._poll_events)

    def _copy_selection_then_run_callback(
        self,
        trigger_context: dict | None = None,
    ):
        if not self.enabled:
            return

        # Если фокус внутри самой программы, не делаем автовставку —
        # так обычное копирование/вставка в полях программы не ломается.
        if self._is_our_app_focused():
            return

        context = dict(trigger_context or {})
        if (
            not context.get("source_process")
            and context.get("source_pid")
        ):
            context["source_process"] = (
                self._process_name_from_pid(
                    int(context["source_pid"])
                )
            )

        if "clipboard_sequence_before" not in context:
            context["clipboard_sequence_before"] = (
                self._clipboard_sequence_number()
            )
        if "trigger_perf" not in context:
            context["trigger_perf"] = time.perf_counter()

        # Для обычного Ctrl+C копирование уже сделал Telegram/браузер/другая программа.
        # Для других сочетаний программа сама отправляет Ctrl+C активному окну.
        if not self._hotkey_is_plain_ctrl_c():
            self._send_ctrl_c_to_active_window()

        self._wait_for_clipboard_change(
            context,
            deadline=time.perf_counter()
            + CLIPBOARD_COPY_TIMEOUT_SEC,
        )

    def _wait_for_clipboard_change(
        self,
        context: dict,
        *,
        deadline: float,
    ) -> None:
        if not self.enabled:
            return

        before = context.get("clipboard_sequence_before")
        after = self._clipboard_sequence_number()
        changed = bool(
            before is not None
            and after is not None
            and int(after) != int(before)
        )

        # If sequence numbers are unavailable, keep compatibility by allowing
        # a small delay and then trying the clipboard once.
        sequence_unavailable = (
            before is None or after is None
        )
        elapsed = max(
            0.0,
            time.perf_counter()
            - float(
                context.get("trigger_perf")
                or time.perf_counter()
            ),
        )

        if changed or (
            sequence_unavailable
            and elapsed >= 0.20
        ):
            capture_info = {
                **context,
                "clipboard_changed": (
                    True if changed else None
                ),
                "clipboard_sequence_after": after,
                "copy_latency_ms": round(
                    elapsed * 1000.0,
                    1,
                ),
            }
            self._last_capture_info = capture_info
            self._run_callback(capture_info)
            return

        if time.perf_counter() >= deadline:
            capture_info = {
                **context,
                "clipboard_changed": False,
                "clipboard_sequence_after": after,
                "copy_latency_ms": round(
                    elapsed * 1000.0,
                    1,
                ),
            }
            self._last_capture_info = capture_info
            self._run_callback(capture_info)
            return

        self.root.after(
            CLIPBOARD_POLL_INTERVAL_MS,
            lambda: self._wait_for_clipboard_change(
                context,
                deadline=deadline,
            ),
        )

    def _run_callback(self, capture_info: dict | None = None):
        if self.enabled:
            self.callback(capture_info or {})

    def _hotkey_is_plain_ctrl_c(self):
        return (
            len(self.hotkey_main_keys) == 1
            and self.hotkey_main_keys[0] == "C"
            and self.hotkey_modifiers == {"CTRL"}
        )

    def _send_ctrl_c_to_active_window(self):
        if not self.is_windows:
            return

        try:
            import ctypes
            user32 = ctypes.windll.user32

            VK_CONTROL = 0x11
            VK_C = 0x43
            KEYEVENTF_KEYUP = 0x0002

            # Отпускаем клавиши выбранной горячей клавиши и модификаторы,
            # чтобы Q+W/F8/Ctrl+Shift+C не мешали отправить обычный Ctrl+C.
            for vk in sorted(self.hotkey_vks):
                if user32.GetAsyncKeyState(vk) & 0x8000:
                    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)

            for vk in (0x10, 0x12):  # Shift, Alt
                if user32.GetAsyncKeyState(vk) & 0x8000:
                    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)

            user32.keybd_event(VK_CONTROL, 0, 0, 0)
            time.sleep(0.03)
            user32.keybd_event(VK_C, 0, 0, 0)
            time.sleep(0.03)
            user32.keybd_event(VK_C, 0, KEYEVENTF_KEYUP, 0)
            user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
        except Exception:
            pass

    def remember_text_and_check_duplicate(self, text):
        """Защита от случайного двойного срабатывания на один и тот же текст."""
        now = time.monotonic()
        if text == self._last_text and now - self._last_text_time < 1.2:
            return True
        self._last_text = text
        self._last_text_time = now
        return False

    # ─────────────────────────────────────────────────────────────────────
    # Запасной локальный режим для Linux/macOS
    # ─────────────────────────────────────────────────────────────────────

    def _tk_sequence_for_hotkey(self):
        parts = []
        if "CTRL" in self.hotkey_modifiers:
            parts.append("Control")
        if "ALT" in self.hotkey_modifiers:
            parts.append("Alt")
        if "SHIFT" in self.hotkey_modifiers:
            parts.append("Shift")

        # Tkinter не умеет глобально ловить Q+W без сторонних библиотек.
        # В локальном запасном режиме берём последнюю клавишу сочетания.
        key = self.hotkey_main_keys[-1]
        if len(key) == 1:
            key = key.lower()
        parts.append(key)

        return "<" + "-".join(parts) + ">"

    def _enable_local_binding(self):
        self._disable_local_binding()
        sequence = self._tk_sequence_for_hotkey()
        self.root.bind_all(sequence, self._local_hotkey, add="+")
        self._local_binding_sequence = sequence

    def _disable_local_binding(self):
        if not self._local_binding_sequence:
            return
        try:
            self.root.unbind_all(self._local_binding_sequence)
        except Exception:
            pass
        self._local_binding_sequence = None

    def _local_hotkey(self, event=None):
        if self.enabled:
            context = self._foreground_capture_context()
            self.root.after(
                70,
                lambda: self._copy_selection_then_run_callback(
                    context
                ),
            )
        # Не возвращаем "break", чтобы обычное копирование не ломалось.
        return None

    # ─────────────────────────────────────────────────────────────────────
    # Глобальный keyboard hook для Windows
    # ─────────────────────────────────────────────────────────────────────

    def _windows_keyboard_hook_loop(self):
        try:
            import ctypes
            from ctypes import wintypes
        except Exception:
            return

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        WH_KEYBOARD_LL = 13
        HC_ACTION = 0
        WM_KEYDOWN = 0x0100
        WM_KEYUP = 0x0101
        WM_SYSKEYDOWN = 0x0104
        WM_SYSKEYUP = 0x0105
        GA_ROOT = 2

        modifier_vks = {
            "CTRL": (0x11, 0xA2, 0xA3),
            "SHIFT": (0x10, 0xA0, 0xA1),
            "ALT": (0x12, 0xA4, 0xA5),
        }

        ULONG_PTR = getattr(wintypes, "ULONG_PTR", ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong)
        HHOOK = getattr(wintypes, "HHOOK", wintypes.HANDLE)
        HINSTANCE = getattr(wintypes, "HINSTANCE", wintypes.HANDLE)

        class KBDLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [
                ("vkCode", wintypes.DWORD),
                ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR),
            ]

        LowLevelKeyboardProc = ctypes.WINFUNCTYPE(
            ctypes.c_int,
            ctypes.c_int,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )

        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, LowLevelKeyboardProc, HINSTANCE, wintypes.DWORD]
        user32.SetWindowsHookExW.restype = HHOOK
        user32.CallNextHookEx.argtypes = [HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        user32.CallNextHookEx.restype = ctypes.c_int
        user32.UnhookWindowsHookEx.argtypes = [HHOOK]
        user32.UnhookWindowsHookEx.restype = wintypes.BOOL
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user32.GetMessageW.restype = wintypes.BOOL
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = HINSTANCE
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD

        try:
            user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
            user32.GetAncestor.restype = wintypes.HWND
            user32.GetForegroundWindow.restype = wintypes.HWND
        except Exception:
            pass

        def foreground_is_our_app():
            try:
                if not self._root_hwnd:
                    return False
                fg = user32.GetForegroundWindow()
                if not fg:
                    return False
                root_fg = user32.GetAncestor(fg, GA_ROOT)
                return int(fg) == int(self._root_hwnd) or int(root_fg) == int(self._root_hwnd)
            except Exception:
                return False

        def is_modifier_pressed(modifier):
            return any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in modifier_vks[modifier])

        def modifiers_match():
            required = set(self.hotkey_modifiers)
            for modifier in self.MODIFIER_ORDER:
                pressed = bool(is_modifier_pressed(modifier))
                if (modifier in required) != pressed:
                    return False
            return True

        def hotkey_is_pressed():
            return self.hotkey_vks.issubset(self._pressed_vks) and modifiers_match()

        def should_suppress(vk):
            # Plain Ctrl+C не глушим: пусть Telegram/браузер/Word копируют штатно.
            if self._hotkey_is_plain_ctrl_c():
                return False
            if vk not in self.hotkey_vks:
                return False
            return modifiers_match() or len(self.hotkey_vks) > 1

        def hook_proc(n_code, w_param, l_param):
            try:
                if n_code == HC_ACTION:
                    kb = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                    vk = int(kb.vkCode)
                    is_down = w_param in (WM_KEYDOWN, WM_SYSKEYDOWN)
                    is_up = w_param in (WM_KEYUP, WM_SYSKEYUP)

                    if is_down:
                        self._pressed_vks.add(vk)
                    elif is_up:
                        # На keyup проверка уже не нужна, но клавишу из набора убираем.
                        self._pressed_vks.discard(vk)

                    if self.enabled and not foreground_is_our_app():
                        if is_down and vk in self.hotkey_vks and hotkey_is_pressed():
                            now = time.monotonic()
                            if now - self._last_trigger_time > 0.35:
                                self._last_trigger_time = now
                                context = self._foreground_capture_context()
                                self._last_trigger_context = dict(
                                    context
                                )
                                self._events.put(context)
                            if not self._hotkey_is_plain_ctrl_c():
                                return 1

                        if should_suppress(vk):
                            return 1
            except Exception:
                pass

            return user32.CallNextHookEx(self._hook_id, n_code, w_param, l_param)

        self._thread_id = kernel32.GetCurrentThreadId()
        self._hook_callback = LowLevelKeyboardProc(hook_proc)
        module_handle = kernel32.GetModuleHandleW(None)
        self._hook_id = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._hook_callback, module_handle, 0)

        if not self._hook_id:
            print("Не удалось включить глобальную автовставку по горячей клавише.")
            return

        msg = wintypes.MSG()
        try:
            while not self._stop_event.is_set():
                result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result == 0 or result == -1:
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            if self._hook_id:
                user32.UnhookWindowsHookEx(self._hook_id)
                self._hook_id = None


class ClosableNotebook(ttk.Notebook):
    """
    ttk.Notebook with a real close icon rendered inside every tab.
    The widget does not close tabs itself: it emits <<NotebookCloseRequested>>
    so the application can reject closing a running conversion safely.
    """

    def __init__(self, master=None, **kwargs) -> None:
        self._build_close_style(master)
        kwargs["style"] = "ClosableNotebook"
        super().__init__(master, **kwargs)
        self._close_pressed_index: int | None = None
        self.close_requested_index: int | None = None

        self.bind("<ButtonPress-1>", self._on_close_press, add="+")
        self.bind("<ButtonRelease-1>", self._on_close_release, add="+")

    def _build_close_style(self, master) -> None:
        style = ttk.Style(master)

        # Keep images on the instance later; Tcl keeps named images, but Python
        # references are also retained by assigning them to the root.
        root = master.winfo_toplevel() if master is not None else None

        normal = tk.PhotoImage(width=9, height=9, master=root)
        active = tk.PhotoImage(width=9, height=9, master=root)
        pressed = tk.PhotoImage(width=9, height=9, master=root)

        def draw_cross(image: tk.PhotoImage, color: str) -> None:
            for offset in (0, 1):
                for i in range(1, 8):
                    x1 = min(8, i + offset)
                    x2 = max(0, 8 - i - offset)
                    image.put(color, to=(x1, i))
                    image.put(color, to=(x2, i))

        draw_cross(normal, "#666666")
        draw_cross(active, "#cc3333")
        draw_cross(pressed, "#992222")

        if root is not None:
            root._closable_notebook_images = (normal, active, pressed)  # type: ignore[attr-defined]

        try:
            style.element_create(
                "ClosableNotebook.close",
                "image",
                normal,
                ("active", "!disabled", active),
                ("pressed", "!disabled", pressed),
                border=2,
                sticky="",
            )
        except tk.TclError:
            # The element may already exist if another notebook was created.
            pass

        style.layout(
            "ClosableNotebook",
            [("Notebook.client", {"sticky": "nswe"})],
        )
        style.layout(
            "ClosableNotebook.Tab",
            [
                (
                    "Notebook.tab",
                    {
                        "sticky": "nswe",
                        "children": [
                            (
                                "Notebook.padding",
                                {
                                    "side": "top",
                                    "sticky": "nswe",
                                    "children": [
                                        (
                                            "Notebook.focus",
                                            {
                                                "side": "top",
                                                "sticky": "nswe",
                                                "children": [
                                                    (
                                                        "Notebook.label",
                                                        {
                                                            "side": "left",
                                                            "sticky": "",
                                                        },
                                                    ),
                                                    (
                                                        "ClosableNotebook.close",
                                                        {
                                                            "side": "left",
                                                            "sticky": "",
                                                        },
                                                    ),
                                                ],
                                            },
                                        )
                                    ],
                                },
                            )
                        ],
                    },
                )
            ],
        )

    def _on_close_press(self, event):
        element = self.identify(event.x, event.y)
        if "close" not in element:
            return None

        try:
            index = self.index(f"@{event.x},{event.y}")
        except tk.TclError:
            return None

        self._close_pressed_index = index
        self.state(["pressed"])
        return "break"

    def _on_close_release(self, event):
        if self._close_pressed_index is None:
            return None

        pressed_index = self._close_pressed_index
        self._close_pressed_index = None
        self.state(["!pressed"])

        element = self.identify(event.x, event.y)
        if "close" not in element:
            return "break"

        try:
            index = self.index(f"@{event.x},{event.y}")
        except tk.TclError:
            return "break"

        if index == pressed_index:
            self.close_requested_index = index
            self.event_generate("<<NotebookCloseRequested>>")

        return "break"


class TaskTab:
    def __init__(
        self,
        app: "TTSApp",
        number: int,
        *,
        workspace_id: str | None = None,
        restored_title: str | None = None,
        restored_text: str = "",
        restored_settings: dict | None = None,
        restored_preview_bookmark: dict | None = None,
        custom_title: bool = False,
    ) -> None:
        self.app = app
        self.number = number
        self.workspace_id = workspace_id or uuid.uuid4().hex
        self.task_id = f"{number:02d}_{uuid.uuid4().hex[:6]}"

        tab_settings = dict(app.settings)
        if isinstance(restored_settings, dict):
            tab_settings.update(restored_settings)

        self.frame = ttk.Frame(app.notebook, padding=8)
        self.worker: threading.Thread | None = None
        self.cancel_event = threading.Event()
        self.preview_thread: threading.Thread | None = None
        self.preview_stop_event = threading.Event()
        self.preview_pause_event = threading.Event()
        self.preview_is_paused = False
        self.current_preview_segment_text = ""
        self.current_preview_run_id: str | None = None
        self.current_preview_base_index = "1.0"
        self.current_preview_base_text_offset = 0
        self.current_preview_source_mode = ""
        self.current_preview_segment_start = 0
        self.current_preview_segment_end = 0
        self.current_preview_range_end = 0
        self.current_preview_snapshot_end = 0
        self.current_preview_follow_end = False
        self.current_preview_segment_index = 0
        self.current_preview_segments_total = 0
        self.current_preview_word_length = 0
        self.preview_keep_bookmark_after_stop = False
        self.preview_resume_previous_pending = False

        mark_suffix = re.sub(
            r"[^A-Za-z0-9_]",
            "_",
            self.workspace_id[:16],
        )
        self.preview_base_mark = f"_preview_base_{mark_suffix}"
        self.preview_snapshot_end_mark = f"_preview_snapshot_end_{mark_suffix}"
        self.preview_pending_delete_start_mark = f"_preview_delete_start_{mark_suffix}"
        self.preview_pending_delete_end_mark = f"_preview_delete_end_{mark_suffix}"
        self.preview_deleted_chars = 0
        self.preview_pending_delete_active = False

        # A running MP3 task owns one immutable source snapshot. Marks keep its
        # exact widget range stable while hotkey text is appended after it.
        self.mp3_source_start_mark = f"_mp3_source_start_{mark_suffix}"
        self.mp3_source_end_mark = f"_mp3_source_end_{mark_suffix}"
        self.mp3_source_run_id: str | None = None
        self.mp3_source_chars = 0
        self.mp3_source_sha256 = ""
        self.mp3_source_separator_pending = False

        # Compact read/delete diagnostics and per-tab queue counters.
        self._delete_log_segments = 0
        self._delete_log_chars = 0
        self._delete_log_last_flush_perf = time.perf_counter()
        self._text_state_last_log_perf = 0.0
        self.session_captured_chars = 0
        self.session_read_segments = 0
        self.session_read_chars = 0
        self.session_deleted_segments = 0
        self.session_deleted_chars = 0
        self.queue_status_after_id: str | None = None

        # Preview finish cause is logged separately from SAPI's stopped=True/False.
        self.current_preview_finish_reason: str | None = None
        self.preview_resume_from_run_id: str | None = None

        self.preview_bookmark: dict | None = (
            dict(restored_preview_bookmark)
            if isinstance(restored_preview_bookmark, dict)
            else None
        )
        self.current_output_path: Path | None = None
        self.current_mp3_text_backup_path: Path | None = None
        self.current_run_id: str | None = None
        self.current_job_stage = "idle"

        self.voice_var = tk.StringVar(value=str(tab_settings.get("voice") or DEFAULT_VOICE_HINT))

        # Speed and Pitch are intentionally ONE shared Tk variable each for the
        # whole application. Every tab's sliders point to these same variables,
        # so changing either value in one tab instantly updates every other tab.
        self.rate_var = app.global_rate_var
        self.pitch_var = app.global_pitch_var
        self.audio_output_var = app.global_audio_output_var

        self.volume_var = tk.IntVar(value=clamp_int(tab_settings.get("volume"), 0, 100, 100))
        self.bitrate_var = tk.StringVar(value=str(tab_settings.get("bitrate") or "96k"))
        self.delete_read_text_var = tk.BooleanVar(
            value=bool(tab_settings.get("delete_read_text", False))
        )
        self.auto_read_hotkey_var = tk.BooleanVar(
            value=bool(tab_settings.get("auto_read_hotkey_text", True))
        )

        # Hotkey belongs to THIS tab. It is intentionally not inherited from
        # application settings: every newly created tab starts with "Нет".
        restored_copy_hotkey = str(
            tab_settings.get("copy_hotkey")
            or TAB_HOTKEY_NONE_LABEL
        ).strip() or TAB_HOTKEY_NONE_LABEL
        self.copy_hotkey_var = tk.StringVar(
            value=restored_copy_hotkey
        )
        self.copy_hotkey_status_var = tk.StringVar(
            value="Горячая клавиша не назначена."
        )
        self.applied_copy_hotkey = TAB_HOTKEY_NONE_LABEL

        self.status_var = tk.StringVar(value="Готово к работе.")
        self.queue_status_var = tk.StringVar(value="Очередь: считаю…")
        self.rate_value_var = tk.StringVar(value=str(self.rate_var.get()))
        self.pitch_value_var = tk.StringVar(value=str(self.pitch_var.get()))

        self.workspace_dirty = False
        self.custom_title = bool(custom_title)
        self.restored_title = restored_title or f"Вкладка {number}"

        self._build_ui()

        if restored_text:
            self.text_box.insert("1.0", restored_text)

        self._restore_preview_bookmark_ui()
        self.text_box.edit_modified(False)
        self.text_box.bind("<<Modified>>", self._on_text_modified, add="+")
        self.schedule_queue_status_refresh()

        # Speed and Pitch have one application-level trace each. Do not
        # attach one trace per tab to shared variables, otherwise a single slider
        # movement would cause duplicate callbacks from every open tab.
        for variable in (
            self.voice_var,
            self.volume_var,
            self.bitrate_var,
            self.delete_read_text_var,
            self.auto_read_hotkey_var,
        ):
            variable.trace_add("write", self._settings_changed)

    def _build_ui(self) -> None:
        toolbar = ttk.Frame(self.frame)
        toolbar.pack(fill="x")

        ttk.Button(
            toolbar,
            text="Вставить текст",
            command=self.paste_text_from_clipboard,
        ).pack(side="left")

        ttk.Button(
            toolbar,
            text="Очистить",
            command=self.clear_text,
        ).pack(side="left", padx=(6, 0))

        self.start_button = ttk.Button(
            toolbar,
            text="Создать MP3",
            command=self.start_job,
        )
        self.start_button.pack(side="left", padx=(18, 0))

        self.cancel_button = ttk.Button(
            toolbar,
            text="Остановить создание MP3",
            command=self.cancel_job,
            state="disabled",
        )
        self.cancel_button.pack(side="left", padx=(6, 0))

        self.preview_button = ttk.Button(
            toolbar,
            text="▶ Воспроизвести текст",
            command=self.start_preview,
        )
        self.preview_button.pack(side="left", padx=(18, 0))

        self.preview_cursor_button = ttk.Button(
            toolbar,
            text="▶ Читать с курсора",
            command=self.start_preview_from_cursor,
        )
        self.preview_cursor_button.pack(side="left", padx=(6, 0))

        self.pause_preview_button = ttk.Button(
            toolbar,
            text="⏸ Пауза",
            command=self.pause_preview,
            state="disabled",
        )
        self.pause_preview_button.pack(side="left", padx=(10, 0))

        self.resume_preview_button = ttk.Button(
            toolbar,
            text="▶ Продолжить",
            command=self.resume_preview,
            state="disabled",
        )
        self.resume_preview_button.pack(side="left", padx=(6, 0))

        self.stop_preview_button = ttk.Button(
            toolbar,
            text="■ Остановить",
            command=self.stop_preview,
            state="disabled",
        )
        self.stop_preview_button.pack(side="left", padx=(6, 0))

        capture_bar = ttk.Frame(self.frame)
        capture_bar.pack(fill="x", pady=(6, 0))

        ttk.Label(
            capture_bar,
            text="Горячая клавиша чтения:",
        ).pack(side="left")

        self.copy_hotkey_combo = ttk.Combobox(
            capture_bar,
            textvariable=self.copy_hotkey_var,
            values=(
                TAB_HOTKEY_NONE_LABEL,
                *GlobalCopyHotkeyMonitor.PRESET_HOTKEYS,
            ),
            state="normal",
            width=16,
        )
        self.copy_hotkey_combo.pack(
            side="left",
            padx=(6, 0),
        )
        self.copy_hotkey_combo.bind(
            "<<ComboboxSelected>>",
            lambda _event: self.apply_copy_hotkey(),
        )
        self.copy_hotkey_combo.bind(
            "<FocusOut>",
            lambda _event: self.apply_copy_hotkey(
                silent=True
            ),
        )
        self.copy_hotkey_combo.bind(
            "<Return>",
            lambda _event: self.apply_copy_hotkey(),
        )

        ttk.Label(
            capture_bar,
            textvariable=self.copy_hotkey_status_var,
        ).pack(side="left", padx=(10, 0))

        ttk.Checkbutton(
            capture_bar,
            text="Удалять прочитанные предложения",
            variable=self.delete_read_text_var,
        ).pack(side="left", padx=(18, 0))

        ttk.Checkbutton(
            capture_bar,
            text="Сразу читать добавленный текст",
            variable=self.auto_read_hotkey_var,
        ).pack(side="left", padx=(18, 0))

        ttk.Label(
            capture_bar,
            textvariable=self.queue_status_var,
            anchor="e",
        ).pack(side="right", padx=(16, 0))

        ttk.Label(
            self.frame,
            text="Текст для озвучки:",
        ).pack(anchor="w", pady=(10, 4))

        text_frame = ttk.Frame(self.frame)
        text_frame.pack(fill="both", expand=True)

        self.text_box = tk.Text(
            text_frame,
            wrap="word",
            undo=True,
            font=("Segoe UI", 10),
        )

        scrollbar = ttk.Scrollbar(
            text_frame,
            orient="vertical",
            command=self.text_box.yview,
        )
        self.text_box.configure(yscrollcommand=scrollbar.set)

        # Отдельный тег текущего читаемого предложения. Он не меняет текст,
        # не мешает обычному выделению мышью и снимается после остановки.
        self.text_box.tag_configure(
            "preview_current",
            background="#fff2a8",
            relief="solid",
            borderwidth=1,
        )

        # Instance-level binding runs before Tk's Text class binding.
        # This makes Ctrl+C/V/X/A/Z/Y work by the same physical keys under
        # English and Russian keyboard layouts without double copy/paste.
        self.text_box.bind(
            "<Control-KeyPress>",
            lambda event: handle_text_ctrl_shortcut(
                self.text_box,
                event,
            ),
            add="+",
        )

        self.text_box.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        voice_group = ttk.LabelFrame(
            self.frame,
            text="Параметры голоса",
            padding=10,
        )
        voice_group.pack(fill="x", pady=(10, 0))

        voice_group.columnconfigure(1, weight=1)
        voice_group.columnconfigure(4, weight=1)

        ttk.Label(voice_group, text="Голос:").grid(
            row=0,
            column=0,
            sticky="w",
        )

        self.voice_combo = ttk.Combobox(
            voice_group,
            textvariable=self.voice_var,
            state="readonly",
        )
        self.voice_combo.grid(
            row=0,
            column=1,
            columnspan=5,
            sticky="ew",
            padx=(8, 0),
        )
        self.voice_combo["values"] = self.app.voices

        ttk.Label(voice_group, text="Скорость:").grid(
            row=1,
            column=0,
            sticky="w",
            pady=(9, 0),
        )

        self.rate_scale = tk.Scale(
            voice_group,
            from_=RATE_MIN,
            to=RATE_MAX,
            orient="horizontal",
            resolution=1,
            showvalue=False,
            variable=self.rate_var,
            length=300,
        )
        self.rate_scale.grid(
            row=1,
            column=1,
            sticky="ew",
            padx=(8, 6),
            pady=(3, 0),
        )

        ttk.Spinbox(
            voice_group,
            from_=RATE_MIN,
            to=RATE_MAX,
            textvariable=self.rate_var,
            width=6,
        ).grid(
            row=1,
            column=2,
            sticky="w",
            pady=(9, 0),
        )

        ttk.Label(voice_group, text="Pitch:").grid(
            row=1,
            column=3,
            sticky="w",
            padx=(22, 0),
            pady=(9, 0),
        )

        self.pitch_scale = tk.Scale(
            voice_group,
            from_=PITCH_MIN,
            to=PITCH_MAX,
            orient="horizontal",
            resolution=1,
            showvalue=False,
            variable=self.pitch_var,
            length=300,
        )
        self.pitch_scale.grid(
            row=1,
            column=4,
            sticky="ew",
            padx=(8, 6),
            pady=(3, 0),
        )

        ttk.Spinbox(
            voice_group,
            from_=PITCH_MIN,
            to=PITCH_MAX,
            textvariable=self.pitch_var,
            width=6,
        ).grid(
            row=1,
            column=5,
            sticky="w",
            pady=(9, 0),
        )

        ttk.Label(voice_group, text="Громкость:").grid(
            row=2,
            column=0,
            sticky="w",
            pady=(8, 0),
        )

        ttk.Spinbox(
            voice_group,
            from_=0,
            to=100,
            textvariable=self.volume_var,
            width=7,
        ).grid(
            row=2,
            column=1,
            sticky="w",
            padx=(8, 0),
            pady=(8, 0),
        )

        ttk.Label(voice_group, text="Качество MP3:").grid(
            row=2,
            column=3,
            sticky="w",
            padx=(22, 0),
            pady=(8, 0),
        )

        ttk.Combobox(
            voice_group,
            textvariable=self.bitrate_var,
            values=("64k", "96k", "128k", "160k", "192k"),
            state="readonly",
            width=9,
        ).grid(
            row=2,
            column=4,
            sticky="w",
            padx=(8, 0),
            pady=(8, 0),
        )

        ttk.Label(
            voice_group,
            text="Устройство воспроизведения:",
        ).grid(
            row=3,
            column=0,
            sticky="w",
            pady=(8, 0),
        )

        self.audio_output_combo = ttk.Combobox(
            voice_group,
            textvariable=self.audio_output_var,
            values=self.app.audio_output_choices,
            state="readonly",
        )
        self.audio_output_combo.grid(
            row=3,
            column=1,
            columnspan=4,
            sticky="ew",
            padx=(8, 0),
            pady=(8, 0),
        )

        ttk.Button(
            voice_group,
            text="↻ Обновить",
            command=self.app.refresh_audio_outputs,
        ).grid(
            row=3,
            column=5,
            sticky="e",
            padx=(8, 0),
            pady=(8, 0),
        )

        progress_group = ttk.Frame(self.frame)
        progress_group.pack(fill="x", pady=(10, 0))

        self.progress = ttk.Progressbar(
            progress_group,
            maximum=100,
            mode="determinate",
        )
        self.progress.pack(fill="x")

        ttk.Label(
            progress_group,
            textvariable=self.status_var,
        ).pack(anchor="w", pady=(5, 0))


    def _on_text_modified(self, _event=None) -> None:
        try:
            modified = bool(self.text_box.edit_modified())
        except tk.TclError:
            return

        if not modified:
            return

        self.text_box.edit_modified(False)
        self.workspace_dirty = True

        if (
            self.preview_thread
            and self.preview_thread.is_alive()
            and bool(self.delete_read_text_var.get())
        ):
            self.app.schedule_workspace_save(
                delay_ms=int(
                    WORKSPACE_READING_SAVE_INTERVAL_SEC
                    * 1000
                ),
                keep_earliest=True,
            )
        else:
            self.app.schedule_workspace_save()

        self.schedule_queue_status_refresh()

    @staticmethod
    def _format_char_count(value: int) -> str:
        return f"{max(0, int(value)):,}".replace(",", " ")

    def schedule_queue_status_refresh(self) -> None:
        # Throttle instead of debounce: the first request schedules an update
        # and rapid sentence events cannot postpone it forever.
        if self.queue_status_after_id:
            return

        try:
            self.queue_status_after_id = self.app.root.after(
                QUEUE_STATUS_REFRESH_MS,
                self.refresh_queue_status,
            )
        except tk.TclError:
            self.queue_status_after_id = None

    def refresh_queue_status(self) -> None:
        self.queue_status_after_id = None
        try:
            text = self._full_text()
            chars = len(text)
            sentence_count = len(
                re.findall(
                    r"[.!?…]+(?=\s|$)",
                    text,
                )
            )
            if chars and sentence_count == 0:
                sentence_count = max(
                    1,
                    sum(
                        1
                        for line in text.splitlines()
                        if line.strip()
                    ),
                )

            label = (
                f"В очереди: ~{sentence_count} предл. / "
                f"{self._format_char_count(chars)} симв."
            )
            if self.session_captured_chars:
                label += (
                    " • добавлено: "
                    + self._format_char_count(
                        self.session_captured_chars
                    )
                )
            if self.session_read_chars:
                label += (
                    " • прочитано: "
                    + self._format_char_count(
                        self.session_read_chars
                    )
                )
            self.queue_status_var.set(label)
        except (tk.TclError, RuntimeError):
            pass

    def log_text_state(
        self,
        reason: str,
        *,
        force: bool = False,
    ) -> None:
        now_perf = time.perf_counter()
        if (
            not force
            and now_perf - self._text_state_last_log_perf
            < TEXT_STATE_LOG_INTERVAL_SEC
        ):
            return

        try:
            text = self._full_text()
            chars = len(text)
            if (
                self.preview_thread
                and self.preview_thread.is_alive()
            ):
                unread_start = max(
                    0,
                    min(
                        self.current_preview_segment_start,
                        chars,
                    ),
                )
            elif self._bookmark_is_valid():
                unread_start = max(
                    0,
                    min(
                        int(
                            (self.preview_bookmark or {}).get(
                                "sentence_start_offset",
                                0,
                            )
                            or 0
                        ),
                        chars,
                    ),
                )
            else:
                unread_start = 0

            self.app.logger.event(
                "text_state",
                task_id=self.task_id,
                tab_id=self.workspace_id,
                preview_run_id=self.current_preview_run_id,
                reason=reason,
                text_chars=chars,
                text_sha256_16=hashlib.sha256(
                    text.encode("utf-8")
                ).hexdigest()[:16],
                unread_chars=max(0, chars - unread_start),
                sentence_endings_approx=len(
                    re.findall(
                        r"[.!?…]+(?=\s|$)",
                        text,
                    )
                ),
                delete_read_text=bool(
                    self.delete_read_text_var.get()
                ),
                preview_running=bool(
                    self.preview_thread
                    and self.preview_thread.is_alive()
                ),
                paused=bool(self.preview_is_paused),
                session_captured_chars=self.session_captured_chars,
                session_read_segments=self.session_read_segments,
                session_read_chars=self.session_read_chars,
                session_deleted_segments=self.session_deleted_segments,
                session_deleted_chars=self.session_deleted_chars,
            )
            self._text_state_last_log_perf = now_perf
        except (tk.TclError, RuntimeError):
            pass

    def _accumulate_delete_log(
        self,
        *,
        deleted_chars: int,
        deleted_segments: int = 1,
    ) -> None:
        self._delete_log_chars += max(
            0,
            int(deleted_chars),
        )
        self._delete_log_segments += max(
            0,
            int(deleted_segments),
        )
        self.session_deleted_chars += max(
            0,
            int(deleted_chars),
        )
        self.session_deleted_segments += max(
            0,
            int(deleted_segments),
        )

        if (
            time.perf_counter()
            - self._delete_log_last_flush_perf
            >= READ_DELETE_LOG_FLUSH_INTERVAL_SEC
        ):
            self.flush_read_delete_log(
                reason="interval",
                force=True,
            )

    def flush_read_delete_log(
        self,
        *,
        reason: str,
        force: bool = False,
    ) -> None:
        if (
            self._delete_log_segments <= 0
            and self._delete_log_chars <= 0
        ):
            return

        now_perf = time.perf_counter()
        if (
            not force
            and now_perf - self._delete_log_last_flush_perf
            < READ_DELETE_LOG_FLUSH_INTERVAL_SEC
        ):
            return

        segments = self._delete_log_segments
        chars = self._delete_log_chars
        self._delete_log_segments = 0
        self._delete_log_chars = 0
        self._delete_log_last_flush_perf = now_perf

        self.app.logger.event(
            "preview_read_text_deleted_batch",
            task_id=self.task_id,
            tab_id=self.workspace_id,
            preview_run_id=self.current_preview_run_id,
            reason=reason,
            deleted_segments=segments,
            deleted_chars=chars,
            session_deleted_segments=self.session_deleted_segments,
            session_deleted_chars=self.session_deleted_chars,
            text_chars=len(self._full_text()),
        )
        self.log_text_state(
            f"delete_batch_{reason}",
            force=True,
        )

    def _settings_changed(self, *_args) -> None:
        self.app.schedule_settings_save(self)
        self.app.schedule_workspace_save()

    def apply_voice_list(self, voices: list[str]) -> None:
        self.voice_combo["values"] = voices

        current = self.voice_var.get().strip()
        for voice in voices:
            if current and voice.casefold() == current.casefold():
                self.voice_var.set(voice)
                self.status_var.set(f"Голос выбран: {voice}")
                return

        preferred = str(self.app.settings.get("voice") or "").strip()
        for voice in voices:
            if preferred and voice.casefold() == preferred.casefold():
                self.voice_var.set(voice)
                self.status_var.set(f"Голос выбран: {voice}")
                return

        for voice in voices:
            if DEFAULT_VOICE_HINT.casefold() in voice.casefold():
                self.voice_var.set(voice)
                self.status_var.set(f"Голос выбран: {voice}")
                return

        if voices:
            self.voice_var.set(voices[0])
            self.status_var.set(f"Голос выбран: {voices[0]}")
        else:
            self.voice_var.set("")
            self.status_var.set("SAPI-голоса не найдены.")

    def tab_title(self) -> str:
        try:
            return str(self.app.notebook.tab(self.frame, "text"))
        except tk.TclError:
            return self.restored_title

    def set_tab_title(
        self,
        title: str,
        *,
        custom: bool = True,
        schedule_save: bool = True,
    ) -> None:
        title = title.strip() or f"Вкладка {self.number}"
        if len(title) > 60:
            title = title[:57] + "..."
        self.custom_title = bool(custom)
        self.restored_title = title
        self.app.notebook.tab(self.frame, text=title)
        if schedule_save:
            self.app.schedule_workspace_save()

    def apply_copy_hotkey(
        self,
        *,
        silent: bool = False,
    ) -> bool:
        return self.app.apply_tab_copy_hotkey(
            self,
            silent=silent,
        )

    def workspace_record(self) -> dict:
        return {
            "workspace_id": self.workspace_id,
            "title": self.tab_title(),
            "custom_title": self.custom_title,
            "text_file": f"{self.workspace_id}.txt",
            "voice": self.voice_var.get().strip(),
            "rate": clamp_int(self.app.global_rate_var.get(), RATE_MIN, RATE_MAX, 0),
            "pitch": clamp_int(self.app.global_pitch_var.get(), PITCH_MIN, PITCH_MAX, 0),
            "audio_output": self.app.global_audio_output_var.get().strip()
            or DEFAULT_AUDIO_OUTPUT_LABEL,
            "volume": clamp_int(self.volume_var.get(), 0, 100, 100),
            "bitrate": self.bitrate_var.get().strip() or "96k",
            "delete_read_text": bool(self.delete_read_text_var.get()),
            "auto_read_hotkey_text": bool(
                self.auto_read_hotkey_var.get()
            ),
            "copy_hotkey": (
                self.applied_copy_hotkey
                if self.applied_copy_hotkey
                else TAB_HOTKEY_NONE_LABEL
            ),
            "preview_bookmark": (
                dict(self.preview_bookmark)
                if isinstance(self.preview_bookmark, dict)
                else None
            ),
        }

    def _reset_mp3_source_tracking(self) -> None:
        for mark_name in (
            self.mp3_source_start_mark,
            self.mp3_source_end_mark,
        ):
            try:
                self.text_box.mark_unset(mark_name)
            except tk.TclError:
                pass

        self.mp3_source_run_id = None
        self.mp3_source_chars = 0
        self.mp3_source_sha256 = ""
        self.mp3_source_separator_pending = False

    def track_mp3_source_snapshot(
        self,
        run_id: str,
        raw_text: str,
    ) -> bool:
        """Track the exact widget range owned by one MP3 conversion."""
        self._reset_mp3_source_tracking()

        try:
            start_index = self.text_box.index("1.0")
            end_index = self.text_box.index(
                f"{start_index}+{len(raw_text)}c"
            )
            if self.text_box.get(start_index, end_index) != raw_text:
                return False

            self.text_box.mark_set(
                self.mp3_source_start_mark,
                start_index,
            )
            self.text_box.mark_gravity(
                self.mp3_source_start_mark,
                "left",
            )
            self.text_box.mark_set(
                self.mp3_source_end_mark,
                end_index,
            )
            # Text inserted exactly at the old end must stay AFTER the source
            # range and therefore survive successful MP3 cleanup.
            self.text_box.mark_gravity(
                self.mp3_source_end_mark,
                "left",
            )
        except tk.TclError:
            self._reset_mp3_source_tracking()
            return False

        self.mp3_source_run_id = run_id
        self.mp3_source_chars = len(raw_text)
        self.mp3_source_sha256 = hashlib.sha256(
            raw_text.encode("utf-8")
        ).hexdigest()
        self.mp3_source_separator_pending = False

        self.app.logger.event(
            "mp3_source_snapshot_tracked",
            task_id=run_id,
            run_id=run_id,
            tab_id=self.workspace_id,
            source_chars=self.mp3_source_chars,
            source_sha256_16=self.mp3_source_sha256[:16],
        )
        return True

    def discard_mp3_source_snapshot(
        self,
        run_id: str | None,
        *,
        reason: str,
    ) -> bool:
        tracked_run_id = self.mp3_source_run_id
        if not tracked_run_id:
            return False
        if run_id and run_id != tracked_run_id:
            return False

        self.app.logger.event(
            "mp3_source_text_preserved",
            task_id=tracked_run_id,
            run_id=tracked_run_id,
            tab_id=self.workspace_id,
            reason=reason,
            source_chars=self.mp3_source_chars,
            current_text_chars=len(self._full_text()),
        )
        self._reset_mp3_source_tracking()
        return True

    def remove_completed_mp3_source(
        self,
        run_id: str | None,
    ) -> dict:
        """
        Remove only an unchanged source snapshot after verified MP3 success.

        Hotkey text appended after the left-gravity end mark is preserved. If
        anything inside the owned range changed, no text is deleted.
        """
        tracked_run_id = self.mp3_source_run_id
        if (
            not tracked_run_id
            or not run_id
            or run_id != tracked_run_id
        ):
            self.app.logger.event(
                "mp3_source_text_remove_skipped",
                task_id=run_id or self.task_id,
                run_id=run_id,
                tab_id=self.workspace_id,
                reason="snapshot_not_tracked",
                tracked_run_id=tracked_run_id,
            )
            return {
                "removed": False,
                "reason": "snapshot_not_tracked",
                "remaining_chars": len(self._full_text()),
            }

        try:
            start_index = self.text_box.index(
                self.mp3_source_start_mark
            )
            end_index = self.text_box.index(
                self.mp3_source_end_mark
            )
            current_source = self.text_box.get(
                start_index,
                end_index,
            )
        except tk.TclError as exc:
            self.app.logger.event(
                "mp3_source_text_remove_skipped",
                task_id=tracked_run_id,
                run_id=tracked_run_id,
                tab_id=self.workspace_id,
                reason="source_marks_unavailable",
                error=f"{type(exc).__name__}: {exc}",
            )
            self._reset_mp3_source_tracking()
            return {
                "removed": False,
                "reason": "source_marks_unavailable",
                "remaining_chars": len(self._full_text()),
            }

        current_sha256 = hashlib.sha256(
            current_source.encode("utf-8")
        ).hexdigest()
        snapshot_matches = bool(
            len(current_source) == self.mp3_source_chars
            and current_sha256 == self.mp3_source_sha256
        )
        if not snapshot_matches:
            self.app.logger.event(
                "mp3_source_text_remove_skipped",
                task_id=tracked_run_id,
                run_id=tracked_run_id,
                tab_id=self.workspace_id,
                reason="source_snapshot_changed",
                expected_chars=self.mp3_source_chars,
                current_source_chars=len(current_source),
                expected_sha256_16=self.mp3_source_sha256[:16],
                current_sha256_16=current_sha256[:16],
                current_text_chars=len(self._full_text()),
            )
            self._reset_mp3_source_tracking()
            return {
                "removed": False,
                "reason": "source_snapshot_changed",
                "remaining_chars": len(self._full_text()),
            }

        before_chars = len(self._full_text())
        source_chars = len(current_source)
        self.text_box.delete(start_index, end_index)

        separator_chars = 0
        if self.mp3_source_separator_pending:
            separator_end = self.text_box.index(
                f"{start_index}+1c"
            )
            if self.text_box.get(
                start_index,
                separator_end,
            ) == "\n":
                self.text_box.delete(
                    start_index,
                    separator_end,
                )
                separator_chars = 1

        self.clear_saved_preview_bookmark(
            reason="mp3_source_deleted",
            schedule_save=False,
        )
        self.workspace_dirty = True
        self._reset_mp3_source_tracking()

        remaining_chars = len(self._full_text())
        self.app.save_workspace()
        self.schedule_queue_status_refresh()
        self.log_text_state(
            "mp3_source_deleted",
            force=True,
        )
        self.app.logger.event(
            "mp3_source_text_removed",
            task_id=tracked_run_id,
            run_id=tracked_run_id,
            tab_id=self.workspace_id,
            source_chars=source_chars,
            separator_chars=separator_chars,
            removed_chars=source_chars + separator_chars,
            text_chars_before=before_chars,
            remaining_chars=remaining_chars,
        )
        return {
            "removed": True,
            "reason": "success",
            "source_chars": source_chars,
            "separator_chars": separator_chars,
            "remaining_chars": remaining_chars,
        }

    def capture_external_selection(self) -> None:
        """Capture selected text from the last active external Windows app."""
        self.app.capture_selected_text_for_tab(self)

    def append_external_selected_text(self, captured_text: str) -> bool:
        """
        Append externally selected text to this tab and make it part of reading.
        """
        if not isinstance(captured_text, str):
            return False

        captured_text = captured_text.strip()
        if not captured_text:
            messagebox.showwarning(
                APP_TITLE,
                "В выделении нет текста для чтения.",
            )
            return False

        old_text = self._full_text()
        old_chars = len(old_text)

        separator = ""
        if (
            old_text
            and not old_text.endswith(("\n", "\r"))
            and not captured_text.startswith(("\n", "\r"))
        ):
            separator = "\n"

        added_text = separator + captured_text
        old_end_index = self._index_from_text_offset(old_chars)
        # Keep treating the MP3 task as active until its final GUI event is
        # handled. The worker may finish a fraction of a second before Tk
        # processes "done"/"cancelled"/"job_error"; text captured in that
        # window must still remain outside the just-finished snapshot.
        during_mp3_conversion = bool(self.current_run_id)
        appended_at_mp3_source_end = False
        if (
            during_mp3_conversion
            and self.mp3_source_run_id
            and self.mp3_source_run_id == self.current_run_id
        ):
            try:
                appended_at_mp3_source_end = self.text_box.compare(
                    self.mp3_source_end_mark,
                    "==",
                    old_end_index,
                )
            except tk.TclError:
                appended_at_mp3_source_end = False

        self.text_box.insert("end-1c", added_text)
        if appended_at_mp3_source_end and separator == "\n":
            self.mp3_source_separator_pending = True

        captured_start_offset = old_chars + len(separator)
        captured_start_index = self._index_from_text_offset(captured_start_offset)

        new_text = self._full_text()
        new_chars = len(new_text)

        live_preview = bool(
            self.preview_thread and self.preview_thread.is_alive()
        )
        saved_pause = bool(
            self.preview_is_paused and self._bookmark_is_valid()
        )
        auto_read_requested = bool(
            self.auto_read_hotkey_var.get()
        )

        if during_mp3_conversion:
            self.status_var.set(
                "Текст добавлен вниз. Он сохранён для следующего MP3 "
                "и не входит в текущую конвертацию."
            )

        elif live_preview and auto_read_requested:
            if not self.current_preview_follow_end:
                try:
                    self.text_box.mark_set(
                        self.preview_snapshot_end_mark,
                        old_end_index,
                    )
                    self.text_box.mark_gravity(
                        self.preview_snapshot_end_mark,
                        "left",
                    )
                except tk.TclError:
                    pass

            self.current_preview_follow_end = True
            self.current_preview_range_end = new_chars
            self.status_var.set(
                "Выделенный текст добавлен вниз. "
                "Текущее чтение не прерывается; новый текст будет прочитан следом."
            )

        elif live_preview:
            self.current_preview_follow_end = False
            self.status_var.set(
                "Текст добавлен вниз. Текущее чтение продолжается, "
                "но новый текст оставлен для ручного запуска."
            )

        elif saved_pause and auto_read_requested:
            if isinstance(self.preview_bookmark, dict):
                bookmark = self.preview_bookmark
                saved_chars = max(
                    0,
                    int(bookmark.get("text_chars") or old_chars),
                )
                bookmark["range_was_to_end"] = True
                bookmark["end_offset"] = new_chars
                bookmark["last_append_at"] = now_iso()
                bookmark["appended_chars_after_pause"] = max(
                    0,
                    new_chars - saved_chars,
                )

            self.status_var.set(
                "Выделенный текст добавлен вниз. "
                "Пауза сохранена — нажмите «Продолжить»."
            )

        elif saved_pause:
            self.status_var.set(
                "Текст добавлен вниз. Пауза сохранена; новый текст "
                "оставлен для ручного запуска."
            )

        elif auto_read_requested:
            self._start_preview_worker(
                raw_text=self.text_box.get(
                    captured_start_index,
                    "end-1c",
                ),
                preview_base_index=captured_start_index,
                source_mode="external_capture",
                selected_text=False,
            )
        else:
            self.status_var.set(
                "Текст добавлен вниз. Автоматическое чтение отключено."
            )

        self.session_captured_chars += len(captured_text)
        self.workspace_dirty = True
        self.app.save_workspace()
        self.schedule_queue_status_refresh()
        self.log_text_state(
            "external_selection_appended",
            force=True,
        )

        self.app.logger.event(
            "external_selection_appended",
            task_id=self.task_id,
            tab_id=self.workspace_id,
            preview_run_id=self.current_preview_run_id,
            captured_chars=len(captured_text),
            inserted_chars=len(added_text),
            old_text_chars=old_chars,
            new_text_chars=new_chars,
            during_mp3_conversion=during_mp3_conversion,
            included_in_current_mp3=False,
            live_preview=live_preview,
            paused=saved_pause,
            auto_read_requested=auto_read_requested,
            delete_read_text=bool(self.delete_read_text_var.get()),
        )
        return True

    def paste_text_from_clipboard(self) -> None:
        """
        Paste Unicode text from the clipboard.

        Normal state:
            behaves like ordinary Ctrl+V at the caret/selection.

        While text is being read OR while a saved listening pause is active:
            appends the clipboard text to the very end of the tab and preserves
            the current reading position. The live SAPI stream is not stopped.
            When the original snapshot reaches its end, the newly appended tail
            is picked up automatically and reading continues.
        """
        if self.worker and self.worker.is_alive():
            messagebox.showwarning(
                APP_TITLE,
                "Нельзя изменять текст, пока эта вкладка создаёт MP3.",
            )
            return

        try:
            clipboard_text = self.app.root.clipboard_get()
        except tk.TclError:
            messagebox.showwarning(
                APP_TITLE,
                "В буфере обмена нет текста для вставки.",
            )
            return

        if not isinstance(clipboard_text, str) or not clipboard_text:
            messagebox.showwarning(
                APP_TITLE,
                "В буфере обмена нет текста для вставки.",
            )
            return

        live_preview = bool(
            self.preview_thread
            and self.preview_thread.is_alive()
        )
        saved_pause = bool(
            self.preview_is_paused
            and self._bookmark_is_valid()
        )
        append_to_end = bool(live_preview or saved_pause)

        try:
            if append_to_end:
                old_text = self._full_text()
                old_chars = len(old_text)

                # "Докинуть вниз": keep the appended block visually separate.
                separator = ""
                if (
                    old_text
                    and not old_text.endswith(("\n", "\r"))
                    and not clipboard_text.startswith(("\n", "\r"))
                ):
                    separator = "\n"

                added_text = separator + clipboard_text
                self.text_box.insert("end-1c", added_text)

                new_text = self._full_text()
                new_chars = len(new_text)

                # A running whole-text/cursor/bookmark preview must ultimately
                # reach the new bottom as well. The current SAPI sentence is
                # untouched and continues normally.
                if live_preview and self.current_preview_follow_end:
                    self.current_preview_range_end = new_chars

                # A persisted pause can remain valid after an append because all
                # offsets before the old end are unchanged. If its range was
                # intended to reach the end, extend that range to the new end.
                if isinstance(self.preview_bookmark, dict):
                    bookmark = self.preview_bookmark
                    saved_chars = max(
                        0,
                        int(bookmark.get("text_chars") or old_chars),
                    )
                    old_end = max(
                        0,
                        int(bookmark.get("end_offset") or saved_chars),
                    )
                    range_was_to_end = bool(
                        bookmark.get(
                            "range_was_to_end",
                            old_end >= saved_chars,
                        )
                    )
                    bookmark["range_was_to_end"] = range_was_to_end
                    if range_was_to_end:
                        bookmark["end_offset"] = new_chars
                    bookmark["last_append_at"] = now_iso()
                    bookmark["appended_chars_after_pause"] = max(
                        0,
                        new_chars - saved_chars,
                    )

                self.workspace_dirty = True
                self.app.save_workspace()
                self.schedule_queue_status_refresh()
                self.log_text_state(
                    "clipboard_append",
                    force=True,
                )

                if saved_pause:
                    self.status_var.set(
                        "Текст добавлен в конец. Пауза сохранена — "
                        "нажмите «Продолжить»."
                    )
                elif live_preview:
                    self.status_var.set(
                        "Текст добавлен в конец. Чтение продолжается."
                    )

                self.app.logger.event(
                    "clipboard_text_appended_during_preview",
                    task_id=self.task_id,
                    tab_id=self.workspace_id,
                    preview_run_id=self.current_preview_run_id,
                    clipboard_chars=len(clipboard_text),
                    inserted_chars=len(added_text),
                    old_text_chars=old_chars,
                    new_text_chars=new_chars,
                    paused=bool(
                        self.preview_pause_event.is_set()
                        or saved_pause
                    ),
                    follow_end=bool(self.current_preview_follow_end),
                )
                return

            # Normal editor paste when no listening session/bookmark is active.
            self.clear_saved_preview_bookmark(
                reason="clipboard_paste",
                schedule_save=False,
            )

            insert_index = self.text_box.index("insert")
            had_selection = False

            try:
                selection_start = self.text_box.index("sel.first")
                selection_end = self.text_box.index("sel.last")
                had_selection = True
                self.text_box.delete(selection_start, selection_end)
                insert_index = selection_start
            except tk.TclError:
                pass

            self.text_box.insert(insert_index, clipboard_text)
            end_index = self.text_box.index(
                f"{insert_index}+{len(clipboard_text)}c"
            )
            self.text_box.mark_set("insert", end_index)
            self.text_box.see(end_index)

            self.workspace_dirty = True
            self.app.schedule_workspace_save()
            self.schedule_queue_status_refresh()
            self.log_text_state(
                "clipboard_paste",
                force=True,
            )

            self.status_var.set(
                "Вставлено из буфера обмена: "
                f"{len(clipboard_text):,} символов.".replace(",", " ")
            )

            self.app.logger.event(
                "clipboard_text_pasted",
                task_id=self.task_id,
                tab_id=self.workspace_id,
                chars=len(clipboard_text),
                replaced_selection=had_selection,
            )

        except (tk.TclError, TypeError, ValueError) as exc:
            self.app.log_ui_error(
                task_id=self.task_id,
                stage="paste_clipboard_text",
                exc=exc,
                context={
                    "clipboard_chars": len(clipboard_text),
                    "append_to_end": append_to_end,
                },
            )
            messagebox.showerror(
                APP_TITLE,
                "Не удалось вставить текст из буфера обмена.\n\n"
                f"{exc}",
            )

    def load_file(self) -> None:
        kwargs = {}
        last_dir = self.app.settings.get("last_input_dir")
        if last_dir and Path(last_dir).is_dir():
            kwargs["initialdir"] = last_dir

        path = filedialog.askopenfilename(
            title="Выберите текстовый файл",
            filetypes=[
                ("Текстовые файлы", "*.txt *.md"),
                ("Все файлы", "*.*"),
            ],
            **kwargs,
        )
        if not path:
            return

        src = Path(path)

        try:
            text = read_text_file(src)
        except Exception as exc:
            self.app.log_ui_error(
                task_id=self.task_id,
                stage="load_text_file",
                exc=exc,
                context={"input_path": str(src)},
            )
            messagebox.showerror(
                APP_TITLE,
                f"Не удалось прочитать файл:\n{exc}",
            )
            return

        self.clear_saved_preview_bookmark(
            reason="load_new_text",
            schedule_save=False,
        )
        self.text_box.delete("1.0", "end")
        self.text_box.insert("1.0", text)
        self.text_box.edit_modified(False)
        self.workspace_dirty = True

        self.app.settings["last_input_dir"] = str(src.parent)
        # A filename is a meaningful user-facing title and must not be overwritten
        # by automatic renumbering.
        self.set_tab_title(src.stem, custom=True)
        self.app.renumber_default_tabs()
        self.status_var.set(
            f"Загружено: {src.name} — {len(text):,} символов.".replace(",", " ")
        )
        self.app.schedule_settings_save(self)
        self.app.schedule_workspace_save()

    def clear_text(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showwarning(
                APP_TITLE,
                "Нельзя очистить текст, пока эта вкладка создаёт MP3.",
            )
            return

        self.stop_preview(
            preserve_bookmark=False,
            reason="clear_text",
        )
        self.clear_saved_preview_bookmark(
            reason="clear_text",
            schedule_save=False,
        )
        self.text_box.delete("1.0", "end")
        self.workspace_dirty = True
        self.status_var.set("Текст очищен.")
        self.schedule_queue_status_refresh()
        self.log_text_state(
            "clear_text",
            force=True,
        )
        self.app.schedule_workspace_save()

    def _full_text(self) -> str:
        return self.text_box.get("1.0", "end-1c")

    def _full_text_sha256(self) -> str:
        return hashlib.sha256(
            self._full_text().encode("utf-8")
        ).hexdigest()

    def _text_offset_from_index(self, index: str) -> int:
        try:
            # Tk 8.6 reports a non-BMP character (for example an emoji) as
            # two UTF-16 units in Text.count(..., "chars"), while Python
            # offsets and Text's +Nc index movement treat it as one character.
            # Using the actual returned string keeps bookmarks and delete
            # offsets in the same coordinate system as Python's len().
            return len(
                self.text_box.get(
                    "1.0",
                    self.text_box.index(index),
                )
            )
        except (tk.TclError, TypeError, ValueError):
            return 0

    def _index_from_text_offset(self, offset: int) -> str:
        offset = max(0, int(offset))
        return self.text_box.index(f"1.0+{offset}c")

    def clear_saved_preview_bookmark(
        self,
        *,
        reason: str,
        schedule_save: bool = True,
    ) -> None:
        had_bookmark = isinstance(self.preview_bookmark, dict)
        self.preview_bookmark = None
        self.preview_keep_bookmark_after_stop = False
        if had_bookmark:
            self.app.logger.event(
                "preview_bookmark_cleared",
                task_id=self.task_id,
                tab_id=self.workspace_id,
                reason=reason,
            )
        if schedule_save:
            self.app.schedule_workspace_save()

    def _bookmark_is_valid(self) -> bool:
        """
        A bookmark remains valid when text was only APPENDED after it was saved.

        We hash exactly the prefix that existed at pause time. Therefore adding
        new chapters at the bottom does not invalidate the saved sentence, while
        edits/deletions inside the old text still invalidate it safely.
        """
        bookmark = self.preview_bookmark
        if not isinstance(bookmark, dict):
            return False

        try:
            offset = int(bookmark.get("char_offset") or 0)
            saved_chars = int(bookmark.get("text_chars") or 0)
        except Exception:
            return False

        full_text = self._full_text()
        if (
            not full_text
            or saved_chars <= 0
            or len(full_text) < saved_chars
            or not (0 <= offset < len(full_text))
        ):
            return False

        expected_hash = str(bookmark.get("text_sha256") or "")
        if not expected_hash:
            return False

        saved_prefix = full_text[:saved_chars]
        return expected_hash == hashlib.sha256(
            saved_prefix.encode("utf-8")
        ).hexdigest()

    def _restore_preview_bookmark_ui(self) -> None:
        if not isinstance(self.preview_bookmark, dict):
            return
        if not self._bookmark_is_valid():
            self.preview_bookmark = None
            return

        try:
            offset = int(self.preview_bookmark.get("char_offset") or 0)
            sentence_start = max(
                0,
                int(
                    self.preview_bookmark.get(
                        "sentence_start_offset",
                        offset,
                    )
                    or 0
                ),
            )
            sentence_end = max(
                sentence_start + 1,
                int(
                    self.preview_bookmark.get(
                        "sentence_end_offset",
                        sentence_start + 1,
                    )
                    or (sentence_start + 1)
                ),
            )
            start_index = self._index_from_text_offset(sentence_start)
            end_index = self._index_from_text_offset(
                min(sentence_end, len(self._full_text()))
            )
            self.clear_preview_highlight()
            self.text_box.tag_add(
                "preview_current",
                start_index,
                end_index,
            )
            self.text_box.tag_raise("preview_current")
            self.text_box.see(start_index)
            self.text_box.mark_set("insert", start_index)
        except tk.TclError:
            pass

        self.preview_is_paused = True
        self.pause_preview_button.configure(state="disabled")
        self.resume_preview_button.configure(state="normal")
        self.stop_preview_button.configure(state="normal")
        self.preview_button.configure(state="normal")
        self.preview_cursor_button.configure(state="normal")

        offset = int(self.preview_bookmark.get("char_offset") or 0)
        total = max(1, len(self._full_text()))
        percent = min(100.0, max(0.0, offset / total * 100.0))
        snippet = str(self.preview_bookmark.get("snippet") or "").strip()
        if snippet:
            self.current_preview_segment_text = snippet
            self.status_var.set(
                f"Сохранена пауза ({percent:.1f}%): {snippet}"
            )
        else:
            self.status_var.set(
                f"Сохранена пауза книги: {percent:.1f}%. "
                "Нажмите «Продолжить»."
            )

    def save_current_preview_bookmark(
        self,
        *,
        reason: str,
        absolute_offset: int | None = None,
        word_length: int | None = None,
        force_save: bool = True,
        log_event: bool = True,
        accuracy: str = "sentence",
    ) -> bool:
        full_text = self._full_text()
        if not full_text:
            return False

        if absolute_offset is None:
            absolute_offset = self.current_preview_segment_start
        absolute_offset = max(
            0,
            min(int(absolute_offset), max(0, len(full_text) - 1)),
        )
        if word_length is None:
            word_length = self.current_preview_word_length
        word_length = max(1, int(word_length or 1))

        snippet_start = max(0, absolute_offset - 35)
        snippet_end = min(len(full_text), absolute_offset + 90)
        snippet = " ".join(
            full_text[snippet_start:snippet_end].split()
        )
        if len(snippet) > 120:
            snippet = snippet[:117] + "..."

        sentence_start = max(
            0,
            min(
                int(self.current_preview_segment_start),
                max(0, len(full_text) - 1),
            ),
        )
        sentence_end = max(
            sentence_start + 1,
            min(
                int(
                    self.current_preview_segment_end
                    or (sentence_start + 1)
                ),
                len(full_text),
            ),
        )
        sentence_text = full_text[sentence_start:sentence_end]
        previous_sentence_start = previous_preview_segment_start(
            full_text,
            sentence_start,
        )

        self.preview_bookmark = {
            "schema": 2,
            "state": "paused",
            "saved_at": now_iso(),
            "reason": reason,
            "char_offset": absolute_offset,
            "sentence_start_offset": sentence_start,
            "sentence_end_offset": sentence_end,
            "previous_sentence_start_offset": previous_sentence_start,
            "sentence_sha256": hashlib.sha256(
                sentence_text.encode("utf-8")
            ).hexdigest(),
            "end_offset": max(
                absolute_offset + 1,
                int(self.current_preview_range_end or len(full_text)),
            ),
            "range_was_to_end": bool(self.current_preview_follow_end),
            "word_length": word_length,
            "segment_index": self.current_preview_segment_index,
            "segments_total": self.current_preview_segments_total,
            "source_mode": self.current_preview_source_mode,
            "text_sha256": hashlib.sha256(
                full_text.encode("utf-8")
            ).hexdigest(),
            "text_chars": len(full_text),
            "snippet": snippet,
        }
        self.preview_keep_bookmark_after_stop = True

        if log_event:
            self.app.logger.event(
                "preview_bookmark_saved",
                task_id=self.task_id,
                tab_id=self.workspace_id,
                preview_run_id=self.current_preview_run_id,
                reason=reason,
                accuracy=accuracy,
                char_offset=absolute_offset,
                sentence_start_offset=sentence_start,
                sentence_end_offset=sentence_end,
                previous_sentence_start_offset=previous_sentence_start,
                word_length=word_length,
                text_chars=len(full_text),
                source_mode=self.current_preview_source_mode,
            )

        if force_save:
            # Persist immediately so a pause survives an immediate restart.
            self.app.save_workspace()
        else:
            self.app.schedule_workspace_save()
        return True

    def apply_preview_checkpoint(self, info: dict) -> None:
        if not (
            self.preview_thread
            and self.preview_thread.is_alive()
        ):
            return
        checkpoint_run_id = str(
            info.get("preview_run_id") or ""
        )
        if (
            checkpoint_run_id
            and checkpoint_run_id != self.current_preview_run_id
        ):
            return
        try:
            base_index = str(info.get("base_index") or self.current_preview_base_index)
            segment_start = int(info.get("start") or 0)
            word_position = max(0, int(info.get("word_position") or 0))
            word_length = max(1, int(info.get("word_length") or 1))
            adjusted_segment_start = max(
                0,
                segment_start - int(self.preview_deleted_chars),
            )
            point_index = self.text_box.index(
                f"{base_index}+{adjusted_segment_start + word_position}c"
            )
            absolute_offset = self._text_offset_from_index(point_index)
        except (tk.TclError, TypeError, ValueError):
            absolute_offset = self.current_preview_segment_start
            word_length = max(1, self.current_preview_word_length or 1)

        self.current_preview_word_length = word_length
        checkpoint_reason = str(
            info.get("reason")
            or "sapi_checkpoint"
        )
        raw_position = info.get("sapi_position_raw")
        accuracy = (
            "exact_word"
            if (
                checkpoint_reason == "pause_inside_segment"
                and raw_position is not None
                and word_length > 0
            )
            else "sentence"
        )
        self.save_current_preview_bookmark(
            reason=checkpoint_reason,
            absolute_offset=absolute_offset,
            word_length=word_length,
            force_save=True,
            log_event=True,
            accuracy=accuracy,
        )
        self.log_text_state(
            "preview_checkpoint",
            force=True,
        )
        self._restore_preview_bookmark_ui()

    def resume_saved_preview(self) -> bool:
        if self.preview_thread and self.preview_thread.is_alive():
            return False
        if not self._bookmark_is_valid():
            if self.preview_bookmark is not None:
                messagebox.showwarning(
                    APP_TITLE,
                    "Сохранённая позиция чтения больше не подходит: "
                    "текст вкладки был изменён. Позиция сброшена.",
                )
                self.clear_saved_preview_bookmark(
                    reason="text_changed",
                    schedule_save=True,
                )
                self.preview_finished_ui()
            return False

        bookmark = dict(self.preview_bookmark or {})
        word_offset = int(bookmark.get("char_offset") or 0)
        paused_sentence_offset = max(
            0,
            int(
                bookmark.get(
                    "sentence_start_offset",
                    word_offset,
                )
                or 0
            ),
        )

        # User-requested behavior: ALWAYS rewind one preview sentence when
        # pressing Continue. This avoids SAPI jumping to the next sentence and
        # also gives a little context before the exact pause point.
        stored_previous = bookmark.get(
            "previous_sentence_start_offset"
        )
        if stored_previous is None:
            # Migration for bookmarks created by version 3.3.
            offset = previous_preview_segment_start(
                self._full_text(),
                paused_sentence_offset,
            )
        else:
            offset = max(0, int(stored_previous))

        saved_chars = max(
            0,
            int(bookmark.get("text_chars") or len(self._full_text())),
        )
        saved_end_offset = max(
            offset + 1,
            int(bookmark.get("end_offset") or saved_chars),
        )
        range_was_to_end = bool(
            bookmark.get(
                "range_was_to_end",
                saved_end_offset >= saved_chars,
            )
        )

        start_index = self._index_from_text_offset(offset)
        if range_was_to_end:
            end_offset = len(self._full_text())
        else:
            end_offset = max(
                offset + 1,
                min(saved_end_offset, len(self._full_text())),
            )
        end_index = self._index_from_text_offset(end_offset)
        raw_text = self.text_box.get(start_index, end_index)
        if not normalize_text(raw_text):
            self.clear_saved_preview_bookmark(
                reason="bookmark_at_end",
                schedule_save=True,
            )
            return False

        self.log_text_state(
            "preview_bookmark_resume",
            force=True,
        )
        self.app.logger.event(
            "preview_bookmark_resumed",
            task_id=self.task_id,
            tab_id=self.workspace_id,
            saved_at=bookmark.get("saved_at"),
            char_offset=word_offset,
            paused_sentence_offset=paused_sentence_offset,
            resume_previous_sentence_offset=offset,
            range_was_to_end=range_was_to_end,
            text_chars=len(self._full_text()),
        )
        self._start_preview_worker(
            raw_text=raw_text,
            preview_base_index=start_index,
            source_mode="saved_bookmark",
            selected_text=False,
            preserve_existing_bookmark=True,
        )
        return True

    def selected_or_all_text(self) -> tuple[str, bool, str]:
        try:
            start_index = self.text_box.index("sel.first")
            selected = self.text_box.get("sel.first", "sel.last")
            if selected.strip():
                return selected, True, start_index
        except tk.TclError:
            pass

        return self.text_box.get("1.0", "end-1c"), False, "1.0"

    def text_from_insert_cursor(self) -> tuple[str, str]:
        cursor_index = self.text_box.index("insert")
        return self.text_box.get(cursor_index, "end-1c"), cursor_index

    def start_preview_from_cursor(self) -> None:
        if self.preview_thread and self.preview_thread.is_alive():
            return

        raw_text, cursor_index = self.text_from_insert_cursor()
        if not normalize_text(raw_text):
            messagebox.showwarning(
                APP_TITLE,
                "После курсора нет текста для воспроизведения.",
            )
            return

        self._start_preview_worker(
            raw_text=raw_text,
            preview_base_index=cursor_index,
            source_mode="cursor",
            selected_text=False,
        )

    def start_preview(self) -> None:
        if self.preview_thread and self.preview_thread.is_alive():
            return

        raw_text, is_selection, preview_base_index = self.selected_or_all_text()

        if not normalize_text(raw_text):
            messagebox.showwarning(
                APP_TITLE,
                "Вставьте текст для воспроизведения.",
            )
            return

        self._start_preview_worker(
            raw_text=raw_text,
            preview_base_index=preview_base_index,
            source_mode="selection" if is_selection else "all",
            selected_text=is_selection,
        )

    def _start_preview_worker(
        self,
        *,
        raw_text: str,
        preview_base_index: str,
        source_mode: str,
        selected_text: bool,
        preserve_existing_bookmark: bool = False,
    ) -> None:
        if self.preview_thread and self.preview_thread.is_alive():
            return

        preview_run_id = make_run_id("preview")
        self.current_preview_run_id = preview_run_id
        self.current_preview_finish_reason = None

        text = raw_text
        voice = self.voice_var.get().strip()
        if not voice:
            messagebox.showwarning(APP_TITLE, "Выберите голос.")
            return

        rate = clamp_int(self.rate_var.get(), RATE_MIN, RATE_MAX, 0)
        pitch = clamp_int(self.pitch_var.get(), PITCH_MIN, PITCH_MAX, 0)
        audio_output = (
            self.audio_output_var.get().strip()
            or DEFAULT_AUDIO_OUTPUT_LABEL
        )
        volume = clamp_int(self.volume_var.get(), 0, 100, 100)

        self.app.stop_other_previews(self)
        self.preview_stop_event.clear()
        self.preview_pause_event.clear()
        self.preview_is_paused = False
        self.preview_keep_bookmark_after_stop = False
        self.current_preview_segment_text = ""
        self.current_preview_source_mode = source_mode

        try:
            self.text_box.mark_set(
                self.preview_base_mark,
                preview_base_index,
            )
            self.text_box.mark_gravity(
                self.preview_base_mark,
                "left",
            )
            self.text_box.mark_set(
                self.preview_snapshot_end_mark,
                f"{preview_base_index}+{len(raw_text)}c",
            )
            self.text_box.mark_gravity(
                self.preview_snapshot_end_mark,
                "left",
            )
            ui_base_index = self.preview_base_mark
        except tk.TclError:
            ui_base_index = preview_base_index

        self.current_preview_base_index = ui_base_index
        self.preview_deleted_chars = 0
        self.preview_pending_delete_active = False

        self.current_preview_base_text_offset = self._text_offset_from_index(
            ui_base_index
        )
        self.current_preview_segment_start = (
            self.current_preview_base_text_offset
        )
        self.current_preview_segment_end = self.current_preview_segment_start

        full_text_chars = len(self._full_text())
        self.current_preview_snapshot_end = min(
            full_text_chars,
            self.current_preview_segment_start + len(raw_text),
        )
        self.current_preview_range_end = self.current_preview_snapshot_end

        # Full-text, cursor and saved-bookmark playback are intended to reach
        # the bottom. If more text is appended while they run, continue into it.
        self.current_preview_follow_end = bool(
            source_mode
            in {
                "all",
                "cursor",
                "saved_bookmark",
                "appended_tail",
            }
            and self.current_preview_snapshot_end >= full_text_chars
        )

        self.current_preview_segment_index = 0
        self.current_preview_segments_total = 0
        self.current_preview_word_length = 1
        if not preserve_existing_bookmark:
            self.clear_saved_preview_bookmark(
                reason="new_preview_started",
                schedule_save=False,
            )
        self.clear_preview_highlight()

        self.preview_button.configure(state="disabled")
        self.preview_cursor_button.configure(state="disabled")
        self.pause_preview_button.configure(state="normal")
        self.resume_preview_button.configure(state="disabled")
        self.stop_preview_button.configure(state="normal")

        if source_mode == "cursor":
            self.status_var.set("Начинаю чтение с позиции курсора…")
        elif selected_text:
            self.status_var.set("Воспроизвожу выделенный текст…")
        else:
            self.status_var.set("Воспроизвожу текст…")

        self.log_text_state(
            "preview_started",
            force=True,
        )

        self.app.logger.event(
            "preview_started",
            task_id=self.task_id,
            tab_id=self.workspace_id,
            preview_run_id=preview_run_id,
            chars=len(text),
            source_mode=source_mode,
            selected_text=selected_text,
            base_index=preview_base_index,
            voice=voice,
            audio_output=audio_output,
            rate=rate,
            pitch=pitch,
            volume=volume,
            preview_sapi_mode="continuous_blocks",
            preview_sapi_block_max_chars=PREVIEW_SAPI_BLOCK_MAX_CHARS,
        )

        def worker() -> None:
            try:
                def report_progress(info: dict) -> None:
                    self.app.events.put(
                        (
                            self.task_id,
                            "preview_segment",
                            {
                                **info,
                                "base_index": ui_base_index,
                            },
                        )
                    )

                def report_heartbeat(info: dict) -> None:
                    self.app.logger.event(
                        "preview_progress",
                        task_id=self.task_id,
                        tab_id=self.workspace_id,
                        preview_run_id=preview_run_id,
                        segment=info.get("segment"),
                        segments_total=info.get(
                            "segments_total"
                        ),
                        elapsed_sec=info.get("elapsed_sec"),
                        paused=info.get("paused"),
                        source_mode=source_mode,
                    )

                def report_state(state: str) -> None:
                    self.app.events.put(
                        (
                            self.task_id,
                            "preview_state",
                            {"state": state},
                        )
                    )

                def report_checkpoint(info: dict) -> None:
                    self.app.events.put(
                        (
                            self.task_id,
                            "preview_checkpoint",
                            {
                                **info,
                                "base_index": ui_base_index,
                                "preview_run_id": preview_run_id,
                            },
                        )
                    )

                def report_segment_done(info: dict) -> None:
                    self.app.events.put(
                        (
                            self.task_id,
                            "preview_segment_done",
                            {
                                **info,
                                "base_index": ui_base_index,
                                "preview_run_id": preview_run_id,
                            },
                        )
                    )

                preview_result = preview_sapi_text(
                    text=text,
                    voice_description=voice,
                    audio_output_description=audio_output,
                    rate=rate,
                    pitch=pitch,
                    volume=volume,
                    stop_event=self.preview_stop_event,
                    pause_event=self.preview_pause_event,
                    progress_callback=report_progress,
                    state_callback=report_state,
                    heartbeat_callback=report_heartbeat,
                    checkpoint_callback=report_checkpoint,
                    segment_done_callback=report_segment_done,
                )
                preview_result["preview_run_id"] = preview_run_id
                self.app.events.put(
                    (self.task_id, "preview_done", preview_result)
                )
            except Exception as exc:
                self.app.events.put(
                    (
                        self.task_id,
                        "preview_error",
                        {
                            "exc": exc,
                            "traceback": traceback.format_exc(),
                            "context": {
                                "chars": len(text),
                                "tab_id": self.workspace_id,
                                "preview_run_id": preview_run_id,
                                "source_mode": source_mode,
                                "selected_text": selected_text,
                                "base_index": preview_base_index,
                                "voice": voice,
                                "audio_output": audio_output,
                                "rate": rate,
                                "pitch": pitch,
                                "volume": volume,
                            },
                        },
                    )
                )

        self.preview_thread = threading.Thread(
            target=worker,
            name=f"preview-{self.task_id}",
            daemon=True,
        )
        self.preview_thread.start()

    def show_preview_segment(
        self,
        *,
        base_index: str,
        start: int,
        end: int,
        index: int,
        total: int,
        text: str,
    ) -> None:
        try:
            self.text_box.tag_remove("preview_current", "1.0", "end")

            adjusted_start = max(
                0,
                int(start) - int(self.preview_deleted_chars),
            )
            adjusted_end = max(
                adjusted_start,
                int(end) - int(self.preview_deleted_chars),
            )

            start_index = self.text_box.index(
                f"{base_index}+{adjusted_start}c"
            )
            end_index = self.text_box.index(
                f"{base_index}+{adjusted_end}c"
            )

            self.text_box.tag_add(
                "preview_current",
                start_index,
                end_index,
            )
            self.text_box.tag_raise("preview_current")
            self.text_box.see(start_index)

            short_text = " ".join(str(text).split())
            if len(short_text) > 110:
                short_text = short_text[:107] + "..."

            self.current_preview_segment_text = short_text
            self.current_preview_segment_start = (
                self.current_preview_base_text_offset
                + adjusted_start
            )
            self.current_preview_segment_end = (
                self.current_preview_base_text_offset
                + adjusted_end
            )
            self.current_preview_segment_index = int(index)
            self.current_preview_segments_total = int(total)
            self.current_preview_word_length = max(
                1,
                self.current_preview_segment_end
                - self.current_preview_segment_start,
            )
            self.status_var.set(
                f"Читаю {index}/{total}: {short_text}"
            )
        except tk.TclError:
            # Если пользователь успел изменить текст во время чтения, не ломаем
            # само воспроизведение из-за невозможности подсветить старый диапазон.
            pass

    def _delete_pending_read_segment(self) -> int:
        if not self.preview_pending_delete_active:
            return 0

        try:
            start_index = self.text_box.index(
                self.preview_pending_delete_start_mark
            )
            end_index = self.text_box.index(
                self.preview_pending_delete_end_mark
            )

            if not self.text_box.compare(start_index, "<", end_index):
                self.preview_pending_delete_active = False
                return 0

            delete_end = end_index
            try:
                limit = self.text_box.index(
                    self.preview_snapshot_end_mark
                )
            except tk.TclError:
                limit = "end-1c"

            while self.text_box.compare(delete_end, "<", limit):
                ch = self.text_box.get(
                    delete_end,
                    f"{delete_end}+1c",
                )
                if not ch or not ch.isspace():
                    break
                delete_end = self.text_box.index(
                    f"{delete_end}+1c"
                )

            # Do not use Text.count(..., "chars") here. Tk 8.6 counts each
            # emoji as two UTF-16 units, but build_preview_segments() supplies
            # Python character offsets. Mixing both counters shifted every
            # later sentence boundary and left fragments of read text behind.
            deleted_chars = len(
                self.text_box.get(
                    start_index,
                    delete_end,
                )
            )

            if deleted_chars:
                self.text_box.delete(start_index, delete_end)
                self.preview_deleted_chars += deleted_chars
                self.workspace_dirty = True

                # During continuous reading do not rewrite workspace after every
                # sentence. The first delete schedules a save no later than the
                # 15-second checkpoint; later deletes cannot postpone it.
                self.app.schedule_workspace_save(
                    delay_ms=int(
                        WORKSPACE_READING_SAVE_INTERVAL_SEC
                        * 1000
                    ),
                    keep_earliest=True,
                )
                self._accumulate_delete_log(
                    deleted_chars=deleted_chars,
                    deleted_segments=1,
                )
                self.schedule_queue_status_refresh()

            self.preview_pending_delete_active = False
            return deleted_chars

        except tk.TclError:
            self.preview_pending_delete_active = False
            return 0

    def handle_preview_segment_done(self, info: dict) -> None:
        try:
            spoken_text = str(info.get("text") or "")
            self.session_read_segments += 1
            self.session_read_chars += len(spoken_text)
            self.schedule_queue_status_refresh()
        except Exception:
            pass

        if not bool(self.delete_read_text_var.get()):
            return

        run_id = str(info.get("preview_run_id") or "")
        if (
            run_id
            and self.current_preview_run_id
            and run_id != self.current_preview_run_id
        ):
            return

        # Keep the newest completed sentence as a one-sentence buffer.
        self._delete_pending_read_segment()

        try:
            base_index = str(
                info.get("base_index")
                or self.current_preview_base_index
            )
            original_start = int(info.get("start") or 0)
            original_end = int(info.get("end") or original_start)

            adjusted_start = max(
                0,
                original_start - self.preview_deleted_chars,
            )
            adjusted_end = max(
                adjusted_start,
                original_end - self.preview_deleted_chars,
            )

            start_index = self.text_box.index(
                f"{base_index}+{adjusted_start}c"
            )
            end_index = self.text_box.index(
                f"{base_index}+{adjusted_end}c"
            )

            self.text_box.mark_set(
                self.preview_pending_delete_start_mark,
                start_index,
            )
            self.text_box.mark_gravity(
                self.preview_pending_delete_start_mark,
                "left",
            )
            self.text_box.mark_set(
                self.preview_pending_delete_end_mark,
                end_index,
            )
            self.text_box.mark_gravity(
                self.preview_pending_delete_end_mark,
                "right",
            )
            self.preview_pending_delete_active = True

        except (tk.TclError, TypeError, ValueError):
            self.preview_pending_delete_active = False

    def finalize_delete_read_text(self) -> None:
        if bool(self.delete_read_text_var.get()):
            self._delete_pending_read_segment()

    def _unread_appended_tail(self) -> tuple[str, str] | None:
        """
        Return (text, start_index) when new text was appended below the snapshot
        currently owned by SAPI and playback is supposed to follow the end.
        """
        if not self.current_preview_follow_end:
            return None

        full_text = self._full_text()
        try:
            start_index = self.text_box.index(
                self.preview_snapshot_end_mark
            )
        except tk.TclError:
            start_index = self._index_from_text_offset(
                max(
                    0,
                    min(
                        int(self.current_preview_snapshot_end),
                        len(full_text),
                    ),
                )
            )

        end_index = self.text_box.index("end-1c")
        if not self.text_box.compare(
            start_index,
            "<",
            end_index,
        ):
            return None

        tail = self.text_box.get(start_index, end_index)
        if not normalize_text(tail):
            return None

        return tail, start_index

    def continue_appended_tail_if_needed(self) -> bool:
        """
        Start a new SAPI preview immediately after the snapshot that just
        finished, so appended chapters are read without the user pressing Play
        again.
        """
        tail_info = self._unread_appended_tail()
        if tail_info is None:
            return False

        tail, start_index = tail_info
        previous_run_id = self.current_preview_run_id

        self._start_preview_worker(
            raw_text=tail,
            preview_base_index=start_index,
            source_mode="appended_tail",
            selected_text=False,
            preserve_existing_bookmark=True,
        )

        self.app.logger.event(
            "preview_appended_tail_continued",
            task_id=self.task_id,
            tab_id=self.workspace_id,
            previous_preview_run_id=previous_run_id,
            next_preview_run_id=self.current_preview_run_id,
            start_offset=self._text_offset_from_index(start_index),
            tail_chars=len(tail),
        )
        return True

    def clear_preview_highlight(self) -> None:
        try:
            self.text_box.tag_remove(
                "preview_current",
                "1.0",
                "end",
            )
        except tk.TclError:
            pass

    def pause_preview(self) -> None:
        if not (self.preview_thread and self.preview_thread.is_alive()):
            return
        if self.preview_pause_event.is_set():
            return

        self.preview_pause_event.set()
        self.preview_is_paused = True
        self.pause_preview_button.configure(state="disabled")
        self.resume_preview_button.configure(state="normal")
        self.stop_preview_button.configure(state="normal")

        # Save a safe sentence-level checkpoint immediately. The SAPI worker
        # then refines it to the current word and saves again.
        self.save_current_preview_bookmark(
            reason="pause_requested_fallback",
            absolute_offset=self.current_preview_segment_start,
            word_length=max(1, self.current_preview_word_length),
            force_save=True,
            log_event=False,
            accuracy="fallback_sentence",
        )
        self.flush_read_delete_log(
            reason="pause",
            force=True,
        )
        self.log_text_state(
            "pause_requested",
            force=True,
        )

        current = self.current_preview_segment_text
        self.status_var.set(
            f"Пауза: при «Продолжить» чтение начинается на одно предложение назад. {current}" if current else "Пауза."
        )

        self.app.logger.event(
            "preview_pause_requested",
            task_id=self.task_id,
            tab_id=self.workspace_id,
            preview_run_id=self.current_preview_run_id,
            bookmark_char_offset=(
                self.preview_bookmark.get("char_offset")
                if isinstance(self.preview_bookmark, dict)
                else None
            ),
            bookmark_accuracy="fallback_sentence_until_sapi_checkpoint",
            audio_output=self.audio_output_var.get().strip()
            or DEFAULT_AUDIO_OUTPUT_LABEL,
        )

    def resume_preview(self) -> None:
        # Same-process resume is intentionally implemented as a controlled
        # restart, NOT voice.Resume(). Some SAPI voices can finish/advance the
        # queued sentence while Pause/Resume is being handled, which looks like
        # a jump to the next sentence. We purge that paused stream and restart
        # one sentence BEFORE the paused one.
        if self.preview_thread and self.preview_thread.is_alive():
            if not self.preview_pause_event.is_set():
                return

            # The Pause button saves a fallback bookmark immediately and the SAPI
            # worker normally refines it. Keep a safe bookmark even if the user
            # clicked Continue extremely quickly.
            if not self._bookmark_is_valid():
                self.save_current_preview_bookmark(
                    reason="resume_previous_sentence_fallback",
                    absolute_offset=self.current_preview_segment_start,
                    word_length=max(
                        1,
                        self.current_preview_word_length,
                    ),
                    force_save=True,
                )

            self.preview_resume_previous_pending = True
            self.preview_keep_bookmark_after_stop = True
            self.preview_resume_from_run_id = (
                self.current_preview_run_id
            )
            self.current_preview_finish_reason = (
                "resume_rewind"
            )

            bookmark = (
                dict(self.preview_bookmark)
                if isinstance(self.preview_bookmark, dict)
                else {}
            )
            paused_sentence_offset = int(
                bookmark.get(
                    "sentence_start_offset",
                    self.current_preview_segment_start,
                )
                or 0
            )
            previous_offset = bookmark.get(
                "previous_sentence_start_offset"
            )
            if previous_offset is None:
                previous_offset = previous_preview_segment_start(
                    self._full_text(),
                    paused_sentence_offset,
                )

            self.app.logger.event(
                "preview_resume_requested",
                task_id=self.task_id,
                tab_id=self.workspace_id,
                preview_run_id=self.current_preview_run_id,
                resume_mode="restart_previous_sentence",
                paused_sentence_offset=paused_sentence_offset,
                resume_previous_sentence_offset=int(
                    previous_offset
                ),
                audio_output=self.audio_output_var.get().strip()
                or DEFAULT_AUDIO_OUTPUT_LABEL,
            )

            # Wake the paused worker, let it Resume only for purge/cleanup, and
            # stop this old SAPI stream. The event loop starts the replacement
            # preview as soon as COM has been released.
            self.preview_stop_event.set()
            self.preview_pause_event.clear()

            self.preview_is_paused = False
            self.pause_preview_button.configure(state="disabled")
            self.resume_preview_button.configure(state="disabled")
            self.stop_preview_button.configure(state="disabled")
            self.status_var.set(
                "Возвращаюсь на одно предложение назад…"
            )
            self.flush_read_delete_log(
                reason="resume_rewind",
                force=True,
            )
            self.log_text_state(
                "resume_rewind_requested",
                force=True,
            )
            return

        # After a program restart there is no live SAPI object, so immediately
        # start a new preview one sentence before the persisted pause sentence.
        if self.resume_saved_preview():
            return

    def apply_preview_state(self, state: str) -> None:
        if state == "paused":
            self.preview_is_paused = True
            self.pause_preview_button.configure(state="disabled")
            self.resume_preview_button.configure(state="normal")
            self.stop_preview_button.configure(state="normal")
            current = self.current_preview_segment_text
            self.status_var.set(
                f"Пауза: {current}" if current else "Пауза."
            )

        elif state == "running":
            self.preview_is_paused = False
            self.pause_preview_button.configure(state="normal")
            self.resume_preview_button.configure(state="disabled")

    def stop_preview(
        self,
        *,
        preserve_bookmark: bool = False,
        reason: str = "user_stop",
    ) -> None:
        if reason == "user_stop":
            self.preview_resume_previous_pending = False

        finish_reason = {
            "user_stop": "user_stop",
            "another_tab_started_preview": "another_tab",
            "app_exit": "app_exit",
            "mp3_creation_started": "mp3_creation",
            "mp3_recovery_started": "mp3_creation",
            "clear_text": "clear_text",
            "clipboard_paste": "text_changed",
        }.get(reason, reason or "other")
        if (
            self.preview_thread
            and self.preview_thread.is_alive()
        ):
            self.current_preview_finish_reason = finish_reason

        running = bool(
            self.preview_thread and self.preview_thread.is_alive()
        )

        if preserve_bookmark and running:
            # If SAPI already saved the exact paused word, do not overwrite it
            # with the coarser sentence-start fallback.
            if not (
                self.preview_pause_event.is_set()
                and self._bookmark_is_valid()
            ):
                self.save_current_preview_bookmark(
                    reason=reason,
                    absolute_offset=self.current_preview_segment_start,
                    word_length=max(1, self.current_preview_word_length),
                    force_save=True,
                )
            self.preview_keep_bookmark_after_stop = True
        elif not preserve_bookmark:
            self.clear_saved_preview_bookmark(
                reason=reason,
                schedule_save=True,
            )
            self.preview_keep_bookmark_after_stop = False

        if running:
            self.app.logger.event(
                "preview_stop_requested",
                task_id=self.task_id,
                tab_id=self.workspace_id,
                preview_run_id=self.current_preview_run_id,
                paused=bool(self.preview_pause_event.is_set()),
                preserve_bookmark=preserve_bookmark,
                reason=reason,
                audio_output=self.audio_output_var.get().strip()
                or DEFAULT_AUDIO_OUTPUT_LABEL,
            )
            self.preview_stop_event.set()
            self.preview_pause_event.clear()
            self.status_var.set("Останавливаю воспроизведение…")
        elif not preserve_bookmark:
            self.clear_preview_highlight()
            self.preview_is_paused = False
            self.pause_preview_button.configure(state="disabled")
            self.resume_preview_button.configure(state="disabled")
            self.stop_preview_button.configure(state="disabled")
            if not self.conversion_running():
                self.status_var.set("Готово к работе.")

        self.flush_read_delete_log(
            reason=finish_reason,
            force=True,
        )
        self.log_text_state(
            f"preview_stop_{finish_reason}",
            force=True,
        )

    def preview_finished_ui(self, *, stopped: bool = False) -> None:
        keep_bookmark = bool(
            stopped
            and self.preview_keep_bookmark_after_stop
            and self._bookmark_is_valid()
        )

        self.preview_pause_event.clear()
        self.current_preview_run_id = None
        self.preview_button.configure(state="normal")
        self.preview_cursor_button.configure(state="normal")
        self.pause_preview_button.configure(state="disabled")

        if keep_bookmark:
            self.preview_is_paused = True
            self.resume_preview_button.configure(state="normal")
            self.stop_preview_button.configure(state="normal")
            self._restore_preview_bookmark_ui()
        else:
            if not stopped:
                self.clear_saved_preview_bookmark(
                    reason="preview_completed",
                    schedule_save=True,
                )
            self.clear_preview_highlight()
            self.preview_is_paused = False
            self.current_preview_segment_text = ""
            self.resume_preview_button.configure(state="disabled")
            self.stop_preview_button.configure(state="disabled")
            if not self.conversion_running():
                self.status_var.set("Готово к работе.")

        self.preview_keep_bookmark_after_stop = False
        self.flush_read_delete_log(
            reason="preview_finished_ui",
            force=True,
        )
        self.schedule_queue_status_refresh()

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

        finally:
            self.current_job_stage = "cleanup"
            if (
                work_dir is not None
                and not preserve_recovery_on_exit
            ):
                shutil.rmtree(work_dir, ignore_errors=True)
            self.current_job_stage = "idle"
            pythoncom.CoUninitialize()

    def set_job_idle(self) -> None:
        self.start_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")
        self.current_output_path = None
        self.current_mp3_text_backup_path = None
        self.current_run_id = None
        self.current_job_stage = "idle"

    def conversion_running(self) -> bool:
        return bool(self.worker and self.worker.is_alive())


class TTSApp:
    def __init__(self, root: tk.Tk) -> None:
        ensure_help_files()

        self.root = root
        self.settings = load_settings()
        self.logger = ProblemLogger()

        try:
            removed_backups, failed_backups = (
                cleanup_old_mp3_text_backups()
            )
            self.logger.event(
                "mp3_text_backup_cleanup",
                retention_days=(
                    MP3_TEXT_BACKUP_RETENTION_DAYS
                ),
                removed=removed_backups,
                failed=failed_backups,
                backup_dir=str(MP3_TEXT_BACKUPS_DIR),
            )
        except Exception as exc:
            self.logger.error(
                task_id="app",
                stage="mp3_text_backup_cleanup",
                exc=exc,
                traceback_text=traceback.format_exc(),
                context={
                    "backup_dir": str(
                        MP3_TEXT_BACKUPS_DIR
                    ),
                    "retention_days": (
                        MP3_TEXT_BACKUP_RETENTION_DAYS
                    ),
                },
            )

        # One Speed value for the whole application and all tabs.
        self.global_rate_var = tk.IntVar(
            value=clamp_int(self.settings.get("rate"), RATE_MIN, RATE_MAX, 0)
        )
        self._global_rate_syncing = False
        self.global_rate_var.trace_add("write", self._on_global_rate_changed)

        # One Pitch value for the whole application and all tabs.
        self.global_pitch_var = tk.IntVar(
            value=clamp_int(self.settings.get("pitch"), PITCH_MIN, PITCH_MAX, 0)
        )
        self._global_pitch_syncing = False
        self.global_pitch_var.trace_add("write", self._on_global_pitch_changed)

        self.global_audio_output_var = tk.StringVar(
            value=str(
                self.settings.get("audio_output")
                or DEFAULT_AUDIO_OUTPUT_LABEL
            )
        )
        self.global_audio_output_var.trace_add(
            "write",
            self._on_global_audio_output_changed,
        )
        self.audio_outputs: list[str] = []
        self.audio_output_choices: list[str] = [DEFAULT_AUDIO_OUTPUT_LABEL]

        self.root.title(
            f"{APP_TITLE} — {APP_VERSION}"
        )
        self.root.minsize(900, 680)

        try:
            self.root.geometry(
                str(self.settings.get("window_geometry") or "1200x820")
            )
        except tk.TclError:
            self.root.geometry("1200x820")

        self.events: queue.Queue[
            tuple[str, str, object]
        ] = queue.Queue()

        self.voices: list[str] = []
        self.tabs: dict[str, TaskTab] = {}
        self.task_counter = 0
        self.settings_after_id: str | None = None
        self.workspace_after_id: str | None = None
        self.workspace_save_due_perf: float | None = None
        self.workspace_last_saved_perf = time.perf_counter()
        self.context_tab_index: int | None = None
        self.shutdown_in_progress = False
        self.shutdown_deadline = 0.0

        # One low-level monitor per ASSIGNED tab hotkey. A monitor's callback is
        # permanently bound to that tab's workspace_id, so changing the visible
        # active tab cannot redirect captured text to the wrong place.
        self.tab_hotkey_monitors: dict[
            str,
            GlobalCopyHotkeyMonitor,
        ] = {}

        # Состояние берём из реальной записи Windows, а не только из JSON.
        autostart_enabled = False
        if os.name == "nt":
            autostart_enabled = windows_autostart_is_enabled()
            if autostart_enabled:
                # Если файл/EXE перенесли, при ручном запуске обновляем путь
                # существующей записи автозапуска на текущую копию программы.
                try:
                    set_windows_autostart(True)
                except Exception:
                    pass
        self.autostart_windows_var = tk.BooleanVar(
            value=autostart_enabled
        )
        self.settings["autostart_windows"] = autostart_enabled

        self._build_ui()
        self._restore_workspace()
        self._activate_restored_tab_hotkeys()

        legacy_removed = cleanup_legacy_temp_workdirs()
        if legacy_removed:
            self.logger.event(
                "legacy_temp_cleanup",
                removed_dirs=legacy_removed,
                retention_days=RECOVERY_RETENTION_DAYS,
            )

        self.root.after(100, self._process_events)
        self.root.after(900, self._offer_recovery_jobs)
        self._load_voices_async()
        self._load_audio_outputs_async()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=8)
        outer.pack(fill="both", expand=True)

        top = ttk.Frame(outer)
        top.pack(fill="x", pady=(0, 7))

        ttk.Label(
            top,
            text=f"Версия: {APP_VERSION}",
        ).pack(side="right")

        ttk.Button(
            top,
            text="＋ Новая вкладка",
            command=self.new_tab,
        ).pack(side="left")

        self.autostart_checkbutton = ttk.Checkbutton(
            top,
            text="Автозапуск с Windows",
            variable=self.autostart_windows_var,
            command=self.toggle_windows_autostart,
        )
        self.autostart_checkbutton.pack(
            side="left",
            padx=(14, 0),
        )
        if os.name != "nt":
            self.autostart_checkbutton.configure(state="disabled")

        self.notebook = ClosableNotebook(outer)
        self.notebook.pack(fill="both", expand=True)

        self.notebook.bind(
            "<<NotebookCloseRequested>>",
            self._on_notebook_close_requested,
            add="+",
        )
        self.notebook.bind(
            "<Button-3>",
            self._on_tab_right_click,
            add="+",
        )
        self.notebook.bind(
            "<<NotebookTabChanged>>",
            lambda _event: self.schedule_workspace_save(),
            add="+",
        )

        self.tab_menu = tk.Menu(self.root, tearoff=False)
        self.tab_menu.add_command(
            label="Переименовать вкладку",
            command=self.rename_context_tab,
        )
        self.tab_menu.add_command(
            label="Закрыть вкладку",
            command=self.close_context_tab,
        )

    def _restore_workspace(self) -> None:
        entries: list[dict] = []
        selected_index = 0

        if WORKSPACE_FILE.exists():
            try:
                payload = json.loads(
                    WORKSPACE_FILE.read_text(encoding="utf-8")
                )
                if isinstance(payload, dict):
                    raw_entries = payload.get("tabs", [])
                    if isinstance(raw_entries, list):
                        entries = [
                            item for item in raw_entries
                            if isinstance(item, dict)
                        ]
                    selected_index = clamp_int(
                        payload.get("selected_index", 0),
                        0,
                        max(0, len(entries) - 1),
                        0,
                    )
            except Exception as exc:
                self.logger.error(
                    task_id="app",
                    stage="restore_workspace_manifest",
                    exc=exc,
                    traceback_text=traceback.format_exc(),
                    context={"workspace_file": str(WORKSPACE_FILE)},
                )

        has_per_tab_hotkeys = any(
            "copy_hotkey" in entry
            for entry in entries
        )
        legacy_hotkey = TAB_HOTKEY_NONE_LABEL
        if (
            entries
            and not has_per_tab_hotkeys
            and bool(
                self.settings.get(
                    "global_copy_enabled",
                    False,
                )
            )
        ):
            legacy_hotkey = str(
                self.settings.get("global_copy_hotkey")
                or TAB_HOTKEY_NONE_LABEL
            ).strip() or TAB_HOTKEY_NONE_LABEL

        for entry_index, entry in enumerate(entries):
            workspace_id = str(
                entry.get("workspace_id") or uuid.uuid4().hex
            )
            text_file_name = str(
                entry.get("text_file") or f"{workspace_id}.txt"
            )
            text_path = WORKSPACE_TEXT_DIR / Path(text_file_name).name

            try:
                restored_text = (
                    text_path.read_text(encoding="utf-8")
                    if text_path.exists()
                    else ""
                )
            except Exception as exc:
                restored_text = ""
                self.logger.error(
                    task_id="app",
                    stage="restore_workspace_text",
                    exc=exc,
                    traceback_text=traceback.format_exc(),
                    context={"text_path": str(text_path)},
                )

            restored_settings = {
                "voice": entry.get("voice", self.settings["voice"]),
                # Speed and Pitch are global now. Old per-tab values are
                # intentionally ignored during migration.
                "rate": self.settings["rate"],
                "pitch": self.settings["pitch"],
                "audio_output": self.settings["audio_output"],
                "volume": entry.get("volume", self.settings["volume"]),
                "bitrate": entry.get("bitrate", self.settings["bitrate"]),
                "delete_read_text": bool(
                    entry.get("delete_read_text", False)
                ),
                "auto_read_hotkey_text": bool(
                    entry.get("auto_read_hotkey_text", True)
                ),
                "copy_hotkey": (
                    entry.get(
                        "copy_hotkey",
                        TAB_HOTKEY_NONE_LABEL,
                    )
                    if has_per_tab_hotkeys
                    else (
                        legacy_hotkey
                        if entry_index == selected_index
                        else TAB_HOTKEY_NONE_LABEL
                    )
                ),
            }

            raw_title = str(entry.get("title") or "")
            stored_custom_title = entry.get("custom_title")
            if isinstance(stored_custom_title, bool):
                custom_title = stored_custom_title
            else:
                # Migration from 2.2: "Задача 37", "Вкладка 18", etc. were
                # automatic names and should be renumbered, not preserved.
                custom_title = not is_automatic_tab_title(raw_title)

            self.new_tab(
                title=raw_title,
                workspace_id=workspace_id,
                restored_text=restored_text,
                restored_settings=restored_settings,
                restored_preview_bookmark=(
                    entry.get("preview_bookmark")
                    if isinstance(entry.get("preview_bookmark"), dict)
                    else None
                ),
                custom_title=custom_title,
                save_workspace=False,
            )

        if not self.tabs:
            self.new_tab(save_workspace=False)

        # Normalize all non-custom tabs after restoration. This also fixes old
        # titles such as "Задача 37" / "Задача 38".
        self.renumber_default_tabs()

        try:
            if self.notebook.tabs():
                self.notebook.select(selected_index)
        except tk.TclError:
            pass

        self.settings["global_copy_enabled"] = False
        self.schedule_workspace_save()

    def new_tab(
        self,
        *,
        title: str | None = None,
        workspace_id: str | None = None,
        restored_text: str = "",
        restored_settings: dict | None = None,
        restored_preview_bookmark: dict | None = None,
        custom_title: bool = False,
        save_workspace: bool = True,
    ) -> TaskTab:
        # task_counter remains only an internal unique-ish sequence for diagnostics.
        # It is NOT used as the visible tab number anymore.
        self.task_counter += 1
        task = TaskTab(
            self,
            self.task_counter,
            workspace_id=workspace_id,
            restored_title=title or "Вкладка",
            restored_text=restored_text,
            restored_settings=restored_settings,
            restored_preview_bookmark=restored_preview_bookmark,
            custom_title=custom_title,
        )

        self.tabs[task.task_id] = task
        self.notebook.add(
            task.frame,
            text=task.restored_title,
        )
        self.notebook.select(task.frame)

        if self.voices:
            task.apply_voice_list(self.voices)
        task.audio_output_combo["values"] = self.audio_output_choices

        self.renumber_default_tabs()

        self.logger.event(
            "tab_created",
            task_id=task.task_id,
            tab_id=task.workspace_id,
            workspace_id=task.workspace_id,
            internal_seq=self.task_counter,
            visible_tab_index=self.visible_tab_index(task),
            visible_tab_title=task.tab_title(),
            restored=bool(workspace_id),
            copy_hotkey=task.copy_hotkey_var.get(),
        )

        if save_workspace:
            self.schedule_workspace_save()

        return task

    def renumber_default_tabs(self) -> None:
        """
        Renumber only automatically named tabs as Вкладка 1, Вкладка 2, ...
        Custom names are preserved exactly as the user set them.

        Example:
            Вкладка 1 | Моя книга | Вкладка 2 | Вкладка 3
        """
        next_number = 1

        for widget_name in self.notebook.tabs():
            task = None
            for candidate in self.tabs.values():
                if str(candidate.frame) == widget_name:
                    task = candidate
                    break

            if task is None or task.custom_title:
                continue

            wanted = f"Вкладка {next_number}"
            if task.tab_title() != wanted:
                task.set_tab_title(
                    wanted,
                    custom=False,
                    schedule_save=False,
                )
            next_number += 1

        self.schedule_workspace_save()

    def task_for_tab_index(self, index: int) -> TaskTab | None:
        try:
            tabs = self.notebook.tabs()
            if not (0 <= index < len(tabs)):
                return None
            widget_name = tabs[index]
        except (tk.TclError, IndexError):
            return None

        for task in self.tabs.values():
            if str(task.frame) == widget_name:
                return task

        return None

    def visible_tab_index(self, task: TaskTab) -> int | None:
        try:
            return int(self.notebook.index(task.frame)) + 1
        except tk.TclError:
            return None

    def current_tab(self) -> TaskTab | None:
        selected = self.notebook.select()
        if not selected:
            return None

        for task in self.tabs.values():
            if str(task.frame) == selected:
                return task

        return None

    def _on_notebook_close_requested(self, _event=None) -> None:
        index = self.notebook.close_requested_index
        self.notebook.close_requested_index = None

        if index is not None:
            self.close_tab_by_index(index)

    def _on_tab_right_click(self, event) -> str | None:
        try:
            index = self.notebook.index(
                f"@{event.x},{event.y}"
            )
        except tk.TclError:
            return None

        self.context_tab_index = index

        try:
            self.notebook.select(index)
        except tk.TclError:
            pass

        try:
            self.tab_menu.tk_popup(
                event.x_root,
                event.y_root,
            )
        finally:
            self.tab_menu.grab_release()

        return "break"

    def rename_context_tab(self) -> None:
        index = self.context_tab_index
        if index is None:
            return

        task = self.task_for_tab_index(index)
        if task is None:
            return

        new_name = simpledialog.askstring(
            "Переименовать вкладку",
            "Новое имя вкладки:",
            initialvalue=task.tab_title(),
            parent=self.root,
        )
        if new_name is None:
            return

        new_name = new_name.strip()
        if not new_name:
            messagebox.showwarning(
                APP_TITLE,
                "Имя вкладки не может быть пустым.",
            )
            return

        old_name = task.tab_title()
        task.set_tab_title(new_name, custom=True)
        self.renumber_default_tabs()
        self.logger.event(
            "tab_renamed",
            task_id=task.task_id,
            tab_id=task.workspace_id,
            workspace_id=task.workspace_id,
            visible_tab_index=self.visible_tab_index(task),
            old_title=old_name,
            new_title=new_name,
        )

    def close_context_tab(self) -> None:
        index = self.context_tab_index
        if index is not None:
            self.close_tab_by_index(index)

    def close_tab_by_index(self, index: int) -> None:
        task = self.task_for_tab_index(index)
        if task is None:
            return

        if task.conversion_running():
            messagebox.showwarning(
                APP_TITLE,
                "Эта вкладка сейчас создаёт MP3.\n"
                "Сначала остановите задачу и дождитесь её завершения.",
            )
            return

        task.stop_preview()
        self._stop_tab_hotkey_monitor(
            task.workspace_id
        )

        self.logger.event(
            "tab_closed",
            task_id=task.task_id,
            tab_id=task.workspace_id,
            workspace_id=task.workspace_id,
            visible_tab_index=self.visible_tab_index(task),
            title=task.tab_title(),
        )

        text_path = WORKSPACE_TEXT_DIR / f"{task.workspace_id}.txt"

        try:
            self.notebook.forget(task.frame)
            task.frame.destroy()
        finally:
            self.tabs.pop(task.task_id, None)

        try:
            text_path.unlink(missing_ok=True)
        except OSError as exc:
            self.logger.event(
                "workspace_tab_file_delete_failed",
                path=str(text_path),
                error=f"{type(exc).__name__}: {exc}",
            )

        if not self.tabs:
            self.new_tab(save_workspace=False)

        self.renumber_default_tabs()
        self.save_workspace()

    def schedule_workspace_save(
        self,
        delay_ms: int = 1000,
        *,
        keep_earliest: bool = False,
    ) -> None:
        delay_ms = max(0, int(delay_ms))
        due_perf = time.perf_counter() + delay_ms / 1000.0

        if (
            self.workspace_after_id
            and keep_earliest
            and self.workspace_save_due_perf is not None
            and self.workspace_save_due_perf <= due_perf
        ):
            return

        if self.workspace_after_id:
            try:
                self.root.after_cancel(self.workspace_after_id)
            except Exception:
                pass

        self.workspace_save_due_perf = due_perf
        self.workspace_after_id = self.root.after(
            delay_ms,
            self.save_workspace,
        )

    def save_workspace(self) -> None:
        self.workspace_after_id = None
        self.workspace_save_due_perf = None
        save_started = time.perf_counter()
        written_text_bytes = 0
        dirty_tabs_written = 0

        try:
            WORKSPACE_TEXT_DIR.mkdir(
                parents=True,
                exist_ok=True,
            )

            ordered_tasks: list[TaskTab] = []
            for widget_name in self.notebook.tabs():
                for task in self.tabs.values():
                    if str(task.frame) == widget_name:
                        ordered_tasks.append(task)
                        break

            records: list[dict] = []

            for task in ordered_tasks:
                text_path = WORKSPACE_TEXT_DIR / f"{task.workspace_id}.txt"

                if task.workspace_dirty or not text_path.exists():
                    text = task.text_box.get("1.0", "end-1c")
                    atomic_write_text(text_path, text)
                    written_text_bytes += len(
                        text.encode("utf-8")
                    )
                    dirty_tabs_written += 1
                    task.workspace_dirty = False

                records.append(task.workspace_record())

            selected_index = 0
            try:
                selected_index = self.notebook.index(
                    self.notebook.select()
                )
            except tk.TclError:
                pass

            manifest = {
                "schema": 3,
                "saved_at": now_iso(),
                "selected_index": selected_index,
                "tabs": records,
            }
            atomic_write_json(WORKSPACE_FILE, manifest)

            elapsed = time.perf_counter() - save_started
            self.workspace_last_saved_perf = time.perf_counter()
            if elapsed >= WORKSPACE_SLOW_SAVE_SEC:
                self.logger.event(
                    "workspace_save_slow",
                    duration_ms=round(elapsed * 1000.0, 1),
                    dirty_tabs_written=dirty_tabs_written,
                    text_bytes_written=written_text_bytes,
                    tabs_total=len(records),
                )

        except Exception as exc:
            self.logger.error(
                task_id="app",
                stage="save_workspace",
                exc=exc,
                traceback_text=traceback.format_exc(),
                context={
                    "workspace_file": str(WORKSPACE_FILE),
                    "tabs_count": len(self.tabs),
                },
            )

    def _task_by_workspace_id(
        self,
        workspace_id: str,
    ) -> TaskTab | None:
        for task in self.tabs.values():
            if task.workspace_id == workspace_id:
                return task
        return None

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


    def _normalized_tab_hotkey(
        self,
        value: str,
    ) -> str:
        raw = str(value or "").strip()
        if (
            not raw
            or raw.casefold()
            == TAB_HOTKEY_NONE_LABEL.casefold()
        ):
            return TAB_HOTKEY_NONE_LABEL

        parsed = GlobalCopyHotkeyMonitor.parse_hotkey(
            raw
        )
        return str(parsed["text"])

    def _task_hotkey_owner(
        self,
        normalized_hotkey: str,
        *,
        exclude_workspace_id: str | None = None,
    ) -> TaskTab | None:
        wanted = str(normalized_hotkey or "").strip()
        if (
            not wanted
            or wanted == TAB_HOTKEY_NONE_LABEL
        ):
            return None

        for task in self.tabs.values():
            if (
                exclude_workspace_id
                and task.workspace_id
                == exclude_workspace_id
            ):
                continue
            if task.applied_copy_hotkey == wanted:
                return task
        return None

    def _stop_tab_hotkey_monitor(
        self,
        workspace_id: str,
    ) -> None:
        monitor = self.tab_hotkey_monitors.pop(
            workspace_id,
            None,
        )
        if monitor is not None:
            try:
                monitor.stop()
            except Exception:
                pass

    def apply_tab_copy_hotkey(
        self,
        task: TaskTab,
        *,
        silent: bool = False,
        save_workspace: bool = True,
    ) -> bool:
        """
        Bind one unique global hotkey to one exact tab.

        Routing never depends on which tab is currently visible.  "Нет" removes
        the binding. Aliases such as Й+Ц and Q+W normalize to the same physical
        keys, so they are also treated as duplicates.
        """
        previous = (
            task.applied_copy_hotkey
            or TAB_HOTKEY_NONE_LABEL
        )

        try:
            normalized = self._normalized_tab_hotkey(
                task.copy_hotkey_var.get()
            )
        except Exception as exc:
            task.copy_hotkey_var.set(previous)
            task.copy_hotkey_status_var.set(
                f"Ошибка: {exc}"
            )
            self.logger.event(
                "tab_copy_hotkey_invalid",
                task_id=task.task_id,
                tab_id=task.workspace_id,
                requested_hotkey=task.copy_hotkey_var.get(),
                error=f"{type(exc).__name__}: {exc}",
            )
            if not silent:
                messagebox.showerror(
                    "Горячая клавиша",
                    str(exc),
                )
            return False

        if normalized == TAB_HOTKEY_NONE_LABEL:
            self._stop_tab_hotkey_monitor(
                task.workspace_id
            )
            task.applied_copy_hotkey = (
                TAB_HOTKEY_NONE_LABEL
            )
            task.copy_hotkey_var.set(
                TAB_HOTKEY_NONE_LABEL
            )
            task.copy_hotkey_status_var.set(
                "Горячая клавиша не назначена."
            )
            self.logger.event(
                "tab_copy_hotkey_cleared",
                task_id=task.task_id,
                tab_id=task.workspace_id,
                tab_title=task.tab_title(),
            )
            if save_workspace:
                self.schedule_workspace_save()
            return True

        owner = self._task_hotkey_owner(
            normalized,
            exclude_workspace_id=task.workspace_id,
        )
        if owner is not None:
            task.copy_hotkey_var.set(previous)
            message = (
                f'Горячая клавиша {normalized} уже назначена '
                f'вкладке «{owner.tab_title()}». '
                "Выберите другую клавишу."
            )
            task.copy_hotkey_status_var.set(
                f"{normalized} уже занята другой вкладкой."
            )
            self.logger.event(
                "tab_copy_hotkey_conflict",
                task_id=task.task_id,
                tab_id=task.workspace_id,
                requested_hotkey=normalized,
                owner_task_id=owner.task_id,
                owner_tab_id=owner.workspace_id,
                owner_tab_title=owner.tab_title(),
            )
            if not silent:
                messagebox.showwarning(
                    "Горячая клавиша уже используется",
                    message,
                )
            return False

        # Nothing to rebuild when the same binding is simply re-applied.
        existing_monitor = self.tab_hotkey_monitors.get(
            task.workspace_id
        )
        if (
            previous == normalized
            and existing_monitor is not None
            and existing_monitor.enabled
        ):
            task.copy_hotkey_var.set(normalized)
            task.copy_hotkey_status_var.set(
                f"{normalized} → только эта вкладка"
            )
            return True

        # Validate first, then replace the old monitor.
        old_monitor = existing_monitor
        self._stop_tab_hotkey_monitor(
            task.workspace_id
        )

        try:
            monitor = GlobalCopyHotkeyMonitor(
                self.root,
                lambda capture_info, workspace_id=task.workspace_id:
                    self._on_tab_copy_hotkey(
                        workspace_id,
                        capture_info,
                    ),
                hotkey=normalized,
            )
            monitor.enable()
            self.tab_hotkey_monitors[
                task.workspace_id
            ] = monitor

        except Exception as exc:
            # Best effort: restore the previous valid assignment.
            if (
                old_monitor is not None
                and previous
                != TAB_HOTKEY_NONE_LABEL
            ):
                try:
                    restored_monitor = (
                        GlobalCopyHotkeyMonitor(
                            self.root,
                            lambda capture_info, workspace_id=task.workspace_id:
                                self._on_tab_copy_hotkey(
                                    workspace_id,
                                    capture_info,
                                ),
                            hotkey=previous,
                        )
                    )
                    restored_monitor.enable()
                    self.tab_hotkey_monitors[
                        task.workspace_id
                    ] = restored_monitor
                except Exception:
                    previous = TAB_HOTKEY_NONE_LABEL

            task.applied_copy_hotkey = previous
            task.copy_hotkey_var.set(previous)
            task.copy_hotkey_status_var.set(
                f"Не удалось включить {normalized}."
            )
            self.logger.event(
                "tab_copy_hotkey_enable_failed",
                task_id=task.task_id,
                tab_id=task.workspace_id,
                requested_hotkey=normalized,
                error=f"{type(exc).__name__}: {exc}",
            )
            if not silent:
                messagebox.showerror(
                    "Горячая клавиша",
                    "Не удалось включить горячую клавишу.\n\n"
                    f"{exc}",
                )
            return False

        task.applied_copy_hotkey = normalized
        task.copy_hotkey_var.set(normalized)
        task.copy_hotkey_status_var.set(
            f"{normalized} → только эта вкладка"
        )

        self.logger.event(
            "tab_copy_hotkey_applied",
            task_id=task.task_id,
            tab_id=task.workspace_id,
            tab_title=task.tab_title(),
            hotkey=normalized,
        )

        if save_workspace:
            self.schedule_workspace_save()
        return True

    def _activate_restored_tab_hotkeys(self) -> None:
        """
        Restore persisted per-tab bindings in visible tab order.

        If an old/corrupt workspace somehow contains duplicate bindings, the
        first tab keeps the key and later conflicting tabs are reset to "Нет".
        """
        for widget_name in self.notebook.tabs():
            task = None
            for candidate in self.tabs.values():
                if str(candidate.frame) == widget_name:
                    task = candidate
                    break
            if task is None:
                continue

            requested = str(
                task.copy_hotkey_var.get()
                or TAB_HOTKEY_NONE_LABEL
            )
            if not self.apply_tab_copy_hotkey(
                task,
                silent=True,
                save_workspace=False,
            ):
                task.applied_copy_hotkey = (
                    TAB_HOTKEY_NONE_LABEL
                )
                task.copy_hotkey_var.set(
                    TAB_HOTKEY_NONE_LABEL
                )
                task.copy_hotkey_status_var.set(
                    "Горячая клавиша не назначена."
                )
                self.logger.event(
                    "restored_tab_hotkey_reset",
                    task_id=task.task_id,
                    tab_id=task.workspace_id,
                    requested_hotkey=requested,
                    reason="invalid_or_duplicate",
                )

        self.schedule_workspace_save()

    def _on_tab_copy_hotkey(
        self,
        workspace_id: str,
        capture_info: dict | None = None,
    ) -> None:
        """
        A hotkey is permanently routed to its owning tab, regardless of which
        tab is currently selected on screen.
        """
        if self.shutdown_in_progress:
            return

        task = self._task_by_workspace_id(
            workspace_id
        )
        monitor = self.tab_hotkey_monitors.get(
            workspace_id
        )
        if (
            task is None
            or monitor is None
            or not monitor.enabled
        ):
            return

        hotkey = monitor.get_hotkey()
        info = dict(capture_info or {})
        source_process = str(
            info.get("source_process") or ""
        )
        source_pid = info.get("source_pid")
        copy_latency_ms = float(
            info.get("copy_latency_ms") or 0
        )

        if info.get("clipboard_changed") is False:
            self.logger.event(
                "global_copy_clipboard_timeout",
                task_id=task.task_id,
                tab_id=task.workspace_id,
                hotkey=hotkey,
                source_process=source_process,
                source_pid=source_pid,
                copy_latency_ms=copy_latency_ms,
                timeout_sec=CLIPBOARD_COPY_TIMEOUT_SEC,
            )
            task.status_var.set(
                "Не удалось дождаться нового текста в буфере обмена."
            )
            return

        try:
            captured = self.root.clipboard_get()
        except tk.TclError as exc:
            self.logger.event(
                "global_copy_clipboard_read_failed",
                task_id=task.task_id,
                tab_id=task.workspace_id,
                hotkey=hotkey,
                source_process=source_process,
                source_pid=source_pid,
                copy_latency_ms=copy_latency_ms,
                error=f"{type(exc).__name__}: {exc}",
            )
            return

        if not isinstance(captured, str):
            return

        raw_captured = captured.strip()
        if not raw_captured:
            return

        captured = sanitize_hotkey_capture_text(
            raw_captured
        )
        if not captured:
            self.logger.event(
                "global_copy_filtered_empty",
                task_id=task.task_id,
                tab_id=task.workspace_id,
                hotkey=hotkey,
                raw_chars=len(raw_captured),
                source_process=source_process,
                source_pid=source_pid,
                copy_latency_ms=copy_latency_ms,
            )
            task.status_var.set(
                "В выделении нет русских букв, цифр, точек или запятых."
            )
            return

        removed_chars = max(
            0,
            len(raw_captured) - len(captured),
        )

        captured_hash = hashlib.sha256(
            captured.encode("utf-8")
        ).hexdigest()[:16]

        if monitor.remember_text_and_check_duplicate(
            captured
        ):
            self.logger.event(
                "global_copy_duplicate_skipped",
                task_id=task.task_id,
                tab_id=task.workspace_id,
                hotkey=hotkey,
                chars=len(captured),
                raw_chars=len(raw_captured),
                filtered_chars=len(captured),
                removed_chars=removed_chars,
                captured_sha256_16=captured_hash,
                source_process=source_process,
                source_pid=source_pid,
                copy_latency_ms=copy_latency_ms,
            )
            return

        self.logger.event(
            "global_copy_text_captured",
            task_id=task.task_id,
            tab_id=task.workspace_id,
            tab_title=task.tab_title(),
            hotkey=hotkey,
            routing_mode="fixed_tab",
            chars=len(captured),
            raw_chars=len(raw_captured),
            filtered_chars=len(captured),
            removed_chars=removed_chars,
            captured_sha256_16=captured_hash,
            source_process=source_process,
            source_pid=source_pid,
            copy_latency_ms=copy_latency_ms,
            clipboard_changed=info.get(
                "clipboard_changed"
            ),
            preview_running=bool(
                task.preview_thread
                and task.preview_thread.is_alive()
            ),
            paused=bool(task.preview_is_paused),
            auto_read_hotkey_text=bool(
                task.auto_read_hotkey_var.get()
            ),
        )

        # Critical routing rule: DO NOT use current_tab() here.
        task.append_external_selected_text(
            captured
        )

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

    def _process_events(self) -> None:
        try:
            while True:
                task_id, kind, payload = self.events.get_nowait()

                if kind == "voices_loaded":
                    self.voices = list(payload)  # type: ignore[arg-type]

                    voices_blob = "\n".join(self.voices)
                    current_tab = self.current_tab()
                    self.logger.event(
                        "voices_loaded",
                        count=len(self.voices),
                        voices_hash=hashlib.sha256(
                            voices_blob.encode("utf-8")
                        ).hexdigest()[:16],
                        selected_voice=(
                            current_tab.voice_var.get().strip()
                            if current_tab
                            else self.settings.get("voice", "")
                        ),
                    )

                    for task in list(self.tabs.values()):
                        task.apply_voice_list(self.voices)

                    continue

                if kind == "voices_error":
                    info = payload  # type: ignore[assignment]
                    exc = info["exc"]

                    self.logger.error(
                        task_id="app",
                        stage="load_sapi_voices",
                        exc=exc,
                        traceback_text=info["traceback"],
                        context={},
                    )

                    messagebox.showerror(
                        APP_TITLE,
                        "Не удалось получить список SAPI-голосов.\n\n"
                        f"{exc}",
                    )
                    continue

                if kind == "audio_outputs_loaded":
                    info = payload  # type: ignore[assignment]
                    outputs = list(info.get("outputs") or [])
                    current = str(info.get("current") or "")

                    self.audio_outputs = outputs

                    selected = (
                        self.global_audio_output_var.get().strip()
                        or DEFAULT_AUDIO_OUTPUT_LABEL
                    )
                    resolved = None
                    match_mode = "default"

                    if selected != DEFAULT_AUDIO_OUTPUT_LABEL:
                        resolved, match_mode = (
                            resolve_audio_output_description(
                                selected,
                                outputs,
                            )
                        )
                        if resolved:
                            # Windows may have changed only the endpoint number.
                            # Rebind the preference to the current real description.
                            if resolved != selected:
                                self.global_audio_output_var.set(
                                    resolved
                                )
                            selected = resolved

                    # IMPORTANT: if a Bluetooth device is temporarily offline,
                    # keep its saved preference instead of silently replacing it
                    # with the Windows default device.
                    choices = [DEFAULT_AUDIO_OUTPUT_LABEL]
                    if (
                        selected != DEFAULT_AUDIO_OUTPUT_LABEL
                        and selected not in outputs
                    ):
                        choices.append(selected)
                    for item in outputs:
                        if item not in choices:
                            choices.append(item)

                    self.audio_output_choices = choices

                    for tab in list(self.tabs.values()):
                        tab.audio_output_combo["values"] = (
                            self.audio_output_choices
                        )

                    outputs_blob = "\n".join(outputs)
                    self.logger.event(
                        "audio_outputs_loaded",
                        count=len(outputs),
                        outputs_hash=hashlib.sha256(
                            outputs_blob.encode("utf-8")
                        ).hexdigest()[:16],
                        sapi_current_output=current,
                        selected_output=selected,
                        preferred_output_available=bool(
                            selected
                            == DEFAULT_AUDIO_OUTPUT_LABEL
                            or resolved is not None
                        ),
                        preferred_output_match_mode=match_mode,
                    )
                    continue

                if kind == "audio_outputs_error":
                    info = payload  # type: ignore[assignment]
                    exc = info["exc"]
                    self.logger.error(
                        task_id="app",
                        stage="load_audio_outputs",
                        exc=exc,
                        traceback_text=info["traceback"],
                        context={},
                    )
                    continue

                task = self.tabs.get(task_id)
                if task is None:
                    continue

                if kind == "progress":
                    info = payload  # type: ignore[assignment]
                    task.progress["value"] = float(info["pct"])
                    task.status_var.set(str(info["text"]))

                elif kind == "disk_space_warning":
                    info = payload  # type: ignore[assignment]
                    forecast = info.get("forecast") or {}
                    decision_event = info.get("decision_event")
                    decision = info.get("decision")
                    try:
                        continue_job = False
                        if not self.shutdown_in_progress:
                            continue_job = messagebox.askyesno(
                                APP_TITLE,
                                "Прогноз свободного места показывает риск "
                                "нехватки диска.\n\n"
                                "Временные WAV: примерно "
                                f"{format_bytes(forecast.get('estimated_temp_wav_bytes'))}\n"
                                "Итоговый MP3: примерно "
                                f"{format_bytes(forecast.get('estimated_mp3_bytes'))}\n"
                                "Свободно во временной папке: "
                                f"{format_bytes(forecast.get('temp_free_bytes'))}\n"
                                "Свободно в папке MP3: "
                                f"{format_bytes(forecast.get('output_free_bytes'))}\n\n"
                                "Продолжить несмотря на риск?",
                            )
                        if isinstance(decision, dict):
                            decision["continue"] = bool(
                                continue_job
                            )
                        self.logger.event(
                            "disk_space_warning_decision",
                            task_id=info.get("run_id"),
                            run_id=info.get("run_id"),
                            tab_id=task.workspace_id,
                            continue_job=bool(continue_job),
                        )
                    finally:
                        if isinstance(
                            decision_event,
                            threading.Event,
                        ):
                            decision_event.set()

                elif kind == "preview_segment":
                    info = payload  # type: ignore[assignment]
                    task.show_preview_segment(
                        base_index=str(info["base_index"]),
                        start=int(info["start"]),
                        end=int(info["end"]),
                        index=int(info["index"]),
                        total=int(info["total"]),
                        text=str(info["text"]),
                    )

                elif kind == "preview_segment_done":
                    info = payload  # type: ignore[assignment]
                    task.handle_preview_segment_done(info)

                elif kind == "preview_state":
                    info = payload  # type: ignore[assignment]
                    task.apply_preview_state(str(info.get("state") or ""))

                elif kind == "preview_checkpoint":
                    info = payload  # type: ignore[assignment]
                    task.apply_preview_checkpoint(info)

                elif kind == "done":
                    info = payload  # type: ignore[assignment]
                    run_id = str(info.get("run_id") or "")
                    backup_path = (
                        task.current_mp3_text_backup_path
                    )
                    removal = task.remove_completed_mp3_source(
                        run_id
                    )
                    task.set_job_idle()

                    remaining_chars = int(
                        removal.get("remaining_chars") or 0
                    )
                    if removal.get("removed"):
                        task.status_var.set(
                            f"Готово: {info['output']} — "
                            "конвертированный текст удалён."
                        )
                        text_note = (
                            "\n\nКонвертированный текст удалён из вкладки.\n"
                            "Добавленный позже текст сохранён: "
                            f"{remaining_chars:,} символов."
                        ).replace(",", " ")
                    else:
                        task.status_var.set(
                            f"Готово: {info['output']} — "
                            "исходный текст сохранён."
                        )
                        text_note = (
                            "\n\nИсходный текст не удалён: его снимок "
                            "изменился или не удалось безопасно подтвердить "
                            "границы. Новый текст также сохранён."
                        )

                    backup_note = (
                        "\n\nБэкап текста (хранится 45 дней):\n"
                        f"{backup_path}"
                        if backup_path
                        else ""
                    )
                    messagebox.showinfo(
                        APP_TITLE,
                        f"MP3 успешно создан:\n{info['output']}"
                        + text_note
                        + backup_note,
                    )

                elif kind == "cancelled":
                    info = payload  # type: ignore[assignment]
                    task.discard_mp3_source_snapshot(
                        str(info.get("run_id") or "") or None,
                        reason="cancelled",
                    )
                    task.set_job_idle()
                    task.status_var.set("Остановлено.")
                    messagebox.showinfo(
                        APP_TITLE,
                        str(info["message"]),
                    )

                elif kind == "job_error":
                    info = payload  # type: ignore[assignment]
                    task.discard_mp3_source_snapshot(
                        str(info.get("run_id") or "") or None,
                        reason="job_error",
                    )
                    task.set_job_idle()
                    task.status_var.set("Ошибка.")

                    diagnostic = ""
                    if info.get("error_log"):
                        diagnostic = (
                            "\n\nДиагностика сохранена:\n"
                            + str(info["error_log"])
                        )

                    recovery_note = ""
                    if info.get("recovery_preserved"):
                        recovery_note = (
                            "\n\nГотовые WAV сохранены для восстановления "
                            "после следующего запуска программы:\n"
                            + str(info.get("recovery_dir") or "")
                        )

                    messagebox.showerror(
                        APP_TITLE,
                        str(info.get("user_message") or info["exc"])
                        + diagnostic
                        + recovery_note,
                    )

                elif kind == "preview_done":
                    info = payload if isinstance(payload, dict) else {}

                    finished_preview_run_id = info.get(
                        "preview_run_id"
                    )
                    stopped = bool(info.get("stopped"))

                    finish_reason = (
                        task.current_preview_finish_reason
                        or (
                            "completed"
                            if not stopped
                            else "other"
                        )
                    )
                    requested_audio_output = str(
                        info.get("requested_audio_output")
                        or ""
                    )
                    actual_audio_output = str(
                        info.get("actual_audio_output")
                        or ""
                    )

                    if (
                        requested_audio_output
                        and requested_audio_output
                        != DEFAULT_AUDIO_OUTPUT_LABEL
                        and actual_audio_output
                        and audio_output_descriptions_equivalent(
                            requested_audio_output,
                            actual_audio_output,
                        )
                        and self.global_audio_output_var.get().strip()
                        != actual_audio_output
                    ):
                        old_output = (
                            self.global_audio_output_var.get().strip()
                        )
                        self.global_audio_output_var.set(
                            actual_audio_output
                        )
                        self.logger.event(
                            "audio_output_preference_rebound",
                            previous_output=old_output,
                            actual_output=actual_audio_output,
                        )

                    self.logger.event(
                        "preview_finished",
                        task_id=task.task_id,
                        tab_id=task.workspace_id,
                        preview_run_id=finished_preview_run_id,
                        finish_reason=finish_reason,
                        requested_audio_output=requested_audio_output,
                        actual_audio_output=actual_audio_output,
                        chunks=info.get("chunks"),
                        completed_chunks=info.get("completed_chunks"),
                        sapi_stream_blocks=info.get(
                            "sapi_stream_blocks"
                        ),
                        stopped=info.get("stopped"),
                        paused_at_finish=info.get("paused_at_finish"),
                        duration_sec=info.get("duration_sec"),
                    )
                    task.flush_read_delete_log(
                        reason=finish_reason,
                        force=True,
                    )
                    task.log_text_state(
                        f"preview_finished_{finish_reason}",
                        force=True,
                    )

                    # preview_sapi_text has already returned and released COM.
                    # Clear the thread reference so another SAPI preview can
                    # safely be created in a fresh COM worker.
                    task.preview_thread = None

                    if task.preview_resume_previous_pending:
                        task.preview_resume_previous_pending = False
                        task.preview_keep_bookmark_after_stop = False
                        previous_run_id = (
                            task.preview_resume_from_run_id
                            or finished_preview_run_id
                        )

                        if task.resume_saved_preview():
                            self.logger.event(
                                "preview_resume_chain",
                                task_id=task.task_id,
                                tab_id=task.workspace_id,
                                previous_preview_run_id=previous_run_id,
                                next_preview_run_id=task.current_preview_run_id,
                                mode="rewind_previous_sentence",
                            )
                            task.preview_resume_from_run_id = None
                            continue

                        # If the bookmark unexpectedly became invalid, return the
                        # UI to a safe stopped state instead of hanging buttons.
                        task.preview_resume_from_run_id = None
                        task.preview_finished_ui(
                            stopped=True,
                        )
                        continue

                    if not stopped:
                        task.finalize_delete_read_text()

                    if (
                        not stopped
                        and task.continue_appended_tail_if_needed()
                    ):
                        continue

                    task.preview_finished_ui(
                        stopped=stopped,
                    )
                    task.current_preview_finish_reason = None

                elif kind == "preview_error":
                    info = payload  # type: ignore[assignment]
                    exc = info["exc"]

                    preview_context = info["context"]
                    self.logger.error(
                        task_id=str(
                            preview_context.get(
                                "preview_run_id"
                            )
                            or task.task_id
                        ),
                        stage="preview",
                        exc=exc,
                        traceback_text=info["traceback"],
                        context=preview_context,
                    )

                    task.current_preview_finish_reason = "error"
                    self.logger.event(
                        "preview_finished",
                        task_id=task.task_id,
                        tab_id=task.workspace_id,
                        preview_run_id=preview_context.get(
                            "preview_run_id"
                        ),
                        finish_reason="error",
                        stopped=True,
                        duration_sec=None,
                    )
                    task.flush_read_delete_log(
                        reason="error",
                        force=True,
                    )
                    task.preview_keep_bookmark_after_stop = bool(
                        task.preview_bookmark
                    )
                    task.preview_finished_ui(stopped=True)
                    if not task.preview_bookmark:
                        task.clear_preview_highlight()
                    task.status_var.set(
                        "Ошибка воспроизведения. "
                        "Сохранённую позицию можно продолжить."
                        if task.preview_bookmark
                        else "Ошибка воспроизведения."
                    )

                    messagebox.showerror(
                        APP_TITLE,
                        "Не удалось воспроизвести текст.\n\n"
                        f"{exc}",
                    )

        except queue.Empty:
            pass

        self.root.after(100, self._process_events)

    def _alive_background_threads(self) -> list[str]:
        alive: list[str] = []
        for task in self.tabs.values():
            if task.worker and task.worker.is_alive():
                alive.append(task.worker.name)
            if (
                task.preview_thread
                and task.preview_thread.is_alive()
            ):
                alive.append(task.preview_thread.name)
        return alive

    def _finalize_close(self, status: str) -> None:
        for workspace_id in list(
            self.tab_hotkey_monitors
        ):
            self._stop_tab_hotkey_monitor(
                workspace_id
            )

        try:
            self.save_workspace()
        except Exception:
            pass
        try:
            self.save_settings()
        except Exception:
            pass
        self.logger.close(status=status)
        self.root.destroy()

    def _poll_shutdown(self) -> None:
        if not self.shutdown_in_progress:
            return

        alive = self._alive_background_threads()
        if not alive:
            self._finalize_close("closed")
            return

        if time.perf_counter() >= self.shutdown_deadline:
            self.logger.event(
                "forced_shutdown",
                alive_threads=alive,
                grace_period_sec=SHUTDOWN_GRACE_SEC,
            )
            self._finalize_close("forced_shutdown")
            return

        self.root.after(100, self._poll_shutdown)

    def on_close(self) -> None:
        if self.shutdown_in_progress:
            return

        running = [
            task
            for task in self.tabs.values()
            if task.conversion_running()
        ]

        if running:
            answer = messagebox.askyesno(
                "Идёт создание MP3",
                f"Сейчас создаётся MP3-задач: {len(running)}.\n\n"
                "Если закрыть программу, текущая конвертация будет "
                "корректно остановлена. Исходный текст и его бэкап "
                "останутся, но текущий MP3 не будет считаться "
                "завершённым.\n\n"
                "Закрыть программу и остановить создание MP3?\n\n"
                "Выберите «Нет», чтобы продолжить конвертацию.",
                icon=messagebox.WARNING,
                default=messagebox.NO,
            )
            if not answer:
                return

        preview_running = [
            task
            for task in self.tabs.values()
            if (
                task.preview_thread
                and task.preview_thread.is_alive()
            )
        ]

        self.shutdown_in_progress = True
        self.shutdown_deadline = (
            time.perf_counter() + SHUTDOWN_GRACE_SEC
        )

        self.logger.event(
            "shutdown_requested",
            conversion_run_ids=[
                task.current_run_id
                for task in running
                if task.current_run_id
            ],
            conversion_tab_ids=[
                task.workspace_id for task in running
            ],
            conversion_ui_task_ids=[
                task.task_id for task in running
            ],
            preview_run_ids=[
                task.current_preview_run_id
                for task in preview_running
                if task.current_preview_run_id
            ],
            preview_tab_ids=[
                task.workspace_id
                for task in preview_running
            ],
            preview_ui_task_ids=[
                task.task_id for task in preview_running
            ],
            grace_period_sec=SHUTDOWN_GRACE_SEC,
        )

        for task in running:
            task.cancel_event.set()
            task.status_var.set(
                "Завершаю задачу перед закрытием программы…"
            )

        for task in self.tabs.values():
            if (
                task.preview_thread
                and task.preview_thread.is_alive()
            ):
                task.current_preview_finish_reason = "app_exit"
                self.logger.event(
                    "preview_interrupted_by_app_exit",
                    task_id=task.task_id,
                    tab_id=task.workspace_id,
                    preview_run_id=task.current_preview_run_id,
                    audio_output=task.audio_output_var.get().strip()
                    or DEFAULT_AUDIO_OUTPUT_LABEL,
                )
            if (
                task.preview_thread
                and task.preview_thread.is_alive()
            ):
                if not (
                    task.preview_pause_event.is_set()
                    and task._bookmark_is_valid()
                ):
                    task.save_current_preview_bookmark(
                        reason="app_exit",
                        absolute_offset=task.current_preview_segment_start,
                        word_length=max(1, task.current_preview_word_length),
                        force_save=False,
                    )
                task.preview_keep_bookmark_after_stop = True
            task.flush_read_delete_log(
                reason="app_exit",
                force=True,
            )
            task.log_text_state(
                "app_exit",
                force=True,
            )
            task.preview_stop_event.set()
            task.preview_pause_event.clear()

        # Save user text immediately. Final task logs are allowed to finish during
        # the grace period before the logger is closed.
        self.save_workspace()
        self.save_settings()

        if not self._alive_background_threads():
            self._finalize_close("closed")
            return

        self.root.after(100, self._poll_shutdown)


def main() -> None:
    root = tk.Tk()
    TTSApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
