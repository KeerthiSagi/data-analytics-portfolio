"""Data-quality and comparison reports: the same numbers as the engines, valid JSON, honest limits."""

import json

import pandas as pd
import pytest

from refine.compare import CHANGE_EXAMPLES, compare_datasets, history_summary
from refine.export import json_bytes, state_summary
from refine.loader import load_file
from refine.profile import profile_dataset
from refine.quality import CAVEAT, ValidityRule, assess_quality
from refine.reports import (
    COMPARISON_REPORT_FORMAT,
    QUALITY_REPORT_FORMAT,
    REPORT_VERSION,
    comparison_report_document,
    comparison_report_export,
    quality_report_document,
    quality_report_export,
)
from refine.session import CleaningSession
from refine.source import TEXT_DTYPE
from tests.conftest import PROJECT_DIR


def text(*values):
    return pd.array(list(values), dtype=TEXT_DTYPE)


def apply(session, op, params):
    return session.apply(session.preview(op, params), confirmed=True)


def frame() -> pd.DataFrame:
    return pd.DataFrame({
        "id": text("1", "2", "2", "3", "4", "5"),
        "amount": text("10", "20", "20", "NA", "", "40"),
        "city": text("Paris", "Rome", "Rome", " Oslo", "paris", None),
    })


def roundtrip(doc):
    return json.loads(json_bytes(doc), parse_constant=lambda c: pytest.fail(f"non-standard JSON {c}"))


@pytest.fixture
def cleaned():
    s = CleaningSession(frame())
    apply(s, "remove_duplicates", {})
    apply(s, "standardize_missing", {"columns": ["amount"], "tokens": ["NA"]})
    apply(s, "convert_type", {"columns": ["amount"], "target": "integer"})
    return s


@pytest.fixture
def sample():
    data = (PROJECT_DIR / "sample_data" / "messy_customers.csv").read_bytes()
    ds = load_file("messy_customers.csv", data)
    return ds, CleaningSession(ds.data, source_rows=ds.source_rows)


# ---- data-quality report --------------------------------------------------

def test_quality_report_header_and_state(cleaned):
    doc = roundtrip(quality_report_document(cleaned.compare(), state=state_summary(cleaned)))
    assert doc["format"] == QUALITY_REPORT_FORMAT and doc["report_version"] == REPORT_VERSION
    assert doc["state"]["steps_applied"] == 3 and doc["state"]["current"]["fingerprint"] == cleaned.fingerprint
    assert doc["caveat"] == CAVEAT

    def keys(value):
        if isinstance(value, dict):
            for k, v in value.items():
                yield k
                yield from keys(v)
        elif isinstance(value, list):
            for v in value:
                yield from keys(v)

    assert not [k for k in keys(doc) if "score" in k.lower() or "grade" in k.lower()]   # No overall score


def test_quality_metrics_match_profile_and_quality(cleaned):
    c = cleaned.compare()
    doc = roundtrip(quality_report_document(c, state=state_summary(cleaned)))
    for side, frame_ in (("original", cleaned.original_data), ("current", cleaned.data)):
        p = profile_dataset(frame_)
        q = assess_quality(frame_, p)
        d = doc[side]
        assert (d["rows"], d["columns"], d["cells"]) == (p.n_rows, p.n_columns, p.n_cells)
        assert d["missing_cells"] == p.missing_cells and d["missing_pct"] == pytest.approx(p.missing_pct)
        assert d["duplicate_rows"] == p.duplicate_rows and d["duplicate_groups"] == p.duplicate_groups
        assert d["findings_by_severity"] == q.severity_counts()
        assert [(f["check"], f["column"], f["affected_count"], f["denominator"]) for f in d["findings"]] == \
               [(i.check, i.column, i.affected_count, i.denominator) for i in q.issues]
        for col, cp in zip(d["column_profiles"], p.columns):
            assert col["name"] == cp.name and col["missing"] == cp.missing_count
            assert col["distinct"] == cp.distinct_count and col["null_like_text"] == cp.null_token_count


