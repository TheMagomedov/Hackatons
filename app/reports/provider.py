"""Synchronous backend ReportProvider adapter and runtime factory."""

from app.shared.models import ReportInput

from .service import build_report, render_report_text


class PlainTextReportProvider:
    """No-I/O implementation of app.shared.ports.ReportProvider."""

    def build(self, data: ReportInput) -> str:
        return render_report_text(build_report(data))


def build_provider(settings: object) -> PlainTextReportProvider:
    """Factory for REPORT_PROVIDER_FACTORY; no settings, secrets or I/O needed."""
    return PlainTextReportProvider()
