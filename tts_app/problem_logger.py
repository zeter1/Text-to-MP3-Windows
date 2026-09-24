from __future__ import annotations

from .problem_logger_session import ProblemLoggerSessionMixin
from .problem_logger_insights import ProblemLoggerInsightsMixin
from .problem_logger_summary import ProblemLoggerSummaryMixin
from .problem_logger_events import ProblemLoggerEventsMixin

class ProblemLogger(ProblemLoggerSessionMixin, ProblemLoggerInsightsMixin, ProblemLoggerSummaryMixin, ProblemLoggerEventsMixin):
    """Compatibility composition root; implementation lives in focused mixins."""
    pass
