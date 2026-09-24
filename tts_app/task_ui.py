from __future__ import annotations

from .task_ui_build import TaskUiBuildMixin
from .task_ui_status import TaskUiStatusMixin
from .task_ui_state import TaskUiStateMixin

class TaskUiMixin(TaskUiBuildMixin, TaskUiStatusMixin, TaskUiStateMixin):
    """Compatibility composition root; implementation lives in focused mixins."""
    pass
