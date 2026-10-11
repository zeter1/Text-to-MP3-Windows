from __future__ import annotations

import tempfile
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tts_app.runtime import clamp_int, format_bytes, bitrate_to_bps, atomic_write_text
from tts_app.text_processing import normalize_text, split_text, build_sapi_text
from tts_app.windows_hotkeys import GlobalCopyHotkeyMonitor


def main() -> None:
    assert clamp_int("12", 0, 10, 3) == 10
    assert bitrate_to_bps("96k") == 96_000
    assert format_bytes(1024).startswith("1.00")
    assert normalize_text("a\r\nb") == "a\nb"
    parts = split_text("Первое предложение. Второе предложение.", 20)
    assert parts and "".join(parts).replace(" ", "")
    assert build_sapi_text("тест", 0, 0)

    # Hotkeys are physical-key based: Russian Й+Ц and English Q+W must
    # resolve to the same VK pair, and modifier matching must use the hook's
    # own event state rather than GetAsyncKeyState inside LowLevelKeyboardProc.
    latin = GlobalCopyHotkeyMonitor.parse_hotkey("Q+W")
    cyrillic = GlobalCopyHotkeyMonitor.parse_hotkey("Й+Ц")
    assert latin["vks"] == cyrillic["vks"]

    monitor = object.__new__(GlobalCopyHotkeyMonitor)
    monitor.hotkey_modifiers = {"CTRL"}
    monitor._pressed_vks = {0x11, ord("C")}
    assert monitor._hook_modifiers_match()
    monitor._pressed_vks = {ord("C")}
    assert not monitor._hook_modifiers_match()
    assert monitor._hook_event_is_injected(0x10)
    assert not monitor._hook_event_is_injected(0x00)
    with tempfile.TemporaryDirectory(prefix="tts smoke ") as tmp:
        target = Path(tmp) / "данные с пробелом.txt"
        atomic_write_text(target, "ok")
        assert target.read_text(encoding="utf-8") == "ok"
    print("smoke_refactor: OK")


if __name__ == "__main__":
    main()
