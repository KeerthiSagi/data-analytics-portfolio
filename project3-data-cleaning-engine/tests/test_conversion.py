"""Phase 5 parsing rules (refine/conversion.py): numbers, dates, booleans and typed columns."""

import datetime as dt
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from refine.conversion import (
    DATE_DTYPE,
    BooleanParser,
    DateParser,
    NumberParser,
    canonical_text,
    check_custom_format,
    column_kind,
    convert_column,
    decimal_to_float,
    decimal_to_int,
    parse_fill_value,
)
from refine.source import TEXT_DTYPE

ALL_NUMERIC = {"thousands_separator": ",", "decimal_separator": ".", "currency": True,
               "parentheses_negative": True, "percent": "fraction"}


def text(*values):
    return pd.Series(pd.array(list(values), dtype=TEXT_DTYPE), name="c")


def number(value, **options):
    return NumberParser({**{"thousands_separator": None, "decimal_separator": ".", "currency": False,
                            "parentheses_negative": False, "percent": "reject"}, **options})(value)


def values(series):
    return [None if pd.isna(v) else v for v in series.tolist()]


# ---- numbers --------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("1250", Decimal("1250")), ("-42", Decimal("-42")), ("3.14", Decimal("3.14")), ("+7", Decimal("7")),
    (".5", Decimal("0.5")), ("1e3", Decimal("1e3")), ("  12  ", Decimal("12")),
])
def test_plain_numbers_need_no_options(raw, expected):
    value, reason, _ = number(raw)
    assert reason is None and value == expected


@pytest.mark.parametrize("raw, options, expected", [
    ("1,250", {"thousands_separator": ","}, Decimal("1250")),
    ("1,234,567.89", {"thousands_separator": ","}, Decimal("1234567.89")),
    ("$1,250.50", {"thousands_separator": ",", "currency": True}, Decimal("1250.50")),
    ("-$5", {"currency": True}, Decimal("-5")),
    ("$-5", {"currency": True}, Decimal("-5")),
    ("1250 €", {"currency": True}, Decimal("1250")),
    ("(250.75)", {"parentheses_negative": True}, Decimal("-250.75")),
    ("($1,000)", {"parentheses_negative": True, "currency": True, "thousands_separator": ","}, Decimal("-1000")),
    ("12.5%", {"percent": "fraction"}, Decimal("0.125")),
    ("12.5%", {"percent": "number"}, Decimal("12.5")),
    ("1.250,50", {"thousands_separator": ".", "decimal_separator": ","}, Decimal("1250.50")),
    ("1 250", {"thousands_separator": " "}, Decimal("1250")),
    ("1 250", {"thousands_separator": " "}, Decimal("1250")),
    ("1'250", {"thousands_separator": "'"}, Decimal("1250")),
])
def test_opt_in_formats(raw, options, expected):
    value, reason, _ = number(raw, **options)
    assert reason is None and value == expected


@pytest.mark.parametrize("raw, fragment", [
    ("1,250", "turn on thousands-separator parsing"),
    ("$1250", "turn on currency parsing"),
    ("(250)", "turn on parentheses"),
    ("12%", "choose how to read percentages"),
    ("abc", "contains letters"),
    ("inf", "not a finite number"),
    ("-Infinity", "not a finite number"),
    ("NaN", "not a finite number"),
    ("   ", "only whitespace"),
    ("1" * 101, "longer than"),
    ("1-2", "not a number"),
])
def test_rejections_without_options(raw, fragment):
    value, reason, _ = number(raw)
    assert value is None and fragment in reason


@pytest.mark.parametrize("raw, fragment", [
    ("1,25,000", "groups of three"),
    ("12,34", "groups of three"),
    ("1,250.5.5", "not"),
    ("$1,250$", "more than one currency symbol"),
    ("$12%", "both a currency symbol and a percent sign"),
    ("-(5)", "not a number"),
    ("(-5)", "both parentheses and a sign"),
])
def test_malformed_values_with_options(raw, fragment):
    value, reason, _ = NumberParser(ALL_NUMERIC)(raw)
    assert value is None and fragment in reason


def test_separator_ambiguity_is_flagged():
    _, _, flags = number("1,250", thousands_separator=",")
    assert "ambiguous" in flags
    _, _, flags = number("1,250.00", thousands_separator=",")
    assert "ambiguous" not in flags


def test_integers_never_truncate_silently():
    assert decimal_to_int(Decimal("12.7"), "reject")[0] is None
    assert "fractional part" in decimal_to_int(Decimal("12.7"), "reject")[1]
    assert decimal_to_int(Decimal("12.0"), "reject")[0] == 12


