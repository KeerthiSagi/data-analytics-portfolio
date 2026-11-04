"""Text standardization: rules, operations, missing-value preservation and replay guards."""

import numpy as np
import pandas as pd
import pytest

from refine.errors import CleaningError
from refine.operations import get_operation
from refine.source import TEXT_DTYPE
from refine.text import (
    change_case,
    collapse_whitespace,
    control_table,
    find_replace_function,
    mapping_problems,
    normalize_unicode,
    suggest_categories,
    trim,
    value_frequencies,
)


def text(*values):
    return pd.array(list(values), dtype=TEXT_DTYPE)


def run(op_id, params, df):
    op = get_operation(op_id)
    p = op.plan(params, df)
    return p, op.apply(df, np.arange(len(df)), p.resolved)


def col(result, name):
    return [None if pd.isna(v) else v for v in result.data[name].tolist()]


# ---- rules ---------------------------------------------------------

def test_rules():
    assert trim("  Wichita  ") == "Wichita" and trim("\tA ") == "A"
    assert change_case("WICHITA", "lower") == "wichita"
    assert change_case("wIchItA", "title") == "Wichita"
    assert change_case("new york-city", "title") == "New York-City"
    assert change_case("3rd street", "title") == "3rd Street"
    assert change_case("Straße", "casefold") == "strasse"
    assert change_case("abc", "upper") == "ABC"
    assert collapse_whitespace("New    York", False) == "New York"
    assert collapse_whitespace("New\t\tYork", False) == "New\t\tYork"              # Tabs kept unless asked
    assert collapse_whitespace("New\t \nYork", True) == "New York"
    assert normalize_unicode("ＡＢＣ１２３", "NFKC") == "ABC123"
    assert normalize_unicode("ﬁ①", "NFKC") == "fi1"
    assert normalize_unicode("é", "NFC") == "é"
    assert normalize_unicode("ＡＢＣ", "NFC") == "ＡＢＣ"


def test_control_characters_keep_multilingual_text():
    keep = control_table(keep_whitespace=True, remove_invisible=False)
    assert "a\x00b\x1fc\x7f\x85".translate(keep) == "abc"
    assert "a\tb\nc".translate(keep) == "a\tb\nc"
    assert "a\tb".translate(control_table(False, False)) == "ab"
    multilingual = "Zoë 東京 Москва مرحبا नमस्ते 👩‍💻 می‌خواهم"
    assert multilingual.translate(control_table(True, True)) == multilingual       # ZWJ / ZWNJ kept
    assert "a​b﻿".translate(control_table(True, True)) == "ab"
    assert "a​b".translate(keep) == "a​b"                                 # Only when chosen


def test_find_replace_rules():
    whole = find_replace_function("NY", "New York", whole_cell=True, case_sensitive=True)
    assert whole("NY") == ("New York", 1) and whole("NYC") == ("NYC", 0) and whole("ny") == ("ny", 0)
    whole_ci = find_replace_function("NY", "New York", whole_cell=True, case_sensitive=False)
    assert whole_ci("ny") == ("New York", 1)
    sub = find_replace_function("a", "o", whole_cell=False, case_sensitive=True)
    assert sub("banana") == ("bonono", 3)
    sub_ci = find_replace_function("st.", r"Street\1", whole_cell=False, case_sensitive=False)
    assert sub_ci("Main ST. and 2nd st.") == (r"Main Street\1 and 2nd Street\1", 2)     # Literal, no regex
    dots = find_replace_function(".*", "", whole_cell=False, case_sensitive=False)
    assert dots("a.*b") == ("ab", 1) and dots("abc") == ("abc", 0)


def test_category_helpers():
    freqs = value_frequencies(pd.Series(text("New York", "new york", "New York", "NY", "New  York", None)))
    assert freqs == [("New York", 2), ("new york", 1), ("NY", 1), ("New  York", 1)]
    assert suggest_categories(freqs) == {"new york": "New York", "New  York": "New York"}
    tidy = [("CONSUMER ", 56), ("consumer", 51), ("Consumer", 46)]
    assert suggest_categories(tidy) == {"CONSUMER ": "consumer", "Consumer": "consumer"}   # No stray space


def test_ui_mapping_builder_latest_choice_wins():
    from ui.clean_text import add_pairs
    pairs = add_pairs({}, [("CONSUMER ", "consumer"), ("Consumer", "consumer")])
    pairs = add_pairs(pairs, [("consumer", "Consumer")])
    assert pairs == {"CONSUMER ": "Consumer", "consumer": "Consumer"}
    assert not mapping_problems(list(pairs.items()))
    assert mapping_problems([("a", "A"), ("a", "B")])
    assert mapping_problems([("a", "b"), ("b", "c")])
    assert not mapping_problems([("a", "A"), ("b", "A"), ("A", "A")])


# ---- operations ----------------------------------------------------

def frame():
    return pd.DataFrame({
        "city": text("  Wichita ", "WICHITA", None, "New    York", "", "   ", "NY"),
        "note": text("a\x00", "b", None, "c", "d", "e", "f"),
        "n": pd.array([1, 2, None, 4, 5, 6, 7], dtype="Int64"),
    })


