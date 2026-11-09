"""Role 4 reporting over the shared backend ReportInput contract."""

from .models import ActivitySummary, CounterReading, MetricSummary, Report
from .provider import PlainTextReportProvider, build_provider
from .service import build_report, render_report_text

__all__ = [
    "ActivitySummary", "CounterReading", "MetricSummary", "Report",
    "PlainTextReportProvider", "build_provider", "build_report", "render_report_text",
]
