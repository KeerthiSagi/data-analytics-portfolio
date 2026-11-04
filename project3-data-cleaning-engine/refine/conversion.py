"""Explicit data type conversion: column kinds, parsing rules and typed arrays.

Nothing here changes a table. `convert_column` reads one column and
returns a new, typed column together with exact statistics about what
converted, what failed and why. The "Convert data type" operation
(refine/operations/convert_ops.py) decides what to do with failures.

Column kinds and the dtypes REFINE uses for them
-------------------------------------------------
    text      pd.StringDtype("pyarrow")    every loaded column
    integer   Int64                        nullable 64-bit integers
    decimal   Float64                      nullable binary floating point
    boolean   boolean                      nullable True / False
    date      date32[day][pyarrow]         calendar dates, no time of day
    datetime  datetime64[us]               date and time, no time zone
              datetime64[us, UTC]          date and time in UTC

Missing cells (pd.NA, and empty text, which REFINE counts as missing)
become the target dtype's missing value. Nothing is ever written as the
text "nan", "None" or "NaT".

Every parsing rule is opt-in and spelled out in the options, which are
frozen in the recipe, so a replay parses exactly the same way. Rules
work on a column's distinct values, so cost grows with the number of
distinct values rather than rows.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa

from refine.missing import missing_mask
from refine.source import TEXT_DTYPE

# ------------------------------------------------------------------
# Kinds and dtypes
# ------------------------------------------------------------------

KINDS = ("text", "integer", "decimal", "boolean", "date", "datetime")
TARGETS = ("string", "integer", "decimal", "boolean", "date", "datetime")
TARGET_LABELS = {
    "string": "Text", "integer": "Whole number (Int64)", "decimal": "Decimal number (Float64)",
    "boolean": "True / false (boolean)", "date": "Date", "datetime": "Date & time",
}
KIND_LABELS = {"text": "Text", "integer": "Whole number", "decimal": "Decimal number", "boolean": "True / false",
               "date": "Date", "datetime": "Date & time", "other": "Other"}
TARGET_KIND = {"string": "text", "integer": "integer", "decimal": "decimal", "boolean": "boolean",
               "date": "date", "datetime": "datetime"}

DATE_DTYPE = pd.ArrowDtype(pa.date32())
INT64_MIN, INT64_MAX = -(2 ** 63), 2 ** 63 - 1
MAX_NUMBER_LENGTH = 100
_EPOCH = dt.datetime(1970, 1, 1)
_US = dt.timedelta(microseconds=1)


def column_kind(dtype) -> str:
    """'text', 'integer', 'decimal', 'boolean', 'date', 'datetime' or 'other'."""
    if isinstance(dtype, pd.StringDtype):
        return "text"
    if isinstance(dtype, pd.ArrowDtype) and pa.types.is_date(dtype.pyarrow_dtype):
        return "date"          # Checked first: pandas also calls Arrow dates "datetime64-like"
    if pd.api.types.is_bool_dtype(dtype):
        return "boolean"
    if pd.api.types.is_integer_dtype(dtype):
        return "integer"
    if pd.api.types.is_float_dtype(dtype):
        return "decimal"
    if pd.api.types.is_datetime64_any_dtype(dtype):
        return "datetime"
    return "other"


def is_utc(dtype) -> bool:
    return isinstance(dtype, pd.DatetimeTZDtype)


def target_dtype(target: str, utc: bool = False):
    if target == "string":
        return TEXT_DTYPE
    if target == "integer":
        return pd.Int64Dtype()
    if target == "decimal":
        return pd.Float64Dtype()
    if target == "boolean":
        return pd.BooleanDtype()
    if target == "date":
        return DATE_DTYPE
    return pd.DatetimeTZDtype("us", "UTC") if utc else np.dtype("datetime64[us]")


def dtype_name(dtype) -> str:
    """A short, version-independent dtype name for messages and recipes."""
    from refine.fingerprint import dtype_label
    return dtype_label(dtype)


# ------------------------------------------------------------------
# Canonical text for typed values
# ------------------------------------------------------------------

def canonical_text(value: Any, kind: str) -> str:
    """How REFINE writes a typed value as text (non-missing values only).

    integer 1250, decimal 1250.5 (shortest exact form), boolean true/false,
    date 2026-01-15, datetime 2026-01-15 14:30:00[.ffffff][+00:00].
    """
    if kind == "integer":
        return str(int(value))
    if kind == "decimal":
        f = float(value)
        if f.is_integer() and abs(f) < 1e16:
            return f"{f:.1f}"
        return repr(f)
    if kind == "boolean":
        return "true" if bool(value) else "false"
    if kind == "date":
        return value.isoformat()
    if kind == "datetime":
        return pd.Timestamp(value).isoformat(sep=" ")
    return str(value)


def distinct_values(series: pd.Series) -> tuple[np.ndarray, list]:
    """(codes, distinct values) with -1 for missing; values as Python objects."""
    codes, uniques = pd.factorize(series, use_na_sentinel=True)
    return np.asarray(codes, dtype=np.int64), list(uniques)


# ------------------------------------------------------------------
# Options
# ------------------------------------------------------------------

THOUSANDS = (None, ",", ".", " ", "'")
DECIMALS = (".", ",")
PERCENT = ("reject", "fraction", "number")
FRACTION = ("reject", "round_half_even", "round_half_up")
DATE_FORMATS = ("ymd", "mdy", "dmy", "month_name", "custom")
DATE_FORMAT_LABELS = {
    "ymd": "Year first: 2026-01-15, 2026/01/15 (ISO 8601)",
    "mdy": "Month first: 01/15/2026",
    "dmy": "Day first: 15/01/2026",
    "month_name": "Month name: Jan 15, 2026 or 15 January 2026",
    "custom": "Custom format",
}
TIME_OF_DAY = ("reject", "drop")
TIMEZONE = ("reject", "to_utc")
CURRENCY_SYMBOLS = "$€£¥₹"
CUSTOM_DIRECTIVES = frozenset("YmdHMSfz%")

NUMERIC_DEFAULTS = {"thousands_separator": None, "decimal_separator": ".", "currency": False,
                    "parentheses_negative": False, "percent": "reject"}
BOOLEAN_PRESETS = {
    "true/false": (("true",), ("false",)),
    "yes/no": (("yes",), ("no",)),
    "y/n": (("y",), ("n",)),
    "t/f": (("t",), ("f",)),
    "1/0": (("1",), ("0",)),
}


def check_custom_format(fmt: str) -> str | None:
    """Problem with a custom date format, or None if it can be used."""
    if not fmt or len(fmt) > 50:
        return "Enter a custom format of at most 50 characters, for example %d.%m.%Y %H:%M."
    i = 0
    seen = set()
    while i < len(fmt):
        if fmt[i] == "%":
            if i + 1 >= len(fmt):
                return "The format ends with a lone '%'."
            d = fmt[i + 1]
            if d not in CUSTOM_DIRECTIVES:
                return (f"The directive %{d} is not supported. Use %Y (4-digit year), %m, %d, %H, %M, %S, %f "
                        "(microseconds) and %z (time zone offset). Month names and 2-digit years are not "
                        "supported in custom formats because their meaning depends on locale or century.")
            seen.add(d)
            i += 2
        else:
            i += 1
    if not {"Y", "m", "d"} <= seen:
        return "A custom format must contain %Y, %m and %d."
    return None


# ------------------------------------------------------------------
# Per-value parsers. Each returns (value or None, failure reason or None, flags).
# ------------------------------------------------------------------

_INF_WORDS = frozenset({"inf", "infinity", "nan", "-nan", "+nan", "∞"})
_SEP_CHARS = {",": "a comma", ".": "a period", " ": "a space", "'": "an apostrophe"}


def _num_regex(thousands: str | None, decimal: str) -> tuple[re.Pattern, re.Pattern | None]:
    d = re.escape(decimal)
    plain = re.compile(rf"(?:[0-9]+(?:{d}[0-9]*)?|{d}[0-9]+)(?:[eE][+-]?[0-9]+)?")
    grouped = None
    if thousands:
        t = re.escape(thousands)
        grouped = re.compile(rf"[0-9]{{1,3}}(?:{t}[0-9]{{3}})+(?:{d}[0-9]+)?")
    return plain, grouped


_AMBIGUOUS_SEPARATOR = re.compile(r"[0-9]{1,3}[.,][0-9]{3}")


class NumberParser:
    """Reads text as an exact Decimal under explicit, opt-in rules."""

    def __init__(self, options: Mapping[str, Any]):
        self.thousands = options.get("thousands_separator")
        self.decimal = options.get("decimal_separator", ".")
        self.currency = bool(options.get("currency", False))
        self.parentheses = bool(options.get("parentheses_negative", False))
        self.percent = options.get("percent", "reject")
        self.plain, self.grouped = _num_regex(self.thousands, self.decimal)

    def __call__(self, text: str) -> tuple[Decimal | None, str | None, frozenset]:
        flags: set[str] = set()
        s = text.strip()
        if not s:
            return None, "only whitespace (use Standardize missing values if it means no value)", frozenset()
        if len(s) > MAX_NUMBER_LENGTH:
            return None, f"longer than {MAX_NUMBER_LENGTH} characters", frozenset()
        if s.lower().lstrip("+-") in _INF_WORDS:
            return None, "infinity or NaN is not a finite number", frozenset()
        negative = False
        if s.startswith("(") and s.endswith(")"):
            if not self.parentheses:
                return None, "in parentheses (turn on parentheses as negative)", frozenset()
            negative, s = True, s[1:-1].strip()
            flags.add("parentheses")
        pct = False
        if s.endswith("%"):
            if self.percent == "reject":
                return None, "has a percent sign (choose how to read percentages)", frozenset()
            pct, s = True, s[:-1].rstrip()
            flags.add("percent")
        sign = ""
        if s[:1] in "+-" and s:
            sign, s = s[0], s[1:]
        symbol = 0
        if s[:1] and s[0] in CURRENCY_SYMBOLS:
            symbol += 1
            s = s[1:].lstrip(" ")
            if not sign and s[:1] in ("+", "-") and s:
                sign, s = s[0], s[1:]
        if s[-1:] and s[-1] in CURRENCY_SYMBOLS:
            symbol += 1
            s = s[:-1].rstrip(" ")
        if symbol:
            if not self.currency:
                return None, "has a currency symbol (turn on currency parsing)", frozenset()
            if symbol > 1:
                return None, "has more than one currency symbol", frozenset()
            if pct:
                return None, "has both a currency symbol and a percent sign", frozenset()
            flags.add("currency")
        if negative and sign:
            return None, "has both parentheses and a sign", frozenset()
        if not s:
            return None, "has no digits", frozenset()
        core = s
        if self.thousands == " ":
            core = core.replace(" ", " ").replace(" ", " ")
        if self.plain.fullmatch(core):
            digits = core.replace(self.decimal, ".") if self.decimal != "." else core
        elif self.grouped is not None and self.grouped.fullmatch(core):
            flags.add("thousands")
            digits = core.replace(self.thousands, "")
            if self.decimal != ".":
                digits = digits.replace(self.decimal, ".")
        else:
            return None, self._reason(core), frozenset()
        if _AMBIGUOUS_SEPARATOR.fullmatch(core):
            flags.add("ambiguous")
        try:
            number = Decimal(digits)
        except InvalidOperation:
            return None, "not a number", frozenset()
        if sign == "-" or negative:
            number = -number
        if pct and self.percent == "fraction":
            number = number.scaleb(-2)
        return number, None, frozenset(flags)

    def _reason(self, core: str) -> str:
        if self.thousands and self.thousands in core:
            return f"thousands separators ({_SEP_CHARS[self.thousands]}) are not in groups of three digits"
        for ch, name in _SEP_CHARS.items():
            if ch in core and ch != self.decimal and ch != self.thousands:
                if ch in ",." and self.thousands is None:
                    return f"contains {name} (turn on thousands-separator parsing if it groups thousands)"
                return f"contains {name}, which is not the chosen separator"
        if any(c in core for c in CURRENCY_SYMBOLS):
            return "has a currency symbol in an unexpected place"
        if re.search(r"[A-Za-z]", core):
            return "contains letters"
        return "not a number in the chosen format"


def decimal_to_int(number: Decimal, fraction: str) -> tuple[int | None, str | None, frozenset]:
    if not number.is_finite():
        return None, "not a finite number", frozenset()
    if number.adjusted() >= 19:
        return None, "too large for a 64-bit whole number", frozenset()
    flags: set[str] = set()
    if number != number.to_integral_value():
        if fraction == "reject":
            return None, "has a fractional part (choose a rounding rule, or convert to decimal)", frozenset()
        number = number.quantize(Decimal(1), rounding=ROUND_HALF_EVEN if fraction == "round_half_even"
                                 else ROUND_HALF_UP)
        flags.add("rounded")
    value = int(number)
    if not INT64_MIN <= value <= INT64_MAX:
        return None, "too large for a 64-bit whole number", frozenset()
    return value, None, frozenset(flags)


def decimal_to_float(number: Decimal) -> tuple[float | None, str | None, frozenset]:
    if not number.is_finite():
        return None, "not a finite number", frozenset()
    if number.adjusted() > 308:
        return None, "too large for a decimal number", frozenset()
    value = float(number)
    if not math.isfinite(value):
        return None, "too large for a decimal number", frozenset()
    # Lossless when the float is exactly the number, or reads back as it in shortest form ("0.1").
    exact = Decimal(value) == number or Decimal(repr(value)) == number
    flags = frozenset() if exact else frozenset({"precision"})
    return value, None, flags


class BooleanParser:
    def __init__(self, options: Mapping[str, Any]):
        self.case_sensitive = bool(options.get("case_sensitive", False))
        norm = self.normalize
        self.lookup = {**{norm(v): True for v in options["true_values"]},
                       **{norm(v): False for v in options["false_values"]}}

    def normalize(self, text: str) -> str:
        text = text.strip()
        return text if self.case_sensitive else text.casefold()

    def __call__(self, text: str) -> tuple[bool | None, str | None, frozenset]:
        if not text.strip():
            return None, "only whitespace (use Standardize missing values if it means no value)", frozenset()
        value = self.lookup.get(self.normalize(text))
        if value is None:
            return None, "not in the true or false lists", frozenset()
        return value, None, frozenset()


# Dates ------------------------------------------------------------

_TIME = (r"(?:(?:T|\s+)(?P<H>[0-9]{1,2}):(?P<M>[0-9]{2})(?::(?P<S>[0-9]{2})(?:[.,](?P<F>[0-9]{1,9}))?)?"
         r"(?:\s*(?P<tz>Z|UTC|GMT|[+-][0-9]{2}(?::?[0-9]{2})?))?)?")
_YMD = re.compile(rf"(?P<y>[0-9]{{4}})(?P<sep>[-/.])(?P<m>[0-9]{{1,2}})(?P=sep)(?P<d>[0-9]{{1,2}}){_TIME}")
_ABY = re.compile(rf"(?P<a>[0-9]{{1,2}})(?P<sep>[-/.])(?P<b>[0-9]{{1,2}})(?P=sep)(?P<y>[0-9]{{4}}){_TIME}")
_MON_FIRST = re.compile(rf"(?P<mon>[A-Za-z]{{3,9}})\.?\s+(?P<d>[0-9]{{1,2}})(?:st|nd|rd|th)?,?\s+(?P<y>[0-9]{{4}}){_TIME}")
_DAY_FIRST = re.compile(rf"(?P<d>[0-9]{{1,2}})(?:st|nd|rd|th)?\s+(?P<mon>[A-Za-z]{{3,9}})\.?,?\s+(?P<y>[0-9]{{4}}){_TIME}")
_TWO_DIGIT_YEAR = re.compile(r"[0-9]{1,2}[-/.][0-9]{1,2}[-/.][0-9]{2}(?:\s.*)?")
_AMPM = re.compile(r"[0-9]\s*[AaPp]\.?[Mm]\.?$")
_MONTHS = {name: i for i, names in enumerate([
    ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"), ("may",), ("jun", "june"),
    ("jul", "july"), ("aug", "august"), ("sep", "sept", "september"), ("oct", "october"),
    ("nov", "november"), ("dec", "december")], start=1) for name in names}


@dataclass(frozen=True)
class _Moment:
    year: int
    month: int
    day: int
    hour: int = 0
    minute: int = 0
    second: int = 0
    micro: int = 0
    has_time: bool = False
    offset: int | None = None        # Minutes east of UTC; None: no time zone given


def _offset_minutes(tz: str | None) -> int | None:
    if tz is None:
        return None
    if tz.upper() in ("Z", "UTC", "GMT"):
        return 0
    sign = -1 if tz[0] == "-" else 1
    body = tz[1:].replace(":", "")
    hours = int(body[:2])
    minutes = int(body[2:4]) if len(body) > 2 else 0
    return sign * (hours * 60 + minutes)


def _moment(y, m, d, g) -> tuple[_Moment | None, str | None]:
    if g.get("H") is None:
        return _Moment(y, m, d), None
    fraction = g.get("F") or ""
    if len(fraction) > 6 and fraction[6:].strip("0"):
        return None, "has more precision than microseconds"
    micro = int((fraction + "000000")[:6]) if fraction else 0
    offset = _offset_minutes(g.get("tz"))
    if offset is not None and abs(offset) >= 24 * 60:
        return None, "has an invalid time zone offset"
    return _Moment(y, m, d, int(g["H"]), int(g["M"]), int(g.get("S") or 0), micro, True, offset), None


class DateParser:
    """Reads text as a date and optional time in explicitly chosen formats."""

    def __init__(self, options: Mapping[str, Any]):
        self.formats = tuple(options.get("date_formats") or ())
        self.custom = options.get("custom_format")

    def __call__(self, text: str) -> tuple[_Moment | None, str | None, frozenset]:
        s = text.strip()
        if not s:
            return None, "only whitespace (use Standardize missing values if it means no value)", frozenset()
        if _AMPM.search(s):
            return None, "uses AM/PM, which is not supported (use a 24-hour time)", frozenset()
        if "custom" in self.formats:
            return self._custom(s)
        for fmt in self.formats:
            result = self._try(fmt, s)
            if result is not None:
                return result
        return None, self._reason(s), frozenset()

    def _try(self, fmt, s):
        if fmt == "ymd":
            m = _YMD.fullmatch(s)
            if m:
                return self._finish(int(m["y"]), int(m["m"]), int(m["d"]), m.groupdict(), {"ymd"})
        elif fmt in ("mdy", "dmy"):
            m = _ABY.fullmatch(s)
            if m:
                a, b = int(m["a"]), int(m["b"])
                flags = {fmt}
                if a <= 12 and b <= 12 and a != b:
                    flags.add("ambiguous")
                month, day = (a, b) if fmt == "mdy" else (b, a)
                return self._finish(int(m["y"]), month, day, m.groupdict(), flags)
        elif fmt == "month_name":
            for pattern in (_MON_FIRST, _DAY_FIRST):
                m = pattern.fullmatch(s)
                if m:
                    month = _MONTHS.get(m["mon"].lower())
                    if month is None:
                        return None, f"{m['mon']!r} is not an English month name", frozenset()
                    return self._finish(int(m["y"]), month, int(m["d"]), m.groupdict(), {"month_name"})
        return None

    def _finish(self, y, m, d, groups, flags):
        moment, reason = _moment(y, m, d, groups)
        if moment is None:
            return None, reason, frozenset()
        return _check(moment, frozenset(flags))

    def _custom(self, s):
        try:
            parsed = dt.datetime.strptime(s, self.custom)
        except ValueError:
            return None, "does not match the custom format (or is not a real date)", frozenset()
        offset = None
        if parsed.tzinfo is not None:
            delta = parsed.utcoffset()
            offset = int(delta.total_seconds() // 60)
        has_time = any(f"%{d}" in self.custom for d in "HMSf")
        moment = _Moment(parsed.year, parsed.month, parsed.day, parsed.hour, parsed.minute, parsed.second,
                         parsed.microsecond, has_time, offset)
        return moment, None, frozenset({"custom"})

    def _reason(self, s: str) -> str:
        if _TWO_DIGIT_YEAR.fullmatch(s):
            return "has a 2-digit year, which is not supported (the century would be a guess)"
        if _YMD.fullmatch(s):
            return "is year-first (choose the year-first format)"
        if _ABY.fullmatch(s):
            if "mdy" in self.formats or "dmy" in self.formats:
                return "is not a real calendar date in the chosen order"
            return "is month/day or day/month (choose which order the data uses)"
        if _MON_FIRST.fullmatch(s) or _DAY_FIRST.fullmatch(s):
            return "uses a month name (choose the month-name format)"
        return "not a date in the chosen formats"


def _check(moment: _Moment, flags: frozenset):
    try:
        dt.date(moment.year, moment.month, moment.day)
    except ValueError:
        return None, "is not a real calendar date", frozenset()
    if not (moment.hour < 24 and moment.minute < 60 and moment.second < 60):
        return None, "is not a valid time of day", frozenset()
    return moment, None, flags


def moment_to_date(m: _Moment, time_of_day: str) -> tuple[dt.date | None, str | None, frozenset]:
    if m.offset is not None:
        return None, "has a time zone (convert to date & time in UTC instead)", frozenset()
    flags: frozenset = frozenset()
    if (m.hour, m.minute, m.second, m.micro) != (0, 0, 0, 0):
        if time_of_day == "reject":
            return None, "has a time of day (choose to drop the time, or convert to date & time)", frozenset()
        flags = frozenset({"time_dropped"})
    return dt.date(m.year, m.month, m.day), None, flags


def moment_to_micros(m: _Moment, timezone: str) -> tuple[int | None, str | None, frozenset]:
    naive = dt.datetime(m.year, m.month, m.day, m.hour, m.minute, m.second, m.micro)
    if timezone == "reject":
        if m.offset is not None:
            return None, "has a time zone (choose Convert to UTC to keep these values)", frozenset()
        return (naive - _EPOCH) // _US, None, frozenset()
    if m.offset is None:
        return None, "has no time zone (with Convert to UTC every value must include one)", frozenset()
    utc = naive - dt.timedelta(minutes=m.offset)
    flags = frozenset({"shifted"}) if m.offset else frozenset()
    return (utc - _EPOCH) // _US, None, flags


# ------------------------------------------------------------------
# Column conversion
# ------------------------------------------------------------------

@dataclass(frozen=True)
class ValueExample:
    before: str
    after: str | None            # Canonical text of the converted value; None when it failed
    count: int
    reason: str | None = None


@dataclass(frozen=True)
class ConversionStats:
    column: str
    source_dtype: str
    target_dtype: str
    rows: int
    existing_missing: int          # pd.NA or empty text before conversion
    converted: int                 # Non-missing cells converted to a value
    failed: int                    # Non-missing cells that could not be converted
    ambiguous: int                 # Converted, but another reading was possible (see flags)
    precision_loss: int            # Converted to a float that is not exactly the written number
    rounded: int                   # Fractional values rounded to whole numbers
    time_dropped: int              # Date & time values reduced to their date
    shifted: int                   # Values moved from their own time zone offset to UTC
    trimmed: int                   # Values read after removing surrounding whitespace
    failure_reasons: dict[str, int] = field(default_factory=dict)
    failures: tuple[ValueExample, ...] = ()
    examples: tuple[ValueExample, ...] = ()
    distinct_failed: int = 0

    @property
    def present(self) -> int:
        return self.rows - self.existing_missing

    @property
    def failed_pct(self) -> float:
        return 100.0 * self.failed / self.present if self.present else 0.0

    def counts(self) -> dict[str, int]:
        return {k: getattr(self, k) for k in ("rows", "existing_missing", "converted", "failed", "ambiguous",
                                              "precision_loss", "rounded", "time_dropped", "shifted", "trimmed")}


def _value_parser(target: str, options: Mapping[str, Any]):
    """A function text -> (typed value or None, reason, flags) for the target."""
    if target in ("integer", "decimal"):
        numbers = NumberParser(options)
        fraction = options.get("fraction", "reject")

        def parse_number(text):
            number, reason, flags = numbers(text)
            if number is None:
                return None, reason, flags
            value, reason, more = (decimal_to_int(number, fraction) if target == "integer"
                                   else decimal_to_float(number))
            return value, reason, flags | more
        return parse_number
    if target == "boolean":
        return BooleanParser(options)
    if target in ("date", "datetime"):
        dates = DateParser(options)
        time_of_day = options.get("time_of_day", "reject")
        timezone = options.get("timezone", "reject")

        def parse_date(text):
            moment, reason, flags = dates(text)
            if moment is None:
                return None, reason, flags
            value, reason, more = (moment_to_date(moment, time_of_day) if target == "date"
                                   else moment_to_micros(moment, timezone))
            return value, reason, flags | more
        return parse_date
    raise ValueError(f"Unknown target {target!r}")


def typed_source_options(source_kind: str, target: str, options: Mapping[str, Any]) -> dict[str, Any]:
    """Parsing options for a typed column: its values are read from their canonical text."""
    out = dict(options)
    if target in ("integer", "decimal"):
        out.update(NUMERIC_DEFAULTS)
    if target in ("date", "datetime"):
        out["date_formats"] = ["ymd"]
        out["custom_format"] = None
    return out


def convert_column(series: pd.Series, target: str, options: Mapping[str, Any], *,
                   example_limit: int = 8) -> tuple[pd.Series, ConversionStats, np.ndarray]:
    """Convert one column. Returns (new column, statistics, mask of failed cells).

    Failed cells are missing in the new column; the caller decides whether
    that is allowed (coerce) or the step is refused (strict). The input is
    never modified.
    """
    kind = column_kind(series.dtype)
    utc = target == "datetime" and options.get("timezone") == "to_utc"
    out_dtype = target_dtype(target, utc)
    missing = missing_mask(series)
    codes, uniques = distinct_values(series)
    rows = len(series)
    if kind == "text":
        texts = list(uniques)
        parse_opts = options
    else:
        texts = [canonical_text(v, kind) for v in uniques]
        parse_opts = typed_source_options(kind, target, options)
    n = len(uniques)
    freq = np.bincount(codes[codes >= 0], minlength=n) if n else np.zeros(0, dtype=np.int64)
    # Empty text is missing: it converts to no value and is not a failure.
    is_empty = np.array([t == "" for t in texts], dtype=bool) if kind == "text" else np.zeros(n, dtype=bool)

    if target == "string":
        values = texts
        ok = ~is_empty
        reasons: list[str | None] = [None] * n
        flags: list[frozenset] = [frozenset()] * n
    else:
        parse = _value_parser(target, parse_opts)
        values, reasons, flags = [], [], []
        for t, empty in zip(texts, is_empty):
            if empty:
                values.append(None), reasons.append(None), flags.append(frozenset())
                continue
            v, r, f = parse(t)
            if v is not None and kind == "text" and t != t.strip():
                f = f | {"trimmed"}
            values.append(v), reasons.append(r), flags.append(f)
        ok = np.array([v is not None for v in values], dtype=bool)
    failed_u = ~ok & ~is_empty
    new = _build(values, ok, codes, target, out_dtype, series.name)

    def weighted(mask) -> int:
        return int(freq[mask].sum()) if n else 0

    def flagged(name) -> int:
        return weighted(np.array([name in f for f in flags], dtype=bool)) if n else 0

    reason_counts: dict[str, int] = {}
    for i in np.flatnonzero(failed_u):
        reason_counts[reasons[i]] = reason_counts.get(reasons[i], 0) + int(freq[i])
    order = np.argsort(-freq, kind="stable") if n else np.zeros(0, dtype=np.int64)
    failures = tuple(ValueExample(texts[i], None, int(freq[i]), reasons[i]) for i in order if failed_u[i])[:example_limit]
    shown_kind = TARGET_KIND[target]
    examples = []
    # Values whose text changes first (they show what the rules did), then the others.
    for changed_first in (True, False):
        for i in order:
            if len(examples) >= example_limit:
                break
            if not ok[i]:
                continue
            after = (texts[i] if target == "string" else
                     _micros_text(values[i], utc) if target == "datetime" else canonical_text(values[i], shown_kind))
            if (after != texts[i]) == changed_first:
                examples.append(ValueExample(texts[i], after, int(freq[i])))
    stats = ConversionStats(
        column=str(series.name), source_dtype=dtype_name(series.dtype), target_dtype=dtype_name(out_dtype),
        rows=rows, existing_missing=int(missing.sum()), converted=weighted(ok), failed=weighted(failed_u),
        ambiguous=flagged("ambiguous"), precision_loss=flagged("precision"), rounded=flagged("rounded"),
        time_dropped=flagged("time_dropped"), shifted=flagged("shifted"), trimmed=flagged("trimmed"),
        failure_reasons=dict(sorted(reason_counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        failures=failures, examples=tuple(examples), distinct_failed=int(failed_u.sum()),
    )
    failed_rows = np.zeros(rows, dtype=bool)
    if n:
        present = codes >= 0
        failed_rows[present] = failed_u[codes[present]]
    return new, stats, failed_rows


def _micros_text(micros: int, utc: bool) -> str:
    value = _EPOCH + dt.timedelta(microseconds=int(micros))
    text = value.isoformat(sep=" ")
    return text + "+00:00" if utc else text


def _build(values: Sequence, ok: np.ndarray, codes: np.ndarray, target: str, out_dtype, name) -> pd.Series:
    """Typed column from per-distinct values (None or failed: missing) and row codes."""
    n = len(values)
    take = np.where(codes >= 0, codes, 0) if n else np.zeros(len(codes), dtype=np.int64)
    row_ok = (codes >= 0) & (ok[take] if n else np.zeros(len(codes), dtype=bool))
    mask = ~row_ok
    if target == "string":
        uniq = pd.array([v if good else None for v, good in zip(values, ok)], dtype=TEXT_DTYPE)
        arr = uniq.take(np.where(row_ok, codes, -1), allow_fill=True) if n else pd.array([None] * len(codes),
                                                                                          dtype=TEXT_DTYPE)
        return pd.Series(arr, name=name)
    if target == "integer":
        u = np.array([v if good else 0 for v, good in zip(values, ok)], dtype=np.int64) if n else np.zeros(1, np.int64)
        return pd.Series(pd.arrays.IntegerArray(u[take], mask.copy()), name=name)
    if target == "decimal":
        u = np.array([v if good else 0.0 for v, good in zip(values, ok)], dtype=np.float64) if n else np.zeros(1)
        return pd.Series(pd.arrays.FloatingArray(u[take], mask.copy()), name=name)
    if target == "boolean":
        u = np.array([bool(v) if good else False for v, good in zip(values, ok)], dtype=bool) if n else np.zeros(1, bool)
        return pd.Series(pd.arrays.BooleanArray(u[take], mask.copy()), name=name)
    if target == "date":
        u = np.array([(v - dt.date(1970, 1, 1)).days if good else 0 for v, good in zip(values, ok)],
                     dtype=np.int32) if n else np.zeros(1, np.int32)
        arrow = pa.array(u[take], type=pa.int32(), mask=mask).cast(pa.date32())
        return pd.Series(pd.arrays.ArrowExtensionArray(arrow), name=name)
    u = np.array([v if good else 0 for v, good in zip(values, ok)], dtype=np.int64) if n else np.zeros(1, np.int64)
    micros = u[take].copy()
    micros[mask] = np.iinfo(np.int64).min          # NaT
    out = pd.Series(micros.view("M8[us]"), name=name)
    if isinstance(out_dtype, pd.DatetimeTZDtype):
        out = out.dt.tz_localize("UTC")
    return out


# ------------------------------------------------------------------
# Parsing typed fill values (used by Fill missing values on typed columns)
# ------------------------------------------------------------------

def parse_fill_value(text: str, dtype) -> tuple[Any, str | None]:
    """A typed scalar for a column's dtype, from canonical text, or (None, reason)."""
    kind = column_kind(dtype)
    if kind == "text":
        return text, None
    if kind in ("integer", "decimal"):
        number, reason, _ = NumberParser(NUMERIC_DEFAULTS)(text)
        if number is None:
            return None, f"{text!r} is not a plain number ({reason})"
        if kind == "integer":
            value, reason, _ = decimal_to_int(number, "reject")
        else:
            value, reason, _ = decimal_to_float(number)
        return (value, None) if value is not None else (None, f"{text!r} {reason}")
    if kind == "boolean":
        value = {"true": True, "false": False}.get(text.strip().lower())
        return (value, None) if value is not None else (None, f"{text!r} is not true or false")
    if kind == "date":
        moment, reason, _ = DateParser({"date_formats": ["ymd"]})(text)
        if moment is None:
            return None, f"{text!r} is not a YYYY-MM-DD date ({reason})"
        value, reason, _ = moment_to_date(moment, "reject")
        return (value, None) if value is not None else (None, f"{text!r} {reason}")
    if kind == "datetime":
        moment, reason, _ = DateParser({"date_formats": ["ymd"]})(text)
        if moment is None:
            return None, f"{text!r} is not a YYYY-MM-DD HH:MM:SS date and time ({reason})"
        utc = is_utc(dtype)
        micros, reason, _ = moment_to_micros(moment, "to_utc" if utc else "reject")
        if micros is None:
            return None, f"{text!r} {reason}"
        ts = pd.Timestamp(_EPOCH + dt.timedelta(microseconds=int(micros)))
        return (ts.tz_localize("UTC") if utc else ts), None
    return None, f"columns stored as {dtype} cannot be filled"


__all__ = [
    "BOOLEAN_PRESETS", "CURRENCY_SYMBOLS", "DATE_DTYPE", "DATE_FORMATS", "DATE_FORMAT_LABELS", "DECIMALS",
    "FRACTION", "KINDS", "KIND_LABELS", "NUMERIC_DEFAULTS", "PERCENT", "TARGETS", "TARGET_KIND", "TARGET_LABELS",
    "THOUSANDS", "TIMEZONE", "TIME_OF_DAY", "BooleanParser", "ConversionStats", "DateParser", "NumberParser",
    "ValueExample", "canonical_text", "check_custom_format", "column_kind", "convert_column", "decimal_to_float",
    "decimal_to_int", "distinct_values", "dtype_name", "is_utc", "parse_fill_value", "target_dtype",
]
