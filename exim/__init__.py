"""EXIM Insight & Q&A Bot -- core package (H.G. Hawa & Co.)."""
from .ingest import ingest_file, IngestResult, IngestError
from .clean import clean_and_derive, quality_report
from .store import build_database, run_select, is_safe_select
from .insights import build_dashboard
from .qa import Engine, ask

__all__ = [
    "ingest_file", "IngestResult", "IngestError",
    "clean_and_derive", "quality_report",
    "build_database", "run_select", "is_safe_select",
    "build_dashboard", "Engine", "ask",
]
__version__ = "1.0"
