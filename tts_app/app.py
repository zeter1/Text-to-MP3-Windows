from __future__ import annotations

from .runtime import tk
from .app_workspace import AppWorkspaceMixin
from .app_recovery_hotkeys import AppRecoveryHotkeysMixin
from .app_audio_settings import AppAudioSettingsMixin
from .app_events import AppEventsMixin


class TTSApp(
    AppWorkspaceMixin,
    AppRecoveryHotkeysMixin,
    AppAudioSettingsMixin,
    AppEventsMixin,
):
    """Main application assembled from feature-focused mixins."""

    pass


def main() -> None:
    root = tk.Tk()
    TTSApp(root)
    root.mainloop()
