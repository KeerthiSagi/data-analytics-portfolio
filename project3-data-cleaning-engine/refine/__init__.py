"""REFINE: a rule-based data cleaning engine. This package never imports Streamlit."""

from refine.config import ENGINE_NAME, ENGINE_VERSION
from refine.errors import CleaningError, LoadError, RefineError
from refine.loader import PRESERVATION_SUMMARY, detect_format, list_sheets, load_file, verify_raw_bytes
from refine.profile import ColumnProfile, DatasetProfile, profile_column, profile_dataset
from refine.quality import QualityIssue, QualityReport, ValidityRule, assess_quality
from refine.fingerprint import frame_fingerprint, state_fingerprint
from refine.session import AuditEntry, CleaningSession, replay_recipe
from refine.source import TEXT_DTYPE, LoadedDataset, LoadNote, LoadOptions, SheetInfo, SourceInfo

__all__ = [
    "ENGINE_NAME", "ENGINE_VERSION", "LoadError", "RefineError", "PRESERVATION_SUMMARY",
    "detect_format", "list_sheets", "load_file", "verify_raw_bytes", "TEXT_DTYPE",
    "LoadedDataset", "LoadNote", "LoadOptions", "SheetInfo", "SourceInfo",
    "ColumnProfile", "DatasetProfile", "profile_column", "profile_dataset",
    "QualityIssue", "QualityReport", "ValidityRule", "assess_quality",
    "CleaningError", "CleaningSession", "AuditEntry", "replay_recipe", "frame_fingerprint", "state_fingerprint",
]
