"""Session-state access for the loaded dataset.

The dataset lives in st.session_state for this browser session only.
Nothing is cached globally (st.cache_data would share uploaded data
across sessions' memory), and nothing is written to disk.

Cleaning happens in a CleaningSession (refine/session.py) tied to the
loaded dataset. Pages never edit a table: they ask for a preview and the
session applies it only from the confirmation button's callback, so a
rerun, a refresh or a second click can never apply a step by itself.
Loading or re-reading a file starts a new cleaning session.

The profile and quality report are cached in the same browser session,
keyed by the current state's fingerprint (table content + row lineage)
and the profiler version; the quality report also by the validity
rules. Applying, undoing or resetting a step, or loading another file,
changes the fingerprint and so refreshes them; reruns with unchanged
data reuse them. Only the latest result is kept.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import streamlit as st

from refine.duplicates import DuplicateReport, find_duplicates
from refine.errors import CleaningError, LoadError
from refine.loader import load_file
from refine.missing import ColumnMissing, missing_summary
from refine.operations import Preview
from refine.profile import PROFILE_VERSION, DatasetProfile, profile_dataset
from refine.quality import QualityReport, ValidityRule, assess_quality
from refine.session import CleaningSession
from refine.source import LoadedDataset, LoadOptions

DATASET_KEY = "refine_dataset"
ERROR_KEY = "refine_load_error"
UPLOAD_ID_KEY = "refine_upload_id"
UPLOADER_NONCE_KEY = "refine_uploader_nonce"
PROFILE_KEY = "refine_profile_cache"
QUALITY_KEY = "refine_quality_cache"
RULES_KEY = "refine_validity_rules"
CLEANING_KEY = "refine_cleaning_session"
PREVIEW_KEY = "refine_clean_preview"
PREVIEW_REQUEST_KEY = "refine_clean_preview_request"
APPLIED_TOKENS_KEY = "refine_applied_tokens"
FLASH_KEY = "refine_flash"
DUPLICATES_KEY = "refine_duplicate_report"
MISSING_SUMMARY_KEY = "refine_missing_summary"
CONVERSION_ANALYSIS_KEY = "refine_conversion_analysis"      # ui/clean_types.py
CLEANING_KEYS = (CLEANING_KEY, PREVIEW_KEY, PREVIEW_REQUEST_KEY, APPLIED_TOKENS_KEY, FLASH_KEY, DUPLICATES_KEY,
                 MISSING_SUMMARY_KEY, CONVERSION_ANALYSIS_KEY)

SAMPLE_PATH = Path(__file__).resolve().parent.parent / "sample_data" / "messy_customers.csv"
SAMPLE_NAME = SAMPLE_PATH.name

# Validity rules stated for the synthetic sample (see sample_data/README.md).
# They are examples: the user can remove them or add others on the page.
SAMPLE_RULES: tuple[ValidityRule, ...] = (
    ValidityRule("age", "range", minimum=0, maximum=120, description="age between 0 and 120"),
    ValidityRule("annual_spend", "range", minimum=0, description="annual_spend is not negative"),
    ValidityRule("email", "pattern", pattern=r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}",
                 description="email looks like name@domain.tld"),
    ValidityRule("segment", "allowed_values", allowed=("Consumer", "Corporate", "Small Business"),
                 description="segment is Consumer, Corporate or Small Business (exact spelling)"),
)


@dataclass(frozen=True)
class LoadFailure:
    file_name: str
    code: str
    message: str


def get_dataset() -> LoadedDataset | None:
    return st.session_state.get(DATASET_KEY)


def get_error() -> LoadFailure | None:
    return st.session_state.get(ERROR_KEY)


def load_bytes(file_name: str, data: bytes, options: LoadOptions | None = None) -> bool:
    """Parse and store a dataset. On failure the previous dataset is kept."""
    try:
        dataset = load_file(file_name, data, options)
    except LoadError as exc:
        st.session_state[ERROR_KEY] = LoadFailure(file_name, exc.code, exc.message)
        return False
    st.session_state[DATASET_KEY] = dataset
    st.session_state.pop(ERROR_KEY, None)
    for key in CLEANING_KEYS:            # A new dataset starts a new cleaning history
        st.session_state.pop(key, None)
    return True


def load_sample() -> bool:
    return load_bytes(SAMPLE_NAME, SAMPLE_PATH.read_bytes())


def reparse(options: LoadOptions) -> bool:
    """Re-read the retained original bytes with different parsing options."""
    dataset = get_dataset()
    if dataset is None or dataset.raw_bytes is None:
        return False
    return load_bytes(dataset.source.file_name, dataset.raw_bytes, options)


def uploader_key() -> str:
    """Widget key for the file uploader. Changing it gives a fresh, empty uploader."""
    return f"refine_uploader_{st.session_state.get(UPLOADER_NONCE_KEY, 0)}"


def clear_dataset() -> None:
    """Close the dataset and empty the uploader, so the same file can be loaded again."""
    for key in (DATASET_KEY, ERROR_KEY, UPLOAD_ID_KEY, PROFILE_KEY, QUALITY_KEY, *CLEANING_KEYS):
        st.session_state.pop(key, None)
    st.session_state[UPLOADER_NONCE_KEY] = st.session_state.get(UPLOADER_NONCE_KEY, 0) + 1


def on_upload_change() -> None:
    """Callback for the file uploader: load a newly chosen file once.

    Runs before the page is drawn, so the sidebar and page agree on the
    current dataset. Removing the file from the uploader keeps the loaded
    dataset open (it can be closed explicitly).
    """
    uploaded = st.session_state.get(uploader_key())
    if uploaded is None or st.session_state.get(UPLOAD_ID_KEY) == uploaded.file_id:
        return
    st.session_state[UPLOAD_ID_KEY] = uploaded.file_id
    load_bytes(uploaded.name, uploaded.getvalue())


# ------------------------------------------------------------------
# Profile, quality report and validity rules (cached per session)
# ------------------------------------------------------------------

def fingerprint(ds: LoadedDataset) -> tuple:
    """Cache key for the profile: the current cleaning state, not just the file."""
    return (get_session(ds).fingerprint, PROFILE_VERSION)


def get_profile(ds: LoadedDataset) -> DatasetProfile:
    key = fingerprint(ds)
    cached = st.session_state.get(PROFILE_KEY)
    if cached is not None and cached[0] == key:
        return cached[1]
    profile = profile_dataset(get_session(ds).data)
    st.session_state[PROFILE_KEY] = (key, profile)
    return profile


def cached_profile(ds: LoadedDataset) -> DatasetProfile | None:
    """The profile of the current data if it has already been computed; never computes one."""
    cached = st.session_state.get(PROFILE_KEY)
    return cached[1] if cached is not None and cached[0] == fingerprint(ds) else None


@lru_cache(maxsize=1)
def _sample_sha256() -> str:
    return hashlib.sha256(SAMPLE_PATH.read_bytes()).hexdigest()


def is_sample(ds: LoadedDataset) -> bool:
    return ds.source.sha256 == _sample_sha256()


def get_rules(ds: LoadedDataset) -> tuple[ValidityRule, ...]:
    """Rules for this file (kept per checksum, so re-reading keeps them)."""
    rules = st.session_state.setdefault(RULES_KEY, {})
    if ds.source.sha256 not in rules:
        rules[ds.source.sha256] = SAMPLE_RULES if is_sample(ds) else ()
    return rules[ds.source.sha256]


def set_rules(ds: LoadedDataset, new_rules: tuple[ValidityRule, ...]) -> None:
    st.session_state.setdefault(RULES_KEY, {})[ds.source.sha256] = tuple(new_rules)


def get_quality(ds: LoadedDataset) -> QualityReport:
    rules = get_rules(ds)
    key = (fingerprint(ds), rules)
    cached = st.session_state.get(QUALITY_KEY)
    if cached is not None and cached[0] == key:
        return cached[1]
    report = assess_quality(get_session(ds).data, get_profile(ds), rules)
    st.session_state[QUALITY_KEY] = (key, report)
    return report


# ------------------------------------------------------------------
# Cleaning session, previews and confirmed actions
# ------------------------------------------------------------------

def get_session(ds: LoadedDataset) -> CleaningSession:
    """The cleaning session for this dataset, created on first use."""
    stored = st.session_state.get(CLEANING_KEY)
    if stored is not None and stored[0] is ds:
        return stored[1]
    session = CleaningSession(ds.data, source_rows=ds.source_rows)
    st.session_state[CLEANING_KEY] = (ds, session)
    for key in (PREVIEW_KEY, PREVIEW_REQUEST_KEY, APPLIED_TOKENS_KEY, DUPLICATES_KEY, MISSING_SUMMARY_KEY):
        st.session_state.pop(key, None)
    return session


def current_session() -> CleaningSession | None:
    ds = get_dataset()
    return None if ds is None else get_session(ds)


def flash(kind: str, message: str) -> None:
    """A message shown once on the next page draw ("success", "error", "warning", "info")."""
    st.session_state[FLASH_KEY] = (kind, message)


def pop_flash() -> tuple[str, str] | None:
    return st.session_state.pop(FLASH_KEY, None)


def get_preview() -> Preview | None:
    """The pending preview, if it still matches the current data. A stale one is discarded."""
    preview = st.session_state.get(PREVIEW_KEY)
    session = current_session()
    if preview is None or session is None:
        return None
    if not session.is_current(preview):
        st.session_state.pop(PREVIEW_KEY, None)
        st.session_state.pop(PREVIEW_REQUEST_KEY, None)
        flash("warning", "The data changed after the preview was made, so that preview was discarded. "
                         "Nothing was applied. Preview the step again.")
        return None
    return preview


def discard_preview() -> None:
    st.session_state.pop(PREVIEW_KEY, None)
    st.session_state.pop(PREVIEW_REQUEST_KEY, None)


def preview_request() -> tuple[str, dict] | None:
    """(operation, settings as the page sent them) for the pending preview."""
    return st.session_state.get(PREVIEW_REQUEST_KEY)


def request_preview(operation: str, params: dict | Callable[[], dict]) -> None:
    """Button callback: plan a step and keep its preview. Changes no data.

    `params` may be a function, called here, so the settings are read from
    the widgets at the moment of the click rather than when the button was
    drawn.
    """
    session = current_session()
    discard_preview()
    if session is None:
        return
    settings = params() if callable(params) else params
    try:
        st.session_state[PREVIEW_KEY] = session.preview(operation, settings)
    except CleaningError as exc:
        flash("info" if exc.code == "no_change" else "error", exc.message)
        return
    st.session_state[PREVIEW_REQUEST_KEY] = (operation, settings)


def confirm_apply(token: str) -> None:
    """Confirmation-button callback: apply the pending preview with this token, once.

    The token is bound when the button is drawn, so a second click, or a
    click on a button drawn for an older preview, can never apply a step
    again or apply it to different data.
    """
    session = current_session()
    applied = st.session_state.setdefault(APPLIED_TOKENS_KEY, set())
    if token in applied:
        flash("warning", "This step was already applied. It was not applied a second time.")
        return
    preview = st.session_state.get(PREVIEW_KEY)
    if session is None or preview is None or preview.token != token:
        flash("warning", "That preview is no longer current, so nothing was applied. Preview the step again.")
        return
    try:
        entry = session.apply(preview, confirmed=True)
    except CleaningError as exc:
        discard_preview()
        flash("error", f"Nothing was applied. {exc.message}")
        return
    applied.add(token)
    discard_preview()
    flash("success", f"Applied step {entry.step_number}: {entry.summary} "
                     f"The table now has {entry.rows_after:,} rows and {entry.columns_after:,} columns.")


# ------------------------------------------------------------------
# Read-only analyses for the Clean page (cached by data state)
# ------------------------------------------------------------------

def get_missing_summary(ds: LoadedDataset) -> tuple[ColumnMissing, ...]:
    """Missing-cell counts per column of the current data; recomputed only when the data changes."""
    session = get_session(ds)
    cached = st.session_state.get(MISSING_SUMMARY_KEY)
    if cached is not None and cached[0] == session.fingerprint:
        return cached[1]
    summary = missing_summary(session.data)
    st.session_state[MISSING_SUMMARY_KEY] = (session.fingerprint, summary)
    return summary


def find_duplicates_now(settings: Callable[[], dict]) -> None:
    """Button callback: count duplicates for the current settings. Changes no data."""
    session = current_session()
    if session is None:
        return
    s = settings()
    try:
        report = find_duplicates(session.data, s["columns"], ignore_case=s["ignore_case"],
                                 normalize_whitespace=s["normalize_whitespace"])
    except (KeyError, ValueError, TypeError) as exc:
        flash("error", f"Duplicates could not be counted: {exc}")
        return
    st.session_state[DUPLICATES_KEY] = (session.fingerprint, s, report)


def get_duplicate_report(settings: dict) -> DuplicateReport | None:
    """The last duplicate count, if it was made for the current data and these settings."""
    session = current_session()
    cached = st.session_state.get(DUPLICATES_KEY)
    if session is None or cached is None:
        return None
    fp, s, report = cached
    return report if fp == session.fingerprint and s == settings else None


def undo_last() -> None:
    session = current_session()
    if session is None:
        return
    try:
        entry = session.undo()
    except CleaningError as exc:
        flash("error", exc.message)
        return
    # A pending preview is left in place: the Clean page finds it stale and
    # tells the user it was discarded.
    flash("success", f"{entry.summary} The table is back to {entry.rows_after:,} rows and "
                     f"{entry.columns_after:,} columns.")


def reset_to_original() -> None:
    session = current_session()
    if session is None:
        return
    try:
        entry = session.reset()
    except CleaningError as exc:
        flash("error", exc.message)
        return
    flash("success", f"Reset to the original data. {entry.summary}")


def verify_replay() -> None:
    session = current_session()
    if session is None:
        return
    check = session.verify_replay()
    flash("success" if check.ok else "error", check.message)
