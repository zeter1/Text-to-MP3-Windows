from __future__ import annotations

from .runtime import tk
from .app_workspace import AppWorkspaceMixin
from .app_recovery_hotkeys import AppRecoveryHotkeysMixin
from .app_audio_settings import AppAudioSettingsMixin
from .app_events import AppEventsMixin
from .ui_stall_diagnostics import acquire_single_instance, release_single_instance


class TTSApp(
    AppWorkspaceMixin,
    AppRecoveryHotkeysMixin,
    AppAudioSettingsMixin,
    AppEventsMixin,
):
    """Main application assembled from feature-focused mixins."""

    pass


def main() -> None:
    instance_lock, already_running = acquire_single_instance()
    if already_running:
        # No second Tk instance, hotkey monitors or competing workspace writes.
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None, "Программа уже запущена. Переключитесь на открытое окно.",
            "Текст → MP3", 0x40,
        )
        return
    try:
        root = tk.Tk()
        TTSApp(root)
        root.mainloop()
    finally:
        release_single_instance(instance_lock)
