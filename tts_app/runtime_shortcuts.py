from __future__ import annotations

from .runtime_core import *

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
