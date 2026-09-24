from __future__ import annotations

from .windows_hotkey_config import WindowsHotkeyConfigMixin
from .windows_hotkey_capture import WindowsHotkeyCaptureMixin
from .windows_hotkey_hook import WindowsHotkeyHookMixin

class GlobalCopyHotkeyMonitor(
    WindowsHotkeyConfigMixin,
    WindowsHotkeyCaptureMixin,
    WindowsHotkeyHookMixin,
):
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
