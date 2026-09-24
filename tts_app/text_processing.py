from __future__ import annotations

from .runtime import *

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
