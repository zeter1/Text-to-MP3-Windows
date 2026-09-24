from __future__ import annotations

from .app_workspace_init import AppWorkspaceInitMixin
from .app_tabs import AppTabsMixin
from .app_workspace_save import AppWorkspaceSaveMixin

class AppWorkspaceMixin(AppWorkspaceInitMixin, AppTabsMixin, AppWorkspaceSaveMixin):
    """Compatibility composition root; implementation lives in focused mixins."""
    pass