@pytest.mark.parametrize("raw, even, up", [
    ("2.5", 2, 3), ("3.5", 4, 4), ("-2.5", -2, -3), ("12.7", 13, 13), ("12.2", 12, 12), ("0.5", 0, 1),
])
def test_explicit_rounding_rules(raw, even, up):
    assert decimal_to_int(Decimal(raw), "round_half_even")[0] == even
    assert decimal_to_int(Decimal(raw), "round_half_up")[0] == up
    assert "rounded" in decimal_to_int(Decimal(raw), "round_half_even")[2]


def test_integer_overflow_is_detected():
    assert decimal_to_int(Decimal(2 ** 63 - 1), "reject")[0] == 2 ** 63 - 1
    assert decimal_to_int(Decimal(-(2 ** 63)), "reject")[0] == -(2 ** 63)
    for big in (Decimal(2 ** 63), Decimal("1e19"), Decimal("1e999999")):
        value, reason, _ = decimal_to_int(big, "reject")
        assert value is None and "too large" in reason


def test_float_overflow_and_precision():
    assert decimal_to_float(Decimal("1e400"))[0] is None
    assert decimal_to_float(Decimal("1e999999999"))[0] is None
    assert decimal_to_float(Decimal("0.1")) == (0.1, None, frozenset())
    assert decimal_to_float(Decimal(2 ** 60))[2] == frozenset()             # Exactly representable
    assert "precision" in decimal_to_float(Decimal("3.14159265358979323846"))[2]
    assert "precision" in decimal_to_float(Decimal("12345678901234567"))[2]


def test_numeric_column_conversion_counts_and_missing():
    s = text("1250", "-42", "3.14", "1,250", "$1,250.50", "(250.75)", "12.5%", "", None, "abc", "inf")
    new, stats, failed = convert_column(s, "decimal", ALL_NUMERIC)
    assert str(new.dtype) == "Float64"
    assert values(new) == [1250.0, -42.0, 3.14, 1250.0, 1250.5, -250.75, 0.125, None, None, None, None]
    assert (stats.rows, stats.existing_missing, stats.converted, stats.failed) == (11, 2, 7, 2)
    assert failed.tolist() == [False] * 9 + [True, True]
    assert {f.before for f in stats.failures} == {"abc", "inf"}
    assert stats.ambiguous == 1 and stats.failed_pct == pytest.approx(100 * 2 / 9)


def test_missing_values_never_become_text():
    s = text(None, "", "5")
    for target, options in [("integer", {}), ("decimal", {}), ("boolean", {"true_values": ["5"],
                                                                            "false_values": ["0"]})]:
        new, stats, _ = convert_column(s, target, options)
        assert new.isna().tolist() == [True, True, False]
        assert stats.existing_missing == 2 and stats.failed == 0
    back, _, _ = convert_column(convert_column(s, "integer", {})[0], "string", {})
    assert values(back) == [None, None, "5"] and not back.isin(["nan", "None", "<NA>", "NaT"]).any()


def test_whitespace_only_is_a_failure_not_missing():
    _, stats, _ = convert_column(text("  ", "1"), "integer", {})
    assert stats.failed == 1 and "only whitespace" in stats.failures[0].reason


def test_trimmed_values_are_counted():
    _, stats, _ = convert_column(text(" 7 ", "8"), "integer", {})
    assert stats.converted == 2 and stats.trimmed == 1


# ---- dates ---------------------------------------------------------

def date(raw, *formats, custom=None):
    return DateParser({"date_formats": list(formats), "custom_format": custom})(raw)


def test_iso_and_explicit_orders():
    assert date("2026-01-15", "ymd")[0].day == 15
    assert date("2026/01/15", "ymd")[0].month == 1
    m = date("01/15/2026", "mdy")[0]
    assert (m.month, m.day) == (1, 15)
    m = date("15/01/2026", "dmy")[0]
    assert (m.month, m.day) == (1, 15)
    m = date("15.01.2026", "dmy")[0]
    assert (m.month, m.day) == (1, 15)
    assert date("Jan 15, 2026", "month_name")[0].day == 15
    assert date("15 January 2026", "month_name")[0].month == 1


def test_ambiguous_dates_follow_the_explicit_choice_and_are_flagged():
    mdy, _, flags = date("05/06/2026", "mdy")
    assert (mdy.month, mdy.day) == (5, 6) and "ambiguous" in flags
    dmy, _, flags = date("05/06/2026", "dmy")
    assert (dmy.month, dmy.day) == (6, 5) and "ambiguous" in flags
    assert "ambiguous" not in date("05/05/2026", "mdy")[2]                # Same either way
    assert "ambiguous" not in date("01/15/2026", "mdy")[2]                # Only one reading is valid


