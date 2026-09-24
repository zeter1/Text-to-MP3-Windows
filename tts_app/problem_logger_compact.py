from __future__ import annotations

from .runtime import *


_SECRET_KEY_PARTS = (
    "password",
    "passwd",
    "token",
    "secret",
    "authorization",
    "cookie",
    "api_key",
    "apikey",
    "private_key",
)


def _redact_path_text(value: str) -> str:
    text = str(value)
    replacements: list[tuple[str, str]] = []
    try:
        replacements.append((str(STORAGE_BASE_DIR), "<APP_DIR>"))
    except Exception:
        pass
    try:
        replacements.append((str(Path.home()), "<HOME>"))
    except Exception:
        pass

    for source, marker in replacements:
        if source and len(source) >= 3:
            text = text.replace(source, marker)
            text = text.replace(source.replace("\\", "/"), marker)

    # Common credential-like query/header fragments. This is intentionally
    # conservative: it protects obvious secrets without pretending to be a
    # perfect anonymizer.
    text = re.sub(
        r"(?i)(authorization\s*[:=]\s*)([^\s,;]+)",
        r"\1<redacted>",
        text,
    )
    text = re.sub(
        r"(?i)((?:token|api[_-]?key|secret|password)=)([^&\s]+)",
        r"\1<redacted>",
        text,
    )
    return text


def compact_log_value(value, *, key: str = "", depth: int = 0):
    """Bound arbitrary diagnostic context while preserving decision data."""
    key_lower = str(key or "").lower()
    if any(part in key_lower for part in _SECRET_KEY_PARTS):
        return "<redacted>"

    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return round(value, 3)
    if isinstance(value, Path):
        return _redact_path_text(str(value))
    if isinstance(value, BaseException):
        return f"{type(value).__name__}: {_redact_path_text(str(value))[:500]}"

    if isinstance(value, str):
        text = _redact_path_text(value)
        max_chars = 700 if depth == 0 else 420
        if len(text) <= max_chars:
            return text
        digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:12]
        return f"{text[:max_chars]}… <truncated chars={len(text)} sha256={digest}>"

    if depth >= 3:
        text = _redact_path_text(str(value))
        return text[:300]

    if isinstance(value, dict):
        result = {}
        items = list(value.items())
        for index, (child_key, child_value) in enumerate(items[:24]):
            result[str(child_key)] = compact_log_value(
                child_value,
                key=str(child_key),
                depth=depth + 1,
            )
        if len(items) > 24:
            result["_omitted_keys"] = len(items) - 24
        return result

    if isinstance(value, (list, tuple, set)):
        items = list(value)
        compacted = [
            compact_log_value(item, key=key, depth=depth + 1)
            for item in items[:18]
        ]
        if len(items) > 18:
            compacted.append(f"<omitted {len(items) - 18} items>")
        return compacted

    return _redact_path_text(str(value))[:500]


def compact_event_fields(event_type: str, fields: dict) -> dict:
    """Compact routine events without shortening human-readable field names."""
    compacted = {}
    for key, value in fields.items():
        if value is None or value == "" or value == [] or value == {}:
            continue
        compacted[str(key)] = compact_log_value(value, key=str(key))

    # app_start duplicates full paths in session.json and they are rarely useful
    # in the raw timeline. Keep exact runtime identity but not three path copies.
    if event_type == "app_start":
        for key in ("executable", "storage_base", "settings_dir", "logs_dir"):
            compacted.pop(key, None)

    if event_type == "text_state":
        keep = {
            "task_id", "tab_id", "preview_run_id", "reason",
            "text_chars", "unread_chars", "delete_read_text",
            "preview_running", "paused", "session_captured_chars",
            "session_read_segments", "session_deleted_segments",
        }
        compacted = {key: value for key, value in compacted.items() if key in keep}

    if event_type in {"error", "error_repeat"}:
        compacted.pop("message", None)

    return compacted


def compact_traceback_text(traceback_text: str, *, max_lines: int = 30, max_chars: int = 7000) -> str:
    text = _redact_path_text(str(traceback_text or ""))
    lines = text.splitlines()
    if len(lines) > max_lines:
        head = lines[:3]
        tail_count = max(1, max_lines - len(head) - 1)
        omitted = len(lines) - len(head) - tail_count
        lines = head + [f"... <{omitted} traceback lines omitted> ..."] + lines[-tail_count:]
        text = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    if len(text) > max_chars:
        text = "... <traceback head omitted> ...\n" + text[-max_chars:]
    return text



def extract_traceback_locations(traceback_text: str, *, limit: int = 5) -> list[dict]:
    locations: list[dict] = []
    pattern = re.compile(r'^\s*File "([^"]+)", line (\d+), in (.+?)\s*$')
    for line in str(traceback_text or "").splitlines():
        match = pattern.match(line)
        if not match:
            continue
        path, line_number, function = match.groups()
        locations.append({
            "file": _redact_path_text(path),
            "line": int(line_number),
            "function": function.strip()[:120],
        })
    return locations[-max(1, int(limit)):]

def make_error_fingerprint(stage: str, exc: BaseException, message: str) -> str:
    normalized_message = re.sub(r"\s+", " ", str(message or "")).strip().lower()
    # Numbers, PIDs and timing values often vary across identical failures.
    normalized_message = re.sub(r"\b\d{4,}\b", "#", normalized_message)
    raw = f"{stage}|{type(exc).__name__}|{normalized_message[:800]}"
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]


