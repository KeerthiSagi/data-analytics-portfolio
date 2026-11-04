"""Phase 5 steps through CleaningSession, and their interaction with earlier phases:
preview, confirmation, audit, undo, reset, replay, stale previews, tampered recipes,
lineage, profiling and quality checks on typed columns."""

import copy
import datetime as dt
import json

import numpy as np
import pandas as pd
import pytest

from refine.conversion import DATE_DTYPE
from refine.errors import CleaningError
from refine.fingerprint import frame_fingerprint
from refine.profile import profile_dataset
from refine.quality import ValidityRule, assess_quality
from refine.session import CleaningSession, replay_recipe
from refine.source import TEXT_DTYPE
from tests.conftest import PROJECT_DIR, load_csv


def text(*values):
    return pd.array(list(values), dtype=TEXT_DTYPE)


def frame() -> pd.DataFrame:
    return pd.DataFrame({
        "id": text("1", "2", "2", "3", "4", "5"),
        "amount": text("$1,250.50", "NA", "NA", "(20)", "7", "12%"),
        "signup": text("2026-01-15", "01/02/2026", "01/02/2026", None, "2026-03-01 10:30", "2026-03-02"),
        "active": text("yes", "no", "no", "Y", None, "N"),
        "city": text(" Wichita", "NY", "NY", "new  york", "WICHITA", None),
    })


NUMERIC = {"thousands_separator": ",", "currency": True, "parentheses_negative": True, "percent": "fraction"}
STEPS = [
    ("standardize_missing", {"columns": ["amount"], "tokens": ["NA"]}),
    ("convert_type", {"columns": ["amount"], "target": "decimal", **NUMERIC}),
    ("add_missing_indicator", {"column": "amount"}),
    ("convert_type", {"columns": ["amount_missing"], "target": "boolean", "true_values": ["true"],
                      "false_values": ["false"]}),
    ("fill_missing", {"columns": ["amount"], "method": "median"}),
    ("convert_type", {"columns": ["signup"], "target": "datetime", "date_formats": ["ymd", "mdy"]}),
    ("convert_type", {"columns": ["active"], "target": "boolean", "true_values": ["yes", "y"],
                      "false_values": ["no", "n"]}),
    ("trim_whitespace", {"columns": ["city"]}),
    ("collapse_whitespace", {"columns": ["city"]}),
    ("map_categories", {"column": "city", "mapping": [["NY", "New York"], ["new york", "New York"],
                                                       ["WICHITA", "Wichita"]]}),
    ("remove_duplicates", {"columns": None}),
]


def apply(session, op, params):
    return session.apply(session.preview(op, params), confirmed=True)


def run_all(session):
    for op, params in STEPS:
        apply(session, op, params)
    return session


def test_full_pipeline_types_values_and_lineage():
    s = run_all(CleaningSession(frame()))
    d = s.data
    assert list(d.columns) == ["id", "amount", "amount_missing", "signup", "active", "city"]
    assert [str(t) for t in d.dtypes] == ["string", "Float64", "boolean", "datetime64[us]", "boolean", "string"]
    assert d["amount"].tolist() == [1250.5, 3.56, -20.0, 7.0, 0.12]           # Median of 1250.5, -20, 7, 0.12
    assert d["amount_missing"].tolist() == [False, True, False, False, False]
    assert d["signup"].tolist()[:2] == [pd.Timestamp("2026-01-15"), pd.Timestamp("2026-01-02")]
    assert pd.isna(d["signup"].iloc[2]) and d["signup"].iloc[3] == pd.Timestamp("2026-03-01 10:30")
    assert d["active"].tolist()[:3] == [True, False, True]
    assert d["city"].tolist()[:4] == ["Wichita", "New York", "New York", "Wichita"]
    assert s.row_ids.tolist() == [0, 1, 3, 4, 5]                              # Duplicate row 2 removed


def test_preview_needs_confirmation_and_changes_nothing():
    s = CleaningSession(frame())
    fp = s.fingerprint
    p = s.preview(*STEPS[6])
    assert s.fingerprint == fp and s.steps == ()
    with pytest.raises(CleaningError) as err:
        s.apply(p)
    assert err.value.code == "confirmation_required" and s.fingerprint == fp
    entry = s.apply(p, confirmed=True)
    assert entry.operation == "convert_type" and entry.counts["cells_converted"] == 5
    assert entry.params["true_values"] == ["yes", "y"] and entry.resolved["stats"]["active"]["converted"] == 5
    assert entry.fingerprint_before == fp and entry.fingerprint_after == s.fingerprint
    with pytest.raises(CleaningError) as err:
        s.apply(p, confirmed=True)
    assert err.value.code == "preview_used"


