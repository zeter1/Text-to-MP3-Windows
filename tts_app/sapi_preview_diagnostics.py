from __future__ import annotations

import threading
import time
import sys
import traceback


class PreviewPlaybackError(RuntimeError):
    """Speech completed without positive evidence from the selected output."""


def release_preview_exception_frames(exc: BaseException, *, detach: bool = False) -> str:
    """Preserve text evidence, release COM held by inactive traceback frames."""
    trace = getattr(exc, "preview_traceback_text", None) or "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__)
    )
    exc.preview_traceback_text = trace
    pending = [exc]
    visited = set()
    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        pending.extend(item for item in (current.__cause__, current.__context__) if item is not None)
        if current.__traceback__ is not None:
            traceback.clear_frames(current.__traceback__)
        if detach:
            current.__traceback__ = None
            current.__cause__ = None
            current.__context__ = None
    return trace


def preview_output_inventory(voice) -> dict:
    try:
        tokens = voice.GetAudioOutputs()
        count = int(tokens.Count)
        return {"audio_outputs_count": count, "available_audio_outputs": [
            str(tokens.Item(index).GetDescription())[:320] for index in range(min(count, 12))
        ]}
    except Exception as exc:
        return {"audio_enumeration_error": type(exc).__name__}


def preview_sapi_snapshot(voice) -> dict:
    """Copy scalar evidence only; no COM object may escape worker teardown."""
    result = {}
    if voice is None:
        return result
    try:
        result["audio_endpoint_id"] = str(voice.AudioOutput.Id)[:320]
        result["audio_output_description"] = str(voice.AudioOutput.GetDescription())[:320]
    except Exception as exc:
        result["audio_token_error"] = type(exc).__name__
    try:
        status = voice.Status
        for field in ("CurrentStreamNumber", "LastStreamNumberQueued", "RunningState",
                      "LastHResult", "InputWordPosition", "InputWordLength"):
            result[field] = int(getattr(status, field))
    except Exception as exc:
        result["voice_status_error"] = type(exc).__name__
    try:
        # Dynamic IDispatch exposes ISpeechAudio.Status even when makepy's
        # declared ISpeechBaseStream wrapper hides that property. No generation.
        from win32com.client import dynamic
        audio = dynamic.Dispatch(voice.AudioOutputStream)
        status = audio.Status
        for field in ("CurrentDevicePosition", "CurrentSeekPosition", "State"):
            result[field] = int(getattr(status, field))
        try:
            format_info = audio.Format.GetWaveFormatEx()
            result["audio_format"] = {field: int(getattr(format_info, field))
                                      for field in ("SamplesPerSec", "BitsPerSample", "Channels")}
        except Exception as exc:
            result["audio_format_error"] = type(exc).__name__
    except Exception as exc:
        result["audio_status_error"] = type(exc).__name__
    return result


def verify_preview_stream(stream_id: int, before: dict, after: dict) -> None:
    hresult = int(after.get("LastHResult", 0)) & 0xFFFFFFFF
    if hresult & 0x80000000:
        raise PreviewPlaybackError(f"SAPI вернул ошибку вывода 0x{hresult:08X}. Текст сохранён.")
    before_position = before.get("CurrentDevicePosition")
    after_position = after.get("CurrentDevicePosition")
    if (int(after.get("CurrentStreamNumber", 0)) != stream_id
            or stream_id <= 0 or "LastHResult" not in after
            or "voice_status_error" in after or "audio_status_error" in after
            or not isinstance(before_position, int)
            or not isinstance(after_position, int)
            or after_position <= before_position):
        raise PreviewPlaybackError(
            "SAPI не подтвердил воспроизведение выбранным устройством. "
            "Текст непрочитанного блока сохранён. Проверьте аудиоустройство."
        )


def preview_diagnostic(callback, stage: str, voice=None, **fields) -> dict:
    snapshot = preview_sapi_snapshot(voice)
    if callback is not None:
        try:
            callback({"stage": stage, "thread_id": threading.get_ident(),
                      "monotonic_sec": round(time.perf_counter(), 3),
                      "sapi_status": snapshot, **fields})
        except Exception as exc:
            # Preserve the product's primary exception even if a caller's
            # diagnostic callback fails. Do not emit text or exception content.
            try:
                if sys.stderr is not None:
                    print(f"preview diagnostic callback failed: {type(exc).__name__}", file=sys.stderr)
            except Exception:
                pass
    return snapshot