@pytest.mark.parametrize("raw, formats, fragment", [
    ("01/15/2026", ("ymd",), "choose which order"),
    ("2026-01-15", ("mdy",), "year-first"),
    ("2026-02-30", ("ymd",), "not a real calendar date"),
    ("15/01/2026", ("mdy",), "not a real calendar date"),
    ("05/06/26", ("mdy",), "2-digit year"),
    ("Jan 15, 2026", ("ymd",), "month name"),
    ("2026-01-15 2:30 PM", ("ymd",), "AM/PM"),
    ("2026-01-15 25:00", ("ymd",), "not a valid time"),
    ("Smarch 1, 2026", ("month_name",), "not an English month name"),
    ("soon", ("ymd", "mdy"), "not a date"),
])
def test_invalid_dates(raw, formats, fragment):
    value, reason, _ = date(raw, *formats)
    assert value is None and fragment in reason


def test_date_target_rejects_time_unless_dropped_explicitly():
    s = text("2026-01-15", "2026-01-15 14:30:00", "2026-01-16 00:00:00", None)
    new, stats, _ = convert_column(s, "date", {"date_formats": ["ymd"], "time_of_day": "reject"})
    assert new.dtype == DATE_DTYPE
    assert values(new) == [dt.date(2026, 1, 15), None, dt.date(2026, 1, 16), None]
    assert stats.failed == 1 and "time of day" in stats.failures[0].reason
    new, stats, _ = convert_column(s, "date", {"date_formats": ["ymd"], "time_of_day": "drop"})
    assert values(new)[1] == dt.date(2026, 1, 15) and stats.time_dropped == 1 and stats.failed == 0


def test_datetime_target_keeps_time_and_never_shifts_silently():
    s = text("2026-01-15 14:30:00", "2026-01-15T14:30", "2026-01-15 14:30:00.250", "2026-01-15",
             "2026-01-15T14:30:00+02:00", None)
    new, stats, _ = convert_column(s, "datetime", {"date_formats": ["ymd"], "timezone": "reject"})
    assert str(new.dtype) == "datetime64[us]"
    assert values(new)[:4] == [pd.Timestamp("2026-01-15 14:30"), pd.Timestamp("2026-01-15 14:30"),
                               pd.Timestamp("2026-01-15 14:30:00.25"), pd.Timestamp("2026-01-15")]
    assert stats.failed == 1 and "time zone" in stats.failures[0].reason


def test_explicit_utc_conversion():
    s = text("2026-01-15T14:30:00+02:00", "2026-01-15 12:30Z", "2026-01-15 12:30")
    new, stats, _ = convert_column(s, "datetime", {"date_formats": ["ymd"], "timezone": "to_utc"})
    assert str(new.dtype) == "datetime64[us, UTC]"
    assert values(new)[:2] == [pd.Timestamp("2026-01-15 12:30", tz="UTC")] * 2
    assert stats.shifted == 1 and stats.failed == 1 and "no time zone" in stats.failures[0].reason


def test_sub_microsecond_precision_is_refused():
    assert date("2026-01-15 10:00:00.123456789", "ymd")[0] is None
    assert date("2026-01-15 10:00:00.123456000", "ymd")[0].micro == 123456


def test_custom_formats():
    assert check_custom_format("%d.%m.%Y %H:%M") is None
    assert "not supported" in check_custom_format("%d %b %Y")
    assert "not supported" in check_custom_format("%d/%m/%y")
    assert "must contain" in check_custom_format("%H:%M")
    m = date("15.01.2026 14:30", "custom", custom="%d.%m.%Y %H:%M")[0]
    assert (m.day, m.hour, m.minute) == (15, 14, 30)
    assert date("2026-01-15", "custom", custom="%d.%m.%Y")[0] is None


def test_date_conversion_preserves_missing_and_counts_ambiguity():
    s = text("05/06/2026", "01/15/2026", "", None, "bad")
    new, stats, failed = convert_column(s, "date", {"date_formats": ["mdy"], "time_of_day": "reject"})
    assert values(new) == [dt.date(2026, 5, 6), dt.date(2026, 1, 15), None, None, None]
    assert (stats.existing_missing, stats.converted, stats.failed, stats.ambiguous) == (2, 2, 1, 1)


# ---- booleans ------------------------------------------------------

def test_boolean_mappings():
    parse = BooleanParser({"true_values": ["true", "yes", "y", "1"], "false_values": ["false", "no", "n", "0"]})
    for raw in ("true", "TRUE", " Yes ", "y", "1"):
        assert parse(raw)[0] is True
    for raw in ("false", "No", "N", "0"):
        assert parse(raw)[0] is False
    assert parse("maybe")[0] is None and "not in the true or false lists" in parse("maybe")[1]