def test_stale_preview_is_refused():
    s = CleaningSession(frame())
    p = s.preview(*STEPS[7])
    apply(s, *STEPS[6])
    with pytest.raises(CleaningError) as err:
        s.apply(p, confirmed=True)
    assert err.value.code == "stale_preview"


def test_undo_restores_text_exactly_and_reset():
    s = CleaningSession(frame())
    states = [(s.fingerprint, s.data)]
    for op, params in STEPS:
        apply(s, op, params)
        states.append((s.fingerprint, s.data))
    for fp, data in reversed(states[:-1]):
        s.undo()
        assert s.fingerprint == fp
        pd.testing.assert_frame_equal(s.data, data)
    assert all(t == TEXT_DTYPE for t in s.data.dtypes)
    run_all(s)
    s.reset()
    assert s.fingerprint == s.original_fingerprint


def test_undo_without_cache_replays_typed_steps():
    s = run_all(CleaningSession(frame(), snapshot_limit=0))
    expected = [st.fingerprint_before for st in s.steps]
    for fp in reversed(expected):
        s.undo()
        assert s.fingerprint == fp


def test_recipe_is_json_and_replays_deterministically():
    s = run_all(CleaningSession(frame()))
    recipe = json.loads(s.recipe_json())
    data, ids = replay_recipe(frame(), recipe)
    pd.testing.assert_frame_equal(data, s.data)
    assert ids.tolist() == s.row_ids.tolist()
    assert s.verify_replay().ok
    again = run_all(CleaningSession(frame()))
    assert again.recipe_json() == s.recipe_json()                            # Same steps, same recipe


def test_tampered_recipes_are_refused():
    s = run_all(CleaningSession(frame()))
    recipe = s.recipe()
    convert = next(i for i, st in enumerate(recipe["steps"]) if st["operation"] == "convert_type")
    for change in (
        lambda r: r["steps"][convert]["resolved"].update(currency=False),
        lambda r: r["steps"][convert]["resolved"].update(target="integer"),
        lambda r: r["steps"][convert]["resolved"]["stats"]["amount"].update(failed=1),
        lambda r: r["steps"][-2]["resolved"].update(mapping=[["NY", "NYC"], ["new york", "New York"],
                                                             ["WICHITA", "Wichita"]]),
    ):
        tampered = copy.deepcopy(recipe)
        change(tampered)
        with pytest.raises(CleaningError) as err:
            replay_recipe(frame(), tampered)
        assert err.value.code == "replay_mismatch"


def test_replay_refuses_a_changed_input():
    s = run_all(CleaningSession(frame()))
    other = frame()
    other.loc[0, "amount"] = "$1,250.51"
    with pytest.raises(CleaningError) as err:
        replay_recipe(other, s.recipe())
    assert err.value.code == "replay_mismatch"


def test_fingerprints_distinguish_types_and_are_stable():
    s = CleaningSession(frame())
    apply(s, *STEPS[6])
    assert s.fingerprint != s.original_fingerprint
    assert frame_fingerprint(s.data) == frame_fingerprint(s.data.copy(deep=True))
    d = pd.DataFrame({"x": pd.array([dt.date(2026, 1, 15), None], dtype=DATE_DTYPE)})
    as_text = pd.DataFrame({"x": text("2026-01-15", None)})
    assert frame_fingerprint(d) != frame_fingerprint(as_text)                 # Dtype is part of it


# ---- cross-phase integration ---------------------------------------

def typed():
    s = CleaningSession(pd.DataFrame({
        "n": text("1", None, "3", None),
        "f": text("1.5", None, "2.5", "4"),
        "b": text("true", None, "false", "true"),
        "d": text("2026-01-15", None, "2026-01-15", "2026-01-17"),
        "t": text("2026-01-15 10:00", "2026-01-15 10:00", None, "2026-01-16 08:00"),
    }))
    apply(s, "convert_type", {"columns": ["n"], "target": "integer"})
    apply(s, "convert_type", {"columns": ["f"], "target": "decimal"})
    apply(s, "convert_type", {"columns": ["b"], "target": "boolean", "true_values": ["true"],
                              "false_values": ["false"]})
    apply(s, "convert_type", {"columns": ["d"], "target": "date", "date_formats": ["ymd"]})
    apply(s, "convert_type", {"columns": ["t"], "target": "datetime", "date_formats": ["ymd"]})
    return s