def test_quality_report_missing_duplicates_and_dtype_changes(cleaned):
    doc = roundtrip(quality_report_document(cleaned.compare(), state=state_summary(cleaned)))
    assert doc["original"]["duplicate_rows"] == 1 and doc["current"]["duplicate_rows"] == 0
    original_amount = next(c for c in doc["original"]["column_profiles"] if c["name"] == "amount")
    current_amount = next(c for c in doc["current"]["column_profiles"] if c["name"] == "amount")
    assert original_amount["missing"] == 1 and current_amount["missing"] == 2        # "" was already missing
    assert original_amount["dtype"] == "string[pyarrow]/NA" and original_amount["kind"] == "text"
    assert current_amount["dtype"] == "Int64" and current_amount["kind"] == "integer"
    assert current_amount["numeric"]["count"] == 3 and current_amount["numeric"]["maximum"] == 40
    changes = {m["key"]: m for m in doc["changes"]["overview"]}
    assert changes["duplicate_rows"]["direction"] == "decrease" and changes["rows"]["difference"] == -1


def test_quality_report_findings_by_status_match_comparison(cleaned):
    c = cleaned.compare()
    doc = roundtrip(quality_report_document(c, state=state_summary(cleaned)))
    expected = {"new": 0, "no longer detected": 0, "changed": 0, "unchanged": 0}
    for i in c.quality.issues:
        expected[i.status] += 1
    assert doc["changes"]["findings_by_status"] == expected


def test_quality_report_records_validity_rules():
    s = CleaningSession(frame())
    rule = ValidityRule("city", "allowed_values", allowed=("Paris", "Rome"))
    doc = roundtrip(quality_report_document(s.compare(rules=(rule,)), state=state_summary(s)))
    assert doc["validity_rules"][0]["column"] == "city" and doc["validity_rules"][0]["allowed"] == ["Paris", "Rome"]
    assert any(f["check"].startswith("rule") or f["category"] == "Validity rule" for f in doc["original"]["findings"])


def test_quality_report_on_empty_tables():
    empty = pd.DataFrame({"a": text(), "b": text()})
    c = compare_datasets(empty, empty, row_ids=[])
    doc = roundtrip(quality_report_document(c, state={}))
    assert doc["original"]["rows"] == 0 and doc["current"]["cells"] == 0
    no_columns = pd.DataFrame(index=range(3))
    doc = roundtrip(quality_report_document(compare_datasets(no_columns, no_columns), state={}))
    assert doc["current"]["columns"] == 0


def test_quality_report_does_not_contain_the_table(sample):
    ds, s = sample
    raw = quality_report_export(s.compare(), state=state_summary(s), source_name=ds.source.file_name).data
    assert len(raw) < 200_000
    text_ = raw.decode("utf-8")
    emails = [e for e in ds.data["email"].dropna().unique() if "@" in e]
    included = [e for e in emails if f'"{e}"' in text_]
    assert len(included) <= 10                            # Only top values and check examples, never the column
    assert len(emails) > 400


# ---- comparison report ----------------------------------------------------

def test_comparison_report_matches_the_comparison(cleaned):
    c = cleaned.compare()
    doc = roundtrip(comparison_report_document(c, state=state_summary(cleaned),
                                               history=history_summary(cleaned.steps, cleaned.audit_log)))
    assert doc["format"] == COMPARISON_REPORT_FORMAT and doc["report_version"] == REPORT_VERSION
    assert [(m["key"], m["original"], m["current"]) for m in doc["overview"]] == \
           [(m.key, m.original, m.current) for m in c.overview]
    assert [(m["column"], m["original_missing"], m["current_missing"]) for m in doc["missing_values"]] == \
           [(m.name, m.original_missing, m.current_missing) for m in c.missing]
    assert doc["duplicates"]["original_duplicates"] == 1 and doc["duplicates"]["current_duplicates"] == 0
    assert doc["columns"]["type_changed"] == ["amount"]
    retained = {r["name"]: r for r in doc["columns"]["retained"]}
    assert retained["amount"]["original_dtype"] == "string[pyarrow]/NA" and retained["amount"]["current_dtype"] == "Int64"
    assert [(f["check"], f["column"], f["status"]) for f in doc["quality"]["findings"]] == \
           [(i.check, i.column, i.status) for i in c.quality.issues]
    assert [h["operation"] for h in doc["history"]] == ["remove_duplicates", "standardize_missing", "convert_type"]
    assert doc["identical"] is False


