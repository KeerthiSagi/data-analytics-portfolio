"""Convert data type as an operation: settings, failure policies, frozen plans, replay guards."""

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from refine.conversion import DATE_DTYPE
from refine.errors import CleaningError
from refine.operations import get_operation
from refine.source import TEXT_DTYPE

OP = get_operation("convert_type")


def text(*values):
    return pd.array(list(values), dtype=TEXT_DTYPE)


def frame():
    return pd.DataFrame({
        "amount": text("1,250", "$3.50", None, "x", "12"),
        "when": text("2026-01-15", "01/02/2026", "", "2026-03-01", "bad"),
        "flag": text("true", "false", "true", None, "false"),
        "name": text("a", "b", "c", "d", "e"),
    })


def plan(params, data=None):
    return OP.plan(params, frame() if data is None else data)


def error(params, data=None):
    with pytest.raises(CleaningError) as err:
        plan(params, data)
    return err.value


def test_strict_refuses_and_shows_failed_values():
    e = error({"columns": ["amount"], "target": "decimal", "thousands_separator": ",", "currency": True})
    assert e.code == "invalid_params"
    assert "'x' × 1: contains letters" in e.message and "Convert invalid values to missing" in e.message


def test_coerce_freezes_counts_and_warns():
    p = plan({"columns": ["amount"], "target": "decimal", "on_error": "coerce", "thousands_separator": ",",
              "currency": True})
    assert p.resolved["stats"]["amount"]["converted"] == 3 and p.resolved["stats"]["amount"]["failed"] == 1
    assert p.resolved["failures"]["amount"] == [["x", 1, "contains letters"]]
    assert p.resolved["source_dtypes"] == {"amount": "string[pyarrow]/NA"}
    assert any("would become missing" in w and "25.0%" in w for w in p.warnings)
    assert "1 invalid value becomes missing" in p.summary


def test_preserve_is_refused_with_an_explanation():
    e = error({"columns": ["amount"], "target": "integer", "on_error": "preserve"})
    assert "cannot also hold text" in e.message and "leave the column as text" in e.message


def test_every_value_failing_is_refused_even_with_coerce():
    e = error({"columns": ["name"], "target": "integer", "on_error": "coerce"})
    assert "No value in 'name' converts" in e.message


def test_settings_are_validated():
    assert "not used when converting" in error({"columns": ["amount"], "target": "integer",
                                                 "date_formats": ["ymd"]}).message
    assert "must be different" in error({"columns": ["amount"], "target": "decimal", "thousands_separator": ".",
                                         "decimal_separator": "."}).message
    assert "never guesses" in error({"columns": ["when"], "target": "date"}).message
    assert "not both" in error({"columns": ["when"], "target": "date", "date_formats": ["mdy", "dmy"]}).message
    assert "on its own" in error({"columns": ["when"], "target": "date", "date_formats": ["ymd", "custom"],
                                  "custom_format": "%Y"}).message
    assert "does not assume" in error({"columns": ["flag"], "target": "boolean", "true_values": ["true"]}).message
    assert "both true and false" in error({"columns": ["flag"], "target": "boolean", "true_values": ["Yes"],
                                           "false_values": ["yes"]}).message
    assert "Unknown setting" in error({"columns": ["flag"], "target": "boolean", "colour": 1}).message
    assert "one of" in error({"columns": ["flag"], "target": "money"}).message


def test_case_sensitive_mapping_allows_different_spellings():
    p = plan({"columns": ["flag"], "target": "boolean", "true_values": ["true", "T"], "false_values": ["false", "t"],
              "case_sensitive": True})
    assert p.resolved["stats"]["flag"]["converted"] == 4


