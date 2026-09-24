from __future__ import annotations

from .app_event_dispatch import AppEventDispatchMixin
from .app_shutdown import AppShutdownMixin

class AppEventsMixin(AppEventDispatchMixin, AppShutdownMixin):
    """Compatibility composition root; implementation lives in focused mixins."""
    pass