def test_cell_change_summary(cleaned):
    c = cleaned.compare()
    cells = roundtrip(comparison_report_document(c, state={}))["cells"]
    assert cells["available"] and cells["matched_by"].startswith("row lineage")
    assert cells["rows_removed"] == 1 and cells["rows_kept"] == 5 and cells["rows_added"] == 0
    assert cells["cells_changed"] == c.cells.cells_changed and cells["cells_compared"] == c.cells.cells_compared
    amount = next(x for x in cells["columns"] if x["name"] == "amount")
    assert amount["compared_as"] == "text"
    assert (amount["modified"], amount["filled"], amount["made_missing"]) == \
           (c.cells.column("amount").modified, c.cells.column("amount").filled, c.cells.column("amount").made_missing)
    assert amount["changed"] == len(amount["examples"]) and amount["examples_complete"] is True
    assert cells["examples_per_column"] == CHANGE_EXAMPLES


def test_examples_are_marked_incomplete_when_bounded(sample):
    ds, s = sample
    apply(s, "trim_whitespace", {"columns": ["full_name", "country"]})
    cells = roundtrip(comparison_report_document(s.compare(), state={}))["cells"]
    name = next(x for x in cells["columns"] if x["name"] == "full_name")
    assert name["changed"] > CHANGE_EXAMPLES and len(name["examples"]) == CHANGE_EXAMPLES
    assert name["examples_complete"] is False
    assert all(isinstance(e["original_row"], int) for e in name["examples"])


def test_missing_lineage_is_reported_not_invented():
    a, b = frame(), frame().iloc[[1, 0, 2, 3, 4, 5]].reset_index(drop=True)
    doc = roundtrip(comparison_report_document(compare_datasets(a, b), state={}))
    assert doc["cells"]["available"] is False and "lineage" in doc["cells"]["unavailable_reason"].lower()
    assert "columns" not in doc["cells"] and "cells_changed" not in doc["cells"]
    assert doc["limitations"][0].startswith("No cell-level comparison")
    assert doc["overview"]                               # Aggregate comparisons are still there


def test_invalid_lineage_is_reported():
    a = frame()
    doc = roundtrip(comparison_report_document(compare_datasets(a, a, row_ids=[0, 0, 1, 2, 3, 4]), state={}))
    assert doc["cells"]["available"] is False and "more than once" in doc["cells"]["unavailable_reason"]


def test_column_statistics_and_removed_columns(cleaned):
    apply(cleaned, "drop_columns", {"columns": ["city"]})
    c = cleaned.compare()
    doc = roundtrip(comparison_report_document(c, state={}))
    assert [x["name"] for x in doc["columns"]["removed"]] == ["city"]
    stats = {x["name"]: x for x in doc["column_statistics"]}
    assert stats["city"]["status"] == "removed"
    assert stats["amount"]["numeric"] is not None
    values = {m["key"]: m for m in stats["amount"]["values"]}
    assert values["missing"]["original"] == 1 and values["missing"]["current"] == 2
    assert len(stats["id"]["top_values"]["original"]) <= 5


def test_no_change_report():
    s = CleaningSession(frame())
    doc = roundtrip(comparison_report_document(s.compare(), state=state_summary(s)))
    assert doc["identical"] is True and doc["history"] == [] and doc["cells"]["cells_changed"] == 0
    assert {m["direction"] for m in doc["overview"]} == {"no change"}


def test_directions_are_never_judgements(cleaned):
    raw = json_bytes(comparison_report_document(cleaned.compare(), state={})).decode("utf-8").lower()
    for word in ("better", "worse", "improve", "quality score"):
        assert word not in raw


def test_report_exports_have_names_and_types(sample):
    ds, s = sample
    c = s.compare()
    q = quality_report_export(c, state=state_summary(s), source_name=ds.source.file_name)
    k = comparison_report_export(c, state=state_summary(s), source_name=ds.source.file_name)
    assert q.file_name == "messy_customers_quality_report.json" and q.mime == "application/json"
    assert k.file_name == "messy_customers_comparison_report.json"
    json.loads(q.data), json.loads(k.data)


def test_reports_match_the_compare_page_numbers(sample):
    """The page and the report read the same DatasetComparison."""
    ds, s = sample
    apply(s, "remove_duplicates", {})
    apply(s, "standardize_missing", {"columns": ["age"], "tokens": ["NA", "N/A", "null", "unknown"]})
    c = s.compare()
    doc = roundtrip(comparison_report_document(c, state={}))
    overview = {m["key"]: m for m in doc["overview"]}
    assert overview["rows"]["original"] == 500 and overview["rows"]["current"] == 488
    assert overview["duplicate_rows"]["original"] == 12 and overview["duplicate_rows"]["current"] == 0