def classify_error(stage: str, exc: BaseException, message: str) -> dict:
    stage_text = str(stage or "unknown").strip().lower()
    message_text = str(message or "").lower()
    exception_name = type(exc).__name__

    owner = "unknown"
    suggested_checks = ["Открыть traceback и последнее signal-событие перед ошибкой."]
    code = "UNCLASSIFIED_ERROR"
    severity = "error"
    likely_cause = "Ошибка возникла на указанном stage; уточнить по traceback и recent_signals."

    if "не найдено в windows sapi" in message_text or (
        "устройств" in message_text and "sapi" in message_text and "не найден" in message_text
    ):
        code = "AUDIO_OUTPUT_NOT_FOUND"
        owner = "tts_app/sapi_core.py"
        likely_cause = "Выбранное устройство воспроизведения больше не доступно Windows SAPI или изменило системное имя."
        suggested_checks = [
            "Проверить audio_outputs_loaded и выбранное устройство.",
            "Проверить set_audio_output_by_description() в tts_app/sapi_core.py.",
        ]
    elif "ffmpeg" in stage_text or "ffmpeg" in message_text:
        code = "FFMPEG_FAILURE"
        owner = "tts_app/ffmpeg_tools.py"
        likely_cause = "FFmpeg завершил внешний этап кодирования/обработки с ошибкой."
        suggested_checks = [
            "Проверить stage, exit code и bounded stderr FFmpeg.",
            "Проверить входной WAV и финальную output validation.",
        ]
    elif isinstance(exc, FileNotFoundError):
        code = "FILE_NOT_FOUND"
        owner = "filesystem / caller stage"
        likely_cause = "Нужный файл или внешний инструмент отсутствует по ожидаемому пути."
        suggested_checks = ["Проверить sanitized path и существование файла/инструмента на этом stage."]
    elif isinstance(exc, PermissionError):
        code = "FILE_PERMISSION_DENIED"
        owner = "filesystem / caller stage"
        likely_cause = "Операционная система запретила доступ к файлу: права, блокировка или конкурентная запись."
        suggested_checks = ["Проверить блокировку файла, права и atomic replace на этом stage."]
    elif isinstance(exc, TimeoutError) or "timeout" in message_text or "таймаут" in message_text:
        code = "OPERATION_TIMEOUT"
        owner = "caller stage"
        likely_cause = "Операция не завершилась в ожидаемый срок; проверить зависание boundary/subprocess/thread."
        suggested_checks = ["Проверить последнее успешное событие, elapsed и активный subprocess/thread."]
    elif "workspace" in stage_text:
        code = "WORKSPACE_STATE_FAILURE"
        owner = "tts_app/app_workspace_save.py"
    elif "recovery" in stage_text:
        code = "RECOVERY_FAILURE"
        owner = "tts_app/app_recovery.py"
    elif "preview" in stage_text or "sapi" in stage_text:
        code = f"SAPI_{re.sub(r'[^A-Z0-9]+', '_', exception_name.upper()).strip('_')}"
        owner = "tts_app/task_preview_player.py / tts_app/sapi_preview.py"
        suggested_checks = [
            "Проверить preview_started/preview_finished и audio output.",
            "Проверить последнюю SAPI boundary в traceback.",
        ]
    elif "mp3" in stage_text or "convert" in stage_text or "chunk" in stage_text:
        code = f"MP3_{re.sub(r'[^A-Z0-9]+', '_', exception_name.upper()).strip('_')}"
        owner = "tts_app/task_conversion_worker.py"
        suggested_checks = [
            "Проверить task_progress и последний успешно завершённый chunk/stage.",
            "Если есть failed_chunk — проверить только его, не весь исходный текст.",
        ]
    else:
        stage_code = re.sub(r"[^A-Z0-9]+", "_", stage_text.upper()).strip("_") or "UNKNOWN"
        exc_code = re.sub(r"[^A-Z0-9]+", "_", exception_name.upper()).strip("_")
        code = f"{stage_code}_{exc_code}"

    return {
        "error_code": code,
        "severity": severity,
        "likely_cause": likely_cause,
        "owner_module": owner,
        "suggested_checks": suggested_checks[:4],
    }



def write_log_json(path: Path, data: dict) -> int:
    """Atomically write compact UTF-8 JSON for diagnostic artifacts."""
    payload = json.dumps(
        data,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    parsed = json.loads(payload)
    if not isinstance(parsed, dict):
        raise ValueError("Корневое значение JSON лога должно быть объектом")
    return atomic_write_text(path, payload)

def compact_text_state(state: dict) -> dict:
    keys = (
        "reason",
        "text_chars",
        "unread_chars",
        "preview_running",
        "paused",
        "session_read_segments",
        "session_deleted_segments",
    )
    return {key: state.get(key) for key in keys if state.get(key) not in (None, "")}


def compact_signal_record(record: dict) -> dict:
    preferred = (
        "ts",
        "event",
        "task_id",
        "run_id",
        "tab_id",
        "preview_run_id",
        "stage",
        "error_code",
        "error_id",
        "owner_module",
        "exception_type",
        "message",
        "reason",
        "finish_reason",
        "status",
        "chunk",
        "chunks",
        "attempt",
        "duration_sec",
        "free_bytes",
    )
    return {key: record.get(key) for key in preferred if record.get(key) not in (None, "")}