@pytest.mark.parametrize("params, column, expected", [
    ({"method": "constant", "value": "0"}, "n", [1, 0, 3, 0]),
    ({"method": "mean", "decimals": None}, "n", [1, 2, 3, 2]),
    ({"method": "median", "decimals": None}, "f", [1.5, 2.5, 2.5, 4.0]),
    ({"method": "ffill"}, "n", [1, 1, 3, 3]),
    ({"method": "bfill"}, "b", [True, False, False, True]),
    ({"method": "mode"}, "b", [True, True, False, True]),
    ({"method": "constant", "value": "2026-01-01"}, "d",
     [dt.date(2026, 1, 15), dt.date(2026, 1, 1), dt.date(2026, 1, 15), dt.date(2026, 1, 17)]),
    ({"method": "mode"}, "d",
     [dt.date(2026, 1, 15), dt.date(2026, 1, 15), dt.date(2026, 1, 15), dt.date(2026, 1, 17)]),
    ({"method": "ffill"}, "t", [pd.Timestamp("2026-01-15 10:00")] * 3 + [pd.Timestamp("2026-01-16 08:00")]),
])
def test_fill_keeps_typed_columns_typed(params, column, expected):
    s = typed()
    dtype = s.data[column].dtype
    apply(s, "fill_missing", {"columns": [column], **params})
    assert s.data[column].dtype == dtype
    assert s.data[column].tolist() == expected
    assert s.verify_replay().ok


@pytest.mark.parametrize("params, fragment", [
    ({"columns": ["n"], "method": "constant", "value": "1.5"}, "is stored as Int64"),
    ({"columns": ["b"], "method": "constant", "value": "yes"}, "not true or false"),
    ({"columns": ["d"], "method": "constant", "value": "15/01/2026"}, "YYYY-MM-DD"),
    ({"columns": ["d"], "method": "mean"}, "needs numbers"),
    ({"columns": ["b"], "method": "median"}, "needs numbers"),
    ({"columns": ["n"], "method": "mean", "decimals": None, "non_numeric": "reject"}, None),
])
def test_fill_refuses_values_a_typed_column_cannot_hold(params, fragment):
    s = typed()
    if fragment is None:                          # Mean of 1 and 3 is whole: allowed
        s.preview("fill_missing", params)
        return
    with pytest.raises(CleaningError) as err:
        s.preview("fill_missing", params)
    assert fragment in err.value.message


def test_integer_mean_that_is_not_whole_needs_rounding():
    s = CleaningSession(pd.DataFrame({"n": text("1", "2", None)}))
    apply(s, "convert_type", {"columns": ["n"], "target": "integer"})
    with pytest.raises(CleaningError, match="Round to 0 decimal places"):
        s.preview("fill_missing", {"columns": ["n"], "method": "mean", "decimals": None})
    apply(s, "fill_missing", {"columns": ["n"], "method": "mean", "decimals": 0})
    assert s.data["n"].tolist() == [1, 2, 2] and str(s.data["n"].dtype) == "Int64"


def test_missing_value_operations_on_typed_columns():
    s = typed()
    apply(s, "drop_missing_rows", {"columns": ["b"]})
    assert s.shape[0] == 3 and str(s.data["b"].dtype) == "boolean"
    apply(s, "add_missing_indicator", {"column": "n"})
    assert s.data["n_missing"].tolist() == ["false", "false", "true"]        # Rows 0, 2, 3 remain
    apply(s, "drop_missing_columns", {"threshold": 30})                    # 'n' and 't' are 1/3 missing
    assert s.columns == ("n_missing", "f", "b", "d") and str(s.data["f"].dtype) == "Float64"
    with pytest.raises(CleaningError, match="works on text columns"):
        s.preview("standardize_missing", {"columns": ["f"], "tokens": ["NA"]})


