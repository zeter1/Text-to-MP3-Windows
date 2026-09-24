from __future__ import annotations

from .runtime_core import *

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