def test_boolean_case_sensitive():
    parse = BooleanParser({"true_values": ["Y"], "false_values": ["N"], "case_sensitive": True})
    assert parse("Y")[0] is True and parse("y")[0] is None


def test_boolean_column_with_missing_values():
    s = text("active", "inactive", None, "", "other")
    new, stats, _ = convert_column(s, "boolean", {"true_values": ["active"], "false_values": ["inactive"]})
    assert str(new.dtype) == "boolean"
    assert values(new) == [True, False, None, None, None]
    assert (stats.converted, stats.failed, stats.existing_missing) == (2, 1, 2)


# ---- typed columns ------------------------------------------------

def test_column_kinds():
    assert column_kind(TEXT_DTYPE) == "text"
    assert column_kind(pd.Int64Dtype()) == "integer"
    assert column_kind(pd.Float64Dtype()) == "decimal"
    assert column_kind(pd.BooleanDtype()) == "boolean"
    assert column_kind(DATE_DTYPE) == "date"                      # Not "datetime"
    assert column_kind(np.dtype("datetime64[us]")) == "datetime"
    assert column_kind(pd.DatetimeTZDtype("us", "UTC")) == "datetime"
    assert column_kind(np.dtype(object)) == "other"


def test_canonical_text():
    assert canonical_text(1250, "integer") == "1250"
    assert canonical_text(1250.5, "decimal") == "1250.5"
    assert canonical_text(3.0, "decimal") == "3.0"
    assert canonical_text(True, "boolean") == "true"
    assert canonical_text(dt.date(2026, 1, 15), "date") == "2026-01-15"
    assert canonical_text(pd.Timestamp("2026-01-15 14:30"), "datetime") == "2026-01-15 14:30:00"


def test_typed_to_text_and_back_is_lossless():
    for series in (pd.Series(pd.array([1, None, -7], dtype="Int64")),
                   pd.Series(pd.array([0.1, None, 1e20], dtype="Float64")),
                   pd.Series(pd.array([True, None, False], dtype="boolean"))):
        as_text, _, _ = convert_column(series, "string", {})
        assert as_text.dtype == TEXT_DTYPE and as_text.isna().tolist() == [False, True, False]


def test_decimal_to_integer_uses_the_fraction_rule():
    s = pd.Series(pd.array([1.5, 2.0, None], dtype="Float64"))
    new, stats, _ = convert_column(s, "integer", {"fraction": "reject"})
    assert values(new) == [None, 2, None] and stats.failed == 1
    new, stats, _ = convert_column(s, "integer", {"fraction": "round_half_even"})
    assert values(new) == [2, 2, None] and stats.rounded == 1


def test_integer_to_decimal_flags_precision():
    new, stats, _ = convert_column(pd.Series(pd.array([1, 2 ** 53 + 1], dtype="Int64")), "decimal", {})
    assert stats.precision_loss == 1 and str(new.dtype) == "Float64"


def test_datetime_to_date_and_back():
    s = pd.Series(pd.to_datetime(["2026-01-15 10:00", None, "2026-01-16"], format="ISO8601").as_unit("us"))
    new, stats, _ = convert_column(s, "date", {"time_of_day": "reject"})
    assert values(new) == [None, None, dt.date(2026, 1, 16)] and stats.failed == 1
    new, _, _ = convert_column(s, "date", {"time_of_day": "drop"})
    back, _, _ = convert_column(new, "datetime", {"timezone": "reject"})
    assert values(back) == [pd.Timestamp("2026-01-15"), None, pd.Timestamp("2026-01-16")]


def test_fill_values_parse_with_the_column_type():
    assert parse_fill_value("0", pd.Int64Dtype()) == (0, None)
    assert parse_fill_value("2.5", pd.Int64Dtype())[0] is None
    assert parse_fill_value("2.5", pd.Float64Dtype()) == (2.5, None)
    assert parse_fill_value("true", pd.BooleanDtype()) == (True, None)
    assert parse_fill_value("yes", pd.BooleanDtype())[0] is None
    assert parse_fill_value("2026-01-15", DATE_DTYPE) == (dt.date(2026, 1, 15), None)
    assert parse_fill_value("2026-01-15 08:00", np.dtype("datetime64[us]"))[0] == pd.Timestamp("2026-01-15 08:00")
    assert parse_fill_value("2026-01-15 08:00Z", pd.DatetimeTZDtype("us", "UTC"))[0] == pd.Timestamp(
        "2026-01-15 08:00", tz="UTC")