def test_duplicates_on_typed_columns():
    s = typed()
    p = s.preview("remove_duplicates", {"columns": ["t"]})
    assert p.plan.resolved["rows_removed"] == 1                             # Two equal datetimes
    p = s.preview("remove_duplicates", {"columns": ["d"], "ignore_case": True, "normalize_whitespace": True})
    assert p.plan.resolved["rows_removed"] == 1
    s.apply(p, confirmed=True)
    assert s.data["d"].dtype == DATE_DTYPE and s.row_ids.tolist() == [0, 1, 3]


def test_text_cleaning_after_missing_value_standardization():
    s = CleaningSession(pd.DataFrame({"c": text(" NA ", " a ", "NA", None)}))
    apply(s, "standardize_missing", {"columns": ["c"], "tokens": ["NA"]})
    apply(s, "trim_whitespace", {"columns": ["c"]})
    assert [None if pd.isna(v) else v for v in s.data["c"].tolist()] == [None, "a", None, None]


def test_phase4_indicator_converts_to_real_booleans():
    s = CleaningSession(pd.DataFrame({"x": text("1", None, "3")}))
    apply(s, "add_missing_indicator", {"column": "x"})
    apply(s, "convert_type", {"columns": ["x_missing"], "target": "boolean", "true_values": ["true"],
                              "false_values": ["false"]})
    assert s.data["x_missing"].tolist() == [False, True, False] and str(s.data["x_missing"].dtype) == "boolean"


def test_profile_and_quality_after_conversion():
    s = typed()
    p = profile_dataset(s.data)
    kinds = {c.name: (c.inference.semantic_type, c.inference.status) for c in p.columns}
    assert kinds == {"n": ("integer", "confirmed"), "f": ("decimal", "confirmed"), "b": ("boolean", "confirmed"),
                     "d": ("date", "confirmed"), "t": ("datetime", "confirmed")}
    assert p.column("n").missing_count == 2 and p.column("d").distinct_count == 2
    assert [v.value for v in p.column("b").top_values] == ["true", "false"]
    assert p.column("d").top_values[0].value == "2026-01-15"
    assert p.column("f").numeric_stats.mean == pytest.approx(8 / 3)
    rules = (ValidityRule("f", "range", minimum=0, maximum=3),
             ValidityRule("b", "allowed_values", allowed=("true",)),
             ValidityRule("d", "pattern", pattern=r"2026-01-1[0-9]"))
    report = assess_quality(s.data, p, rules)
    by_check = {i.check: i for i in report.issues if i.check.startswith("rule:")}
    assert [i.affected_count for i in by_check.values()] == [1, 1, 0][:len(by_check)]
    assert not any(i.check in ("edge_whitespace", "case_variants") for i in report.issues)


def test_sample_dataset_end_to_end():
    ds = load_csv((PROJECT_DIR / "sample_data" / "messy_customers.csv").read_bytes())
    s = CleaningSession(ds.data, source_rows=ds.source_rows)
    apply(s, "standardize_missing", {"columns": ["annual_spend", "age", "signup_date"],
                                     "tokens": ["NA", "N/A", "null", "-", "unknown"], "case_sensitive": False,
                                     "empty_text": True, "whitespace_only": True})
    apply(s, "convert_type", {"columns": ["annual_spend"], "target": "decimal", "on_error": "coerce", **NUMERIC})
    apply(s, "convert_type", {"columns": ["age"], "target": "integer", "on_error": "coerce",
                              "fraction": "round_half_even"})
    apply(s, "convert_type", {"columns": ["is_active"], "target": "boolean",
                              "true_values": ["true", "yes", "y", "1"], "false_values": ["false", "no", "n", "0"]})
    apply(s, "convert_type", {"columns": ["signup_date"], "target": "date", "on_error": "coerce",
                              "date_formats": ["ymd", "mdy", "month_name"]})
    apply(s, "trim_whitespace", {"columns": ["segment", "country"]})
    apply(s, "collapse_whitespace", {"columns": ["segment"]})
    apply(s, "change_case", {"columns": ["segment"], "case": "title"})
    assert sorted(s.data["segment"].dropna().unique()) == ["Consumer", "Corporate", "Small Business"]
    assert str(s.data["is_active"].dtype) == "boolean" and s.data["is_active"].notna().all()
    assert s.verify_replay().ok
    profile = profile_dataset(s.data)
    assert profile.column("annual_spend").inference.status == "confirmed"
