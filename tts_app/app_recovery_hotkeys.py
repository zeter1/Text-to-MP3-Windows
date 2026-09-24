from __future__ import annotations

from .app_recovery import AppRecoveryMixin
from .app_hotkeys import AppHotkeysMixin

class AppRecoveryHotkeysMixin(AppRecoveryMixin, AppHotkeysMixin):
    """Compatibility composition root; implementation lives in focused mixins."""
    pass