def test_trim_preserves_missing_and_reports_new_empty_text():
    p, out = run("trim_whitespace", {"columns": ["city"]}, frame())
    assert col(out, "city") == ["Wichita", "WICHITA", None, "New    York", "", "", "NY"]
    assert p.resolved["cells"] == {"city": 2} and p.resolved["became_empty"] == 1
    assert any("empty text" in w for w in p.warnings)
    assert out.data["city"].dtype == TEXT_DTYPE and out.data["note"].equals(frame()["note"])


@pytest.mark.parametrize("case, expected", [
    ("lower", "wichita"), ("upper", "WICHITA"), ("title", "Wichita"), ("casefold", "wichita"),
])
def test_change_case(case, expected):
    df = pd.DataFrame({"c": text("wIchItA", None)})
    p, out = run("change_case", {"columns": ["c"], "case": case}, df)
    assert col(out, "c") == [expected, None]
    if case == "title":
        assert any("McDonald" in w for w in p.warnings)


def test_collapse_whitespace_and_unicode_and_controls():
    _, out = run("collapse_whitespace", {"columns": ["city"]}, frame())
    assert col(out, "city")[3] == "New York" and col(out, "city")[0] == " Wichita "
    df = pd.DataFrame({"c": text("ＡＢＣ", "abc", None)})
    p, out = run("normalize_unicode", {"columns": ["c"], "form": "NFKC"}, df)
    assert col(out, "c") == ["ABC", "abc", None] and any("compatibility" in w for w in p.warnings)
    _, out = run("remove_control_characters", {"columns": ["note"]}, frame())
    assert col(out, "note")[0] == "a"


def test_find_replace_operation_counts():
    df = pd.DataFrame({"c": text("banana", "apple", None, "Banana")})
    p, out = run("find_replace", {"columns": ["c"], "find": "an", "replace": "AN", "match": "substring"}, df)
    assert col(out, "c") == ["bANANa", "apple", None, "BANANa"]
    assert p.resolved["cells"] == {"c": 2} and out.counts["replacements"] == 4
    p, out = run("find_replace", {"columns": ["c"], "find": "banana", "replace": "B", "match": "whole_cell",
                                  "case_sensitive": False}, df)
    assert col(out, "c") == ["B", "apple", None, "B"]


def test_find_replace_validation():
    op = get_operation("find_replace")
    df = pd.DataFrame({"c": text("a")})
    with pytest.raises(CleaningError, match="Enter the text to find"):
        op.plan({"columns": ["c"], "find": "", "match": "substring"}, df)
    with pytest.raises(CleaningError) as err:
        op.plan({"columns": ["c"], "find": "zzz", "replace": "y", "match": "substring"}, df)
    assert err.value.code == "no_change"


def test_map_categories():
    df = pd.DataFrame({"city": text("NY", "new york", "New  York", "New York", None, "Boston")})
    p, out = run("map_categories", {"column": "city", "mapping": [["NY", "New York"], ["new york", "New York"],
                                                                   ["New  York", "New York"],
                                                                   ["New York", "New York"]]}, df)
    assert col(out, "city") == ["New York"] * 4 + [None, "Boston"]
    assert p.resolved["mapping"] == [["NY", "New York"], ["new york", "New York"], ["New  York", "New York"]]
    assert p.resolved["cells"] == {"city": 3}


@pytest.mark.parametrize("mapping, fragment", [
    ([["NY", "A"], ["NY", "B"]], "mapped to both"),
    ([["NY", "new york"], ["new york", "New York"]], "also mapped"),
    ([["LA", "Los Angeles"]], "has no value 'LA'"),
    ([["NY", ""]], "empty text"),
    ([["NY", "NY"]], "at least one value"),
    ("NY=New York", "list of"),
])
def test_map_categories_conflicts(mapping, fragment):
    df = pd.DataFrame({"city": text("NY", "new york")})
    with pytest.raises(CleaningError) as err:
        get_operation("map_categories").plan({"column": "city", "mapping": mapping}, df)
    assert fragment in err.value.message


@pytest.mark.parametrize("op_id, params", [
    ("trim_whitespace", {}), ("change_case", {"case": "lower"}), ("collapse_whitespace", {}),
    ("normalize_unicode", {}), ("remove_control_characters", {}),
    ("find_replace", {"find": "1", "replace": "2", "match": "substring"}),
])
def test_text_operations_refuse_typed_columns(op_id, params):
    with pytest.raises(CleaningError) as err:
        get_operation(op_id).plan({"columns": ["n"], **params}, frame())
    assert "works on text columns" in err.value.message and "Convert it to text first" in err.value.message


def test_map_categories_refuses_typed_column():
    with pytest.raises(CleaningError, match="works on text columns"):
        get_operation("map_categories").plan({"column": "n", "mapping": [["1", "one"]]}, frame())


def test_no_change_is_reported():
    df = pd.DataFrame({"c": text("abc", None)})
    for op_id, params in [("trim_whitespace", {}), ("change_case", {"case": "lower"}),
                          ("collapse_whitespace", {}), ("normalize_unicode", {}),
                          ("remove_control_characters", {})]:
        with pytest.raises(CleaningError) as err:
            get_operation(op_id).plan({"columns": ["c"], **params}, df)
        assert err.value.code == "no_change"


def test_apply_refuses_changed_data():
    op = get_operation("trim_whitespace")
    p = op.plan({"columns": ["city"]}, frame())
    changed = frame()
    changed.loc[1, "city"] = " extra "
    with pytest.raises(CleaningError) as err:
        op.apply(changed, np.arange(7), p.resolved)
    assert err.value.code == "replay_mismatch"
