from __future__ import annotations

from .runtime import *
from .text_processing import *

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