def test_typed_columns_follow_the_supported_pairs():
    df = frame()
    df["n"] = pd.array([1, 2, None, 4, 5], dtype="Int64")
    df["b"] = pd.array([True, False, None, True, False], dtype="boolean")
    assert error({"columns": ["n"], "target": "integer"}, df).code == "no_change"
    assert "Convert it to text first" in error({"columns": ["b"], "target": "integer"}, df).message
    assert "apply to text columns only" in error({"columns": ["n"], "target": "decimal", "currency": True},
                                                 df).message
    assert "one kind at a time" in error({"columns": ["n", "amount"], "target": "decimal"}, df).message
    assert plan({"columns": ["n"], "target": "decimal"}, df).resolved["stats"]["n"]["converted"] == 4
    assert plan({"columns": ["b"], "target": "string"}, df).resolved["stats"]["b"]["converted"] == 4


def test_apply_converts_only_selected_columns_and_keeps_order():
    df = frame()
    p = plan({"columns": ["flag"], "target": "boolean", "true_values": ["true"], "false_values": ["false"]}, df)
    ids = np.array([10, 11, 12, 13, 14])
    out = OP.apply(df, ids, p.resolved)
    assert list(out.data.columns) == list(df.columns) and out.row_ids.tolist() == ids.tolist()
    assert str(out.data["flag"].dtype) == "boolean"
    assert out.data["flag"].tolist()[:3] == [True, False, True] and pd.isna(out.data["flag"].iloc[3])
    for c in ("amount", "when", "name"):
        assert out.data[c].equals(df[c])
    assert df["flag"].dtype == TEXT_DTYPE                                  # Input unchanged
    assert out.counts == {"columns_converted": 1, "cells_converted": 4, "cells_failed": 0, "new_missing": 0}


def test_date_conversion_through_the_operation():
    p = plan({"columns": ["when"], "target": "date", "date_formats": ["ymd", "mdy"], "on_error": "coerce"})
    out = OP.apply(frame(), np.arange(5), p.resolved).data
    assert out["when"].dtype == DATE_DTYPE
    assert out["when"].tolist()[:2] == [dt.date(2026, 1, 15), dt.date(2026, 1, 2)]
    assert any("read month-first" in w for w in p.warnings)               # 01/02/2026 is ambiguous


def test_apply_refuses_data_that_does_not_match_the_frozen_plan():
    p = plan({"columns": ["amount"], "target": "decimal", "on_error": "coerce", "thousands_separator": ",",
              "currency": True})
    changed = frame()
    changed.loc[0, "amount"] = "oops"
    with pytest.raises(CleaningError) as err:
        OP.apply(changed, np.arange(5), p.resolved)
    assert err.value.code == "replay_mismatch"
    typed = frame().assign(amount=pd.array([1.0, 2.0, None, None, 3.0], dtype="Float64"))
    with pytest.raises(CleaningError) as err:
        OP.apply(typed, np.arange(5), p.resolved)
    assert err.value.code == "replay_mismatch" and "expects string" in err.value.message


def test_tampered_policy_is_refused_on_apply():
    p = plan({"columns": ["amount"], "target": "decimal", "on_error": "coerce", "thousands_separator": ",",
              "currency": True})
    tampered = {**p.resolved, "on_error": "strict"}
    with pytest.raises(CleaningError) as err:
        OP.apply(frame(), np.arange(5), tampered)
    assert err.value.code == "replay_mismatch"


def test_sample_shows_failed_rows_first_as_text():
    p = plan({"columns": ["amount"], "target": "decimal", "on_error": "coerce", "thousands_separator": ",",
              "currency": True})
    sample = OP.sample(frame(), p.resolved, 10)
    assert list(sample.columns) == ["amount", "amount (after)"]
    assert sample.loc[3, "amount"] == "x" and pd.isna(sample.loc[3, "amount (after)"])
    assert sample.loc[0, "amount (after)"] == "1250.0"


def test_memo_never_answers_for_an_edited_table():
    df = frame()
    p = plan({"columns": ["flag"], "target": "boolean", "true_values": ["true"], "false_values": ["false"]}, df)
    df.loc[0, "flag"] = "maybe"                                            # Edited in place after planning
    with pytest.raises(CleaningError) as err:
        OP.apply(df, np.arange(5), p.resolved)
    assert err.value.code == "replay_mismatch"
