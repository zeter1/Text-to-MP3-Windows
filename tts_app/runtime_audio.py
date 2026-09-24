from __future__ import annotations

from .runtime_core import *

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
