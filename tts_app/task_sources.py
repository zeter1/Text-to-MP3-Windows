from __future__ import annotations

from .task_source_tracking import TaskSourceTrackingMixin
from .task_capture import TaskCaptureMixin
from .task_file_io import TaskFileIoMixin

class TaskSourcesMixin(TaskSourceTrackingMixin, TaskCaptureMixin, TaskFileIoMixin):
    """Compatibility composition root; implementation lives in focused mixins."""
    pass
