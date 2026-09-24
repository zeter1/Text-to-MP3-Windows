from __future__ import annotations

from .task_ui import TaskUiMixin
from .task_sources import TaskSourcesMixin
from .task_preview_state import TaskPreviewStateMixin
from .task_preview_runtime import TaskPreviewRuntimeMixin
from .task_conversion_controls import TaskConversionControlsMixin
from .task_conversion_worker import TaskConversionWorkerMixin


class TaskTab(
    TaskUiMixin,
    TaskSourcesMixin,
    TaskPreviewStateMixin,
    TaskPreviewRuntimeMixin,
    TaskConversionControlsMixin,
    TaskConversionWorkerMixin,
):
    """One UI task tab assembled from concern-specific mixins."""

    pass
