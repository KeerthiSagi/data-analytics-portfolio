"""Rule-based data quality checks. Pure Python and pandas; never imports Streamlit.

Entry point:

    assess_quality(frame, profile, rules=()) -> QualityReport

Principles
----------
* Checks only report. Nothing is fixed, converted or removed.
* Every issue states its count AND what it is counted out of (the
  denominator), so "12" is never shown without "of 500 rows".
* There is no overall quality score. A single number would hide which
  problems matter for the user's purpose and would suggest a precision
  rule-based checks do not have.
* A check that finds nothing is reported as "none detected", never as
  "clean". A check that cannot run reliably is "not assessed", with the
  reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd

from refine.notes import plural, quote, verb
from refine.profile import (
    MIN_VALUES_FOR_TYPE,
    NULL_TOKENS,
    SEMANTIC_LABELS,
    ColumnProfile,
    DatasetProfile,
)
from refine.source import TEXT_DTYPE

Severity = Literal["high", "medium", "low"]
Status = Literal["issues_found", "none_detected", "not_assessed"]
RuleKind = Literal["range", "allowed_values", "pattern"]

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}
SEVERITY_LABELS = {"high": "High", "medium": "Medium", "low": "Low"}

CAVEAT = (
    "These checks look for specific, common problems. A check that finds nothing does not "
    "mean the data is correct: values can be wrong in ways no rule here can detect, such as "
    "a plausible but mistyped number."
)

# Categories, in the order the page lists them.
CATEGORIES = (
    "Missing values", "Null-like text", "Duplicate rows", "Empty column", "Whitespace",
    "Capitalization", "Numeric type", "Date type", "Validity rule",
)

# Text checks (whitespace, capitalization) do not apply to these types.
_NO_CASE_TYPES = {"integer", "decimal", "date", "datetime", "empty", "unknown"}


# ------------------------------------------------------------------
# Models
# ------------------------------------------------------------------

@dataclass(frozen=True)
class QualityIssue:
    check: str                     # Stable identifier, e.g. "missing_values"
    category: str
    column: str | None             # None: the issue concerns the whole dataset
    severity: Severity
    affected_count: int
    denominator: int
    denominator_label: str         # What the denominator counts: "rows", "values checked", ...
    explanation: str
    suggested_action: str | None = None
    examples: tuple[str, ...] = ()
    position: int = 0              # Column position (0 for dataset-level), for ordering

    @property
    def scope(self) -> str:
        return self.column if self.column is not None else "Dataset"

    @property
    def percentage(self) -> float | None:
        """Share of the denominator, or None when there is nothing to divide by."""
        if self.denominator <= 0:
            return None
        return 100.0 * self.affected_count / self.denominator


@dataclass(frozen=True)
class CheckOutcome:
    """What one check concluded for one scope (a column or the dataset)."""

    check: str
    category: str
    column: str | None
    status: Status
    detail: str


@dataclass(frozen=True)
class ValidityRule:
    """A rule the user (or the sample's documentation) states explicitly.

    * range: values that parse as plain numbers must lie in [minimum, maximum]
      (either bound may be None).
    * allowed_values: values checked must be one of `allowed`.
    * pattern: values checked must fully match `pattern` (RE2 syntax, which
      runs in linear time, so a user pattern cannot hang the app).
    """

    column: str
    kind: RuleKind
    minimum: float | None = None
    maximum: float | None = None
    allowed: tuple[str, ...] = ()
    case_sensitive: bool = True
    pattern: str | None = None
    description: str | None = None

    @property
    def summary(self) -> str:
        if self.description:
            return self.description
        if self.kind == "range":
            if self.minimum is not None and self.maximum is not None:
                return f"{self.column} between {_num(self.minimum)} and {_num(self.maximum)}"
            if self.minimum is not None:
                return f"{self.column} at least {_num(self.minimum)}"
            return f"{self.column} at most {_num(self.maximum)}"
        if self.kind == "allowed_values":
            shown = ", ".join(quote(v) for v in self.allowed[:6])
            more = f" and {len(self.allowed) - 6} more" if len(self.allowed) > 6 else ""
            case = "" if self.case_sensitive else " (ignoring capitalization)"
            return f"{self.column} is one of {shown}{more}{case}"
        return f"{self.column} matches {self.pattern}"


@dataclass(frozen=True)
class QualityReport:
    issues: tuple[QualityIssue, ...]
    outcomes: tuple[CheckOutcome, ...]
    rules: tuple[ValidityRule, ...]

    def severity_counts(self) -> dict[str, int]:
        counts = {s: 0 for s in SEVERITY_ORDER}
        for issue in self.issues:
            counts[issue.severity] += 1
        return counts

    @property
    def not_assessed(self) -> tuple[CheckOutcome, ...]:
        return tuple(o for o in self.outcomes if o.status == "not_assessed")

    @property
    def none_detected(self) -> tuple[CheckOutcome, ...]:
        return tuple(o for o in self.outcomes if o.status == "none_detected")

    def for_column(self, name: str) -> tuple[QualityIssue, ...]:
        return tuple(i for i in self.issues if i.column == name)


def _num(x: float) -> str:
    return f"{x:,.0f}" if float(x).is_integer() else f"{x:,}"


def _fmt_pct(part: int, whole: int) -> str:
    return f"{100 * part / whole:.1f}%" if whole else "n/a"


def _shown(value: str) -> str:
    """A value as it should appear in a message: quoted, whitespace visible."""
    if value == "":
        return "(empty text)"
    return '"' + value.replace("\t", "\\t").replace("\n", "\\n") + '"'


def _by_share(part: int, whole: int, high: float, medium: float) -> Severity:
    share = part / whole if whole else 0.0
    if share >= high:
        return "high"
    if share >= medium:
        return "medium"
    return "low"


# ------------------------------------------------------------------
# Checks
# ------------------------------------------------------------------

class _Collector:
    def __init__(self) -> None:
        self.issues: list[QualityIssue] = []
        self.outcomes: list[CheckOutcome] = []

    def issue(self, issue: QualityIssue) -> None:
        self.issues.append(issue)

    def outcome(self, check: str, category: str, column: str | None, status: Status, detail: str) -> None:
        self.outcomes.append(CheckOutcome(check, category, column, status, detail))

    def found_or_none(self, check: str, category: str, column: str | None, found: bool, none_detail: str) -> None:
        if found:
            self.outcome(check, category, column, "issues_found", "See the issue list.")
        else:
            self.outcome(check, category, column, "none_detected", none_detail)


def _check_duplicates(p: DatasetProfile, out: _Collector) -> None:
    cat = "Duplicate rows"
    if p.duplicate_rows is None:
        out.outcome("duplicate_rows", cat, None, "not_assessed", p.duplicate_error or "Could not compare rows.")
        return
    if p.duplicate_rows:
        rows = ", ".join(f"{i + 1:,}" for i in p.duplicate_examples[:5])
        out.issue(QualityIssue(
            "duplicate_rows", cat, None,
            _by_share(p.duplicate_rows, p.n_rows, 0.10, 0.01),
            p.duplicate_rows, p.n_rows, "rows",
            f"{plural(p.duplicate_rows, 'row')} exactly {verb(p.duplicate_rows, 'repeats', 'repeat')} an "
            f"earlier row in every column ({plural(p.duplicate_groups or 0, 'distinct row')} "
            f"{verb(p.duplicate_groups or 0, 'occurs', 'occur')} more than once). The first "
            "occurrence is not counted. Comparison is exact: values differing only in spacing or "
            "capitalization are not treated as duplicates.",
            "Confirm the repeats are not legitimate (for example two identical orders), then keep "
            "one copy of each.",
            examples=(f"Repeated rows (table positions, from 1): {rows}",),
        ))
    out.found_or_none("duplicate_rows", cat, None, bool(p.duplicate_rows),
                      "No row exactly repeats another row.")


def _check_column(c: ColumnProfile, out: _Collector) -> None:
    n = c.n_rows
    pos = c.position

    # Empty column. Reported instead of missing values for the same column.
    if c.is_empty:
        blank = f" and {plural(c.whitespace_only_count, 'whitespace-only value')}" if c.whitespace_only_count else ""
        out.issue(QualityIssue(
            "empty_column", "Empty column", c.name, "medium", n, n, "rows",
            f"Every cell is missing{blank}: the column holds no data. Columns without a header in "
            "the file are named column_<position> when loaded.",
            "Check whether the column is needed. If it is, find the source of its values; if not, "
            "it can be removed in a later cleaning step.",
            position=pos,
        ))
        out.outcome("empty_column", "Empty column", c.name, "issues_found", "See the issue list.")
        return
    out.outcome("empty_column", "Empty column", c.name, "none_detected", "The column holds values.")

    # Missing values
    if c.missing_count:
        parts = []
        if c.null_count:
            parts.append(f"{c.null_count:,} with no value")
        if c.empty_text_count:
            parts.append(f"{c.empty_text_count:,} empty text")
        out.issue(QualityIssue(
            "missing_values", "Missing values", c.name,
            _by_share(c.missing_count, n, 0.50, 0.20),
            c.missing_count, n, "rows",
            f"{plural(c.missing_count, 'cell')} {'is' if c.missing_count == 1 else 'are'} missing "
            f"({', '.join(parts)}). Null-like text and whitespace-only values are counted separately.",
            "Decide whether missing values are acceptable for this column. If not, recover them "
            "from the source or exclude the affected rows from analyses that need them.",
            position=pos,
        ))
    out.found_or_none("missing_values", "Missing values", c.name, bool(c.missing_count),
                      "No missing cells.")

    if not c.text_values:
        return   # Confirmed non-text storage: text checks do not apply.

    # Null-like tokens
    if c.null_token_count:
        out.issue(QualityIssue(
            "null_tokens", "Null-like text", c.name, "medium", c.null_token_count, n, "rows",
            f"{plural(c.null_token_count, 'cell')} {verb(c.null_token_count, 'holds', 'hold')} text that often stands for a missing value. "
            "They are text in the data, not missing values, until you decide otherwise.",
            "Confirm these markers mean 'no value' in this column, then treat them as missing.",
            examples=tuple(f"{_shown(t.value)} × {t.count:,}" for t in c.null_tokens),
            position=pos,
        ))
    out.found_or_none("null_tokens", "Null-like text", c.name, bool(c.null_token_count),
                      f"No values match the null-like list ({', '.join(NULL_TOKENS)}).")

    # Whitespace
    non_missing = c.non_missing_count
    if c.whitespace_only_count:
        out.issue(QualityIssue(
            "whitespace_only", "Whitespace", c.name, "medium", c.whitespace_only_count,
            non_missing, "non-missing values",
            f"{plural(c.whitespace_only_count, 'value')} {verb(c.whitespace_only_count, 'contains', 'contain')} only spaces, tabs or line breaks. "
            "They look empty but are not counted as missing.",
            "Decide whether these mean 'no value'; if so, treat them as missing.",
            position=pos,
        ))
    edge = c.edge_whitespace_count
    if edge:
        out.issue(QualityIssue(
            "edge_whitespace", "Whitespace", c.name, "low", edge, non_missing, "non-missing values",
            f"{plural(edge, 'value')} {verb(edge, 'starts or ends', 'start or end')} with whitespace "
            f"({c.leading_whitespace_count:,} leading, {c.trailing_whitespace_count:,} trailing; a "
            "value can have both). \"India \" and \"India\" are then different values when grouping "
            "or matching.",
            "Trim leading and trailing whitespace, unless the spacing is meaningful.",
            examples=_whitespace_examples(c, edge=True),
            position=pos,
        ))
    if c.repeated_space_count:
        out.issue(QualityIssue(
            "repeated_spaces", "Whitespace", c.name, "low", c.repeated_space_count, non_missing,
            "non-missing values",
            f"{plural(c.repeated_space_count, 'value')} {verb(c.repeated_space_count, 'contains', 'contain')} "
            "two or more spaces in a row inside the text.",
            "Collapse repeated spaces to one, unless the spacing is meaningful.",
            examples=_whitespace_examples(c, edge=False),
            position=pos,
        ))
    out.found_or_none(
        "whitespace", "Whitespace", c.name,
        bool(c.whitespace_only_count or edge or c.repeated_space_count),
        "No whitespace-only values, edge whitespace or repeated spaces.",
    )

    # Capitalization
    if c.inference.semantic_type not in _NO_CASE_TYPES and c.inference.dominant_type not in _NO_CASE_TYPES:
        _check_capitalization(c, out)

    # Numeric and date types
    _check_numeric(c, out)
    _check_dates(c, out)


def _whitespace_examples(c: ColumnProfile, edge: bool) -> tuple[str, ...]:
    def matches(v: str) -> bool:
        return (v != v.strip()) if edge else ("  " in v.strip())
    return tuple(f"{_shown(v.value)} × {v.count:,}" for v in c.whitespace_examples if matches(v.value))


def _check_capitalization(c: ColumnProfile, out: _Collector) -> None:
    cat = "Capitalization"
    styled = sum(c.case_styles.values())
    letters = any(c.case_styles.values()) or c.case_variant_group_count
    if not letters:
        return   # No letters: the check does not apply.
    found = False
    if c.case_variant_cells:
        found = True
        examples = tuple(
            " / ".join(f"{_shown(v.value)} × {v.count:,}" for v in g.variants[:4])
            for g in c.case_variant_groups[:5]
        )
        out.issue(QualityIssue(
            "case_variants", cat, c.name, "medium", c.case_variant_cells, c.values_checked,
            "values checked",
            f"{plural(c.case_variant_group_count, 'value')} {'is' if c.case_variant_group_count == 1 else 'are'} "
            f"written with different capitalization. {plural(c.case_variant_cells, 'cell')} "
            f"{verb(c.case_variant_cells, 'differs', 'differ')} from "
            "the most common spelling of their value, so the same thing is counted as different values.",
            "Pick one spelling for each value (usually the most common) and standardize the others.",
            examples=examples,
            position=c.position,
        ))
    if styled:
        dominant, dominant_n = max(c.case_styles.items(), key=lambda kv: kv[1])
        minority = styled - dominant_n
        if minority and dominant_n / styled >= 0.6:
            found = True
            others = ", ".join(f"{n:,} {s}" for s, n in c.case_styles.items() if s != dominant and n)
            out.issue(QualityIssue(
                "case_style_mix", cat, c.name, "low", minority, styled, "values with a clear case style",
                f"Most values are {dominant} case ({dominant_n:,}), but {others} case values also "
                "appear. This can be intentional (for example acronyms such as \"USA\"); values "
                "in mixed case such as \"McDonald\" are not counted.",
                f"Check whether the other styles are intended; if not, convert them to {dominant} case.",
                position=c.position,
            ))
    out.found_or_none("capitalization", cat, c.name, found, "No capitalization inconsistencies found.")


def _numeric_columnish(c: ColumnProfile) -> bool:
    return c.inference.semantic_type in ("integer", "decimal") or c.inference.dominant_type in ("integer", "decimal")


def _check_numeric(c: ColumnProfile, out: _Collector) -> None:
    cat = "Numeric type"
    if not _numeric_columnish(c):
        return
    if c.values_checked < MIN_VALUES_FOR_TYPE:
        out.outcome("numeric_type", cat, c.name, "not_assessed",
                    f"Only {plural(c.values_checked, 'value')} to check; at least "
                    f"{MIN_VALUES_FOR_TYPE} are needed to judge the column's type.")
        return
    bad = c.values_checked - c.numeric_count
    found = False
    if bad:
        found = True
        out.issue(QualityIssue(
            "numeric_mismatch", cat, c.name,
            "high" if c.inference.semantic_type == "mixed" else "medium",
            bad, c.values_checked, "values checked",
            f"The column looks numeric ({c.numeric_count:,} of {c.values_checked:,} values checked parse "
            f"as numbers), but {plural(bad, 'value')} {verb(bad, 'does', 'do')} not. Those values would block or distort "
            "calculations once the column is treated as numbers.",
            "Review the non-numeric values: correct them, or treat them as missing if they mean 'unknown'.",
            examples=tuple(f"{_shown(v.value)} × {v.count:,}" for v in c.non_numeric_examples),
            position=c.position,
        ))
    if c.numeric_formatted_count:
        found = True
        out.issue(QualityIssue(
            "numeric_formatting", cat, c.name, "low", c.numeric_formatted_count, c.values_checked,
            "values checked",
            f"{plural(c.numeric_formatted_count, 'value')} {verb(c.numeric_formatted_count, 'contains', 'contain')} currency symbols, thousands "
            "separators, percent signs or parentheses (for example \"$1,234.50\"). They parse as "
            "numbers only after those characters are removed, and they are left out of the column's "
            "statistics.",
            "Remove the formatting characters consistently, after confirming the separator convention.",
            position=c.position,
        ))
    out.found_or_none("numeric_type", cat, c.name, found, "Every value checked parses as a plain number.")


def _check_dates(c: ColumnProfile, out: _Collector) -> None:
    cat = "Date type"
    if not ({c.inference.semantic_type, c.inference.dominant_type} & {"date", "datetime"}):
        return
    if c.values_checked < MIN_VALUES_FOR_TYPE:
        out.outcome("date_type", cat, c.name, "not_assessed",
                    f"Only {plural(c.values_checked, 'value')} to check; at least "
                    f"{MIN_VALUES_FOR_TYPE} are needed to judge the column's type.")
        return
    found = False
    bad = c.values_checked - c.date_count
    if bad:
        found = True
        invalid = (f" {plural(c.date_invalid_count, 'value')} {verb(c.date_invalid_count, 'looks', 'look')} like dates but are not real calendar "
                   "dates (for example 2024-02-30)." if c.date_invalid_count else "")
        out.issue(QualityIssue(
            "date_mismatch", cat, c.name,
            "high" if c.inference.semantic_type == "mixed" else "medium",
            bad, c.values_checked, "values checked",
            f"The column looks like dates ({c.date_count:,} of {c.values_checked:,} values checked), "
            f"but {plural(bad, 'value')} {verb(bad, 'is', 'are')} not recognised as valid dates.{invalid}",
            "Review these values: correct them, or treat them as missing if they mean 'unknown'.",
            examples=tuple(f"{_shown(v.value)} × {v.count:,}" for v in c.non_date_examples),
            position=c.position,
        ))
    if len(c.date_formats) > 1:
        found = True
        main = c.date_formats[0]
        others = c.date_count - main.count
        out.issue(QualityIssue(
            "date_formats", cat, c.name, "medium", others, c.date_count, "valid dates",
            f"Dates are written in {len(c.date_formats)} formats. The most common is {main.label} "
            f"({main.count:,}); {plural(others, 'date')} {verb(others, 'uses', 'use')} another format.",
            "Convert all dates to one format (ISO YYYY-MM-DD is unambiguous).",
            examples=tuple(f"{f.label} × {f.count:,}" for f in c.date_formats),
            position=c.position,
        ))
    if c.date_ambiguous_count:
        found = True
        out.issue(QualityIssue(
            "date_ambiguous", cat, c.name, "medium", c.date_ambiguous_count, c.date_count, "valid dates",
            f"{plural(c.date_ambiguous_count, 'date')} can be read month-first or day-first with "
            "different results (for example 01/05/2024 or 01.05.2024 is 5 January or 1 May). The values alone "
            "cannot tell which was meant.",
            "Confirm the convention with the data's source before converting these dates.",
            position=c.position,
        ))
    out.found_or_none("date_type", cat, c.name, found,
                      "Every value checked is a valid date in a single, unambiguous format.")


# ------------------------------------------------------------------
# Validity rules
# ------------------------------------------------------------------

def _rule_values(series: pd.Series) -> pd.Series:
    """Values checked (non-missing, not whitespace-only, not null-like), as Arrow text.

    Typed columns (from Convert data type) are read as REFINE's canonical
    text: 1250.5, true/false, 2026-01-15. A number written that way parses
    back to exactly the stored value, so range rules check the stored numbers.
    """
    from refine.conversion import canonical_text, column_kind

    kind = column_kind(series.dtype)
    if kind not in ("text", "other"):
        present = series.dropna()
        series = pd.Series([canonical_text(v, kind) for v in present.tolist()], dtype=TEXT_DTYPE)
    s = series if series.dtype == TEXT_DTYPE else series.astype(TEXT_DTYPE)
    s = s[s.notna()]
    stripped = s.str.strip()
    keep = (stripped != "") & ~stripped.str.lower().isin(NULL_TOKENS)
    return s[keep.to_numpy(dtype=bool)]


def _fullmatch_re2(values: pd.Series, pattern: str) -> np.ndarray:
    """Full-match with RE2 through pyarrow, never Python's re: RE2 runs in
    linear time and raises on unsupported syntax (backreferences, lookaround)
    instead of silently matching nothing."""
    import pyarrow as pa
    import pyarrow.compute as pc

    array = pa.array(values.array, type=pa.string()) if len(values) else pa.array([], type=pa.string())
    result = pc.match_substring_regex(array, f"^(?:{pattern})$")
    return np.asarray(result.to_numpy(zero_copy_only=False), dtype=bool)


def _check_rule(rule: ValidityRule, frame: pd.DataFrame, out: _Collector) -> None:
    cat = "Validity rule"
    check = f"rule:{rule.summary}"
    if rule.column not in frame.columns:
        out.outcome(check, cat, rule.column, "not_assessed",
                    f"Rule \"{rule.summary}\": the table has no column {quote(rule.column)}.")
        return
    position = list(frame.columns).index(rule.column) + 1
    values = _rule_values(frame[rule.column])

    if rule.kind == "range":
        if rule.minimum is None and rule.maximum is None:
            out.outcome(check, cat, rule.column, "not_assessed", f"Rule \"{rule.summary}\" has no bounds.")
            return
        numbers = pd.to_numeric(values.str.strip(), errors="coerce").to_numpy(dtype=float, na_value=np.nan)
        parsed = ~np.isnan(numbers)
        checked = int(parsed.sum())
        skipped = len(values) - checked
        if checked == 0:
            out.outcome(check, cat, rule.column, "not_assessed",
                        f"Rule \"{rule.summary}\": no values parse as plain numbers.")
            return
        bad = np.zeros(len(numbers), dtype=bool)
        if rule.minimum is not None:
            bad |= parsed & (numbers < rule.minimum)
        if rule.maximum is not None:
            bad |= parsed & (numbers > rule.maximum)
        affected = int(bad.sum())
        examples = tuple(sorted({str(v) for v in values[bad].head(200).tolist()})[:5])
        note = (f" {plural(skipped, 'value')} that {verb(skipped, 'is not a plain number was', 'are not plain numbers were')} "
                "not checked."
                if skipped else "")
        denominator, label = checked, "values parsed as numbers"
        explanation = f"{plural(affected, 'value')} {verb(affected, 'falls', 'fall')} outside the rule \"{rule.summary}\".{note}"
    else:
        if rule.kind == "allowed_values":
            if not rule.allowed:
                out.outcome(check, cat, rule.column, "not_assessed", "The rule lists no allowed values.")
                return
            if rule.case_sensitive:
                ok = values.isin(rule.allowed).to_numpy(dtype=bool)
            else:
                allowed = {a.strip().lower() for a in rule.allowed}
                ok = values.str.strip().str.lower().isin(allowed).to_numpy(dtype=bool)
        else:
            if not rule.pattern:
                out.outcome(check, cat, rule.column, "not_assessed", "The rule has no pattern.")
                return
            try:
                ok = _fullmatch_re2(values, rule.pattern)
            except Exception as exc:  # pyarrow rejects patterns RE2 does not support
                out.outcome(check, cat, rule.column, "not_assessed",
                            f"Rule \"{rule.summary}\": the pattern is not valid ({type(exc).__name__}).")
                return
        if len(values) == 0:
            out.outcome(check, cat, rule.column, "not_assessed",
                        f"Rule \"{rule.summary}\": the column has no values to check.")
            return
        bad = ~ok
        affected = int(bad.sum())
        bad_values = values[bad]
        vc = bad_values.value_counts().head(5)
        examples = tuple(f"{_shown(str(v))} × {int(n):,}" for v, n in vc.items())
        denominator, label = len(values), "values checked"
        explanation = f"{plural(affected, 'value')} {verb(affected, 'does', 'do')} not satisfy the rule \"{rule.summary}\"."

    if affected:
        out.issue(QualityIssue(
            check, cat, rule.column, _by_share(affected, denominator, 0.10, 0.01),
            affected, denominator, label, explanation,
            "Review these values against the rule; correct them or relax the rule if it is too strict.",
            examples=examples, position=position,
        ))
    out.found_or_none(check, cat, rule.column, bool(affected), f"Every value checked meets \"{rule.summary}\".")


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def assess_quality(frame: pd.DataFrame, profile: DatasetProfile,
                   rules: tuple[ValidityRule, ...] | list[ValidityRule] = ()) -> QualityReport:
    """Run every check. Reads `frame` only for validity rules; never modifies it."""
    out = _Collector()
    _check_duplicates(profile, out)
    for c in profile.columns:
        _check_column(c, out)
    rules = tuple(rules)
    if rules:
        for rule in rules:
            _check_rule(rule, frame, out)
    else:
        out.outcome("validity_rules", "Validity rule", None, "not_assessed",
                    "No validity rules are defined for this dataset, so value validity "
                    "(ranges, allowed values, formats) was not assessed.")
    issues = sorted(out.issues, key=lambda i: (SEVERITY_ORDER[i.severity], CATEGORIES.index(i.category),
                                               i.position, i.check))
    return QualityReport(tuple(issues), tuple(out.outcomes), rules)


def type_summary(c: ColumnProfile) -> str:
    """'Integer (inferred, medium confidence)' for tables."""
    inf = c.inference
    return f"{inf.label} ({inf.status}, {inf.confidence} confidence)"


__all__ = [
    "CAVEAT", "CATEGORIES", "SEVERITY_LABELS", "SEVERITY_ORDER", "CheckOutcome", "QualityIssue",
    "QualityReport", "ValidityRule", "assess_quality", "type_summary", "SEMANTIC_LABELS",
]
