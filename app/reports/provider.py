from app.config import Settings
from app.reports.summary import Reporter


def build_provider(settings: Settings) -> Reporter:
    return Reporter()
