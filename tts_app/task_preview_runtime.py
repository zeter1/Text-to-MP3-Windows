from __future__ import annotations

from .task_preview_player import TaskPreviewPlayerMixin
from .task_preview_delete import TaskPreviewDeleteMixin
from .task_preview_controls import TaskPreviewControlsMixin

class TaskPreviewRuntimeMixin(TaskPreviewPlayerMixin, TaskPreviewDeleteMixin, TaskPreviewControlsMixin):
    """Compatibility composition root; implementation lives in focused mixins."""
    pass
