"""Rule-based quality checks (refine.quality)."""

import pandas as pd
import pytest

from refine.profile import profile_dataset
from refine.quality import CATEGORIES, ValidityRule, assess_quality
from refine.source import TEXT_DTYPE


def frame(**cols):
    return pd.DataFrame({k: pd.array(v, dtype=TEXT_DTYPE) for k, v in cols.items()})


def run(df, rules=()):
    return assess_quality(df, profile_dataset(df), rules)


def issue(report, check, column=None):
    found = [i for i in report.issues if i.check == check and i.column == column]
    assert len(found) == 1, f"expected one {check} issue for {column}, got {found}"
    return found[0]


def has(report, check, column=None):
    return any(i.check == check and i.column == column for i in report.issues)


def outcome(report, check, column=None):
    return next(o for o in report.outcomes if o.check == check and o.column == column)


# ------------------------------------------------------------------
# Every issue is complete
# ------------------------------------------------------------------

def test_every_issue_has_required_fields(sample_bytes):
    from refine.loader import load_file
    from ui.state import SAMPLE_RULES

    df = load_file("s.csv", sample_bytes).data
    report = run(df, SAMPLE_RULES)
    assert report.issues
    for i in report.issues:
        assert i.category in CATEGORIES
        assert i.severity in {"high", "medium", "low"}
        assert i.scope
        assert 0 < i.affected_count <= i.denominator
        assert i.denominator_label
        assert i.percentage == pytest.approx(100 * i.affected_count / i.denominator)
        assert i.explanation.endswith(".") and i.suggested_action


def test_issues_sorted_by_severity():
    df = frame(a=[None] * 6 + ["1"] * 4, b=["x"] * 10)
    order = [i.severity for i in run(df).issues]
    rank = {"high": 0, "medium": 1, "low": 2}
    assert order == sorted(order, key=rank.get)


def test_percentage_none_when_denominator_zero():
    from refine.quality import QualityIssue

    i = QualityIssue("x", "Missing values", None, "low", 0, 0, "rows", "Nothing.")
    assert i.percentage is None


def test_no_overall_score_exposed():
    report = run(frame(a=["1", "2"]))
    assert not any("score" in name for name in dir(report))


# ------------------------------------------------------------------
# Individual checks: counts and denominators
# ------------------------------------------------------------------

def test_missing_values_issue():
    df = frame(a=[None, "", "x", "y", "NA"])
    i = issue(run(df), "missing_values", "a")
    assert (i.affected_count, i.denominator, i.denominator_label) == (2, 5, "rows")
    assert "1 with no value, 1 empty text" in i.explanation
    assert i.severity == "medium"                   # 40%


@pytest.mark.parametrize("n_missing, severity", [(1, "low"), (3, "medium"), (5, "high")])
def test_missing_severity_thresholds(n_missing, severity):
    df = frame(a=[None] * n_missing + ["x"] * (10 - n_missing))
    assert issue(run(df), "missing_values", "a").severity == severity


def test_null_tokens_issue_separate_from_missing():
    df = frame(a=["NA", "null", "x", "y"])
    report = run(df)
    i = issue(report, "null_tokens", "a")
    assert (i.affected_count, i.denominator) == (2, 4)
    assert not has(report, "missing_values", "a")
    assert any('"NA" × 1' in e for e in i.examples)


def test_duplicate_rows_issue():
    df = frame(a=["1", "1", "2", "2", "3"], b=["x", "x", "y", "y", "z"])
    i = issue(run(df), "duplicate_rows")
    assert (i.affected_count, i.denominator, i.denominator_label) == (2, 5, "rows")
    assert i.scope == "Dataset"


def test_no_duplicates_is_none_detected_not_clean():
    report = run(frame(a=["1", "2"]))
    o = outcome(report, "duplicate_rows")
    assert o.status == "none_detected"
    assert "clean" not in o.detail.lower()


def test_empty_column_issue_replaces_missing_issue():
    df = frame(a=["1", "2", "3"], column_2=[None, "", "  "])
    report = run(df)
    i = issue(report, "empty_column", "column_2")
    assert (i.affected_count, i.denominator) == (3, 3)
    assert "1 whitespace-only value" in i.explanation
    assert not has(report, "missing_values", "column_2")


def test_whitespace_issues():
    df = frame(a=[" x", "y ", " z ", "a  b", "c", "   ", None])
    report = run(df)
    edge = issue(report, "edge_whitespace", "a")
    assert (edge.affected_count, edge.denominator, edge.denominator_label) == (3, 6, "non-missing values")
    assert issue(report, "repeated_spaces", "a").affected_count == 1
    assert issue(report, "whitespace_only", "a").affected_count == 1
    assert '" x" × 1' in edge.examples


def test_capitalization_variants():
    df = frame(c=["USA"] * 5 + ["usa"] * 2 + ["Usa"] + ["Canada"] * 4)
    i = issue(run(df), "case_variants", "c")
    assert (i.affected_count, i.denominator, i.denominator_label) == (3, 12, "values checked")
    assert i.examples[0].startswith('"USA" × 5')


def test_case_style_mix():
    df = frame(n=["Alice Smith", "Bob Jones", "Carol White", "Dan Brown", "EVE BLACK"])
    i = issue(run(df), "case_style_mix", "n")
    assert (i.affected_count, i.denominator) == (1, 5)
    assert i.severity == "low"


def test_capitalization_not_checked_for_numbers_or_dates():
    df = frame(d=["Jan 5, 2024", "JAN 6, 2024", "jan 7, 2024", "Jan 8, 2024", "Jan 9, 2024"])
    report = run(df)
    assert not has(report, "case_variants", "d") and not has(report, "case_style_mix", "d")


def test_numeric_mismatch():
    df = frame(age=[str(i) for i in range(20)] + ["unknown", "n/a", None])
    i = issue(run(df), "numeric_mismatch", "age")
    # "n/a" is a null-like token and None is missing: neither is a values-checked mismatch.
    assert (i.affected_count, i.denominator, i.denominator_label) == (1, 20 + 1, "values checked")
    assert i.severity == "medium"
    assert i.examples == ('"unknown" × 1',)


def test_mixed_numeric_column_is_high_severity():
    df = frame(v=["1", "2", "3", "4", "5", "6", "a", "b", "c", "d"])
    assert issue(run(df), "numeric_mismatch", "v").severity == "high"


def test_numeric_formatting():
    df = frame(v=["$1,200.00", "1,500", "100", "200", "300"])
    i = issue(run(df), "numeric_formatting", "v")
    assert (i.affected_count, i.denominator) == (2, 5)


def test_text_column_has_no_numeric_check():
    report = run(frame(t=["apple", "pear", "fig", "kiwi", "plum", "1"]))
    assert not any(o.check == "numeric_type" and o.column == "t" for o in report.outcomes)


def test_date_checks():
    df = frame(d=["2024-01-05"] * 6 + ["01/05/2024", "02/03/2024", "Jan 5, 2024", "2024-13-01", "tomorrow"])
    report = run(df)
    mismatch = issue(report, "date_mismatch", "d")
    assert (mismatch.affected_count, mismatch.denominator) == (2, 11)
    assert "1 value looks like" in mismatch.explanation
    formats = issue(report, "date_formats", "d")
    assert (formats.affected_count, formats.denominator, formats.denominator_label) == (3, 9, "valid dates")
    ambiguous = issue(report, "date_ambiguous", "d")
    assert ambiguous.affected_count == 2


def test_type_checks_not_assessed_with_too_few_values():
    report = run(frame(a=["1", "2", "x"]))
    o = outcome(report, "numeric_type", "a")
    assert o.status == "not_assessed" and "at least 5" in o.detail


def test_confirmed_numeric_column_skips_text_checks():
    df = pd.DataFrame({"n": pd.array([1, None, 3], dtype="Int64")})
    report = run(df)
    assert issue(report, "missing_values", "n").affected_count == 1
    assert not any(o.column == "n" and o.check in ("whitespace", "null_tokens") for o in report.outcomes)


# ------------------------------------------------------------------
# Validity rules
# ------------------------------------------------------------------

def test_without_rules_validity_is_not_assessed():
    report = run(frame(a=["1"]))
    o = outcome(report, "validity_rules")
    assert o.status == "not_assessed" and "No validity rules" in o.detail


def test_range_rule():
    df = frame(age=["10", "200", "-1", "x", "NA", None, " 50 "])
    rule = ValidityRule("age", "range", minimum=0, maximum=120)
    i = issue(run(df, [rule]), f"rule:{rule.summary}", "age")
    assert (i.affected_count, i.denominator, i.denominator_label) == (2, 4, "values parsed as numbers")
    assert "1 value that is not a plain number was not checked" in i.explanation   # "x"


def test_range_rule_one_bound():
    df = frame(v=["-1", "5"])
    rule = ValidityRule("v", "range", minimum=0)
    assert issue(run(df, [rule]), f"rule:{rule.summary}", "v").affected_count == 1
    assert rule.summary == "v at least 0"


def test_allowed_values_rule_case_sensitive_and_not():
    df = frame(s=["A", "a", "B", "c", ""])
    strict = ValidityRule("s", "allowed_values", allowed=("A", "B"))
    loose = ValidityRule("s", "allowed_values", allowed=("A", "B"), case_sensitive=False)
    report = run(df, [strict, loose])
    assert issue(report, f"rule:{strict.summary}", "s").affected_count == 2       # "a", "c"
    assert issue(report, f"rule:{loose.summary}", "s").affected_count == 1        # "c"
    assert "ignoring capitalization" in loose.summary


def test_pattern_rule_and_invalid_pattern():
    df = frame(code=["A-1234", "B-12", "c-9999"])
    good = ValidityRule("code", "pattern", pattern=r"[A-Z]-[0-9]{4}")
    bad = ValidityRule("code", "pattern", pattern=r"(a)\1")          # Backreference: not RE2
    report = run(df, [good, bad])
    assert issue(report, f"rule:{good.summary}", "code").affected_count == 2
    o = outcome(report, f"rule:{bad.summary}", "code")
    assert o.status == "not_assessed" and "not valid" in o.detail


def test_rule_on_missing_column_not_assessed():
    rule = ValidityRule("nope", "range", minimum=0)
    o = outcome(run(frame(a=["1"]), [rule]), f"rule:{rule.summary}", "nope")
    assert o.status == "not_assessed"


def test_rule_without_numbers_not_assessed():
    rule = ValidityRule("a", "range", minimum=0)
    o = outcome(run(frame(a=["x", "y"]), [rule]), f"rule:{rule.summary}", "a")
    assert o.status == "not_assessed"


def test_rule_passing_is_none_detected():
    rule = ValidityRule("a", "range", minimum=0, maximum=10)
    o = outcome(run(frame(a=["1", "2"]), [rule]), f"rule:{rule.summary}", "a")
    assert o.status == "none_detected"


# ------------------------------------------------------------------
# Whole-dataset behaviour
# ------------------------------------------------------------------

def test_quality_does_not_mutate_input(sample_bytes):
    from refine.loader import load_file
    from ui.state import SAMPLE_RULES

    df = load_file("s.csv", sample_bytes).data
    before = df.copy(deep=True)
    run(df, SAMPLE_RULES)
    pd.testing.assert_frame_equal(df, before)


def test_sample_quality_counts(sample_bytes):
    from refine.loader import load_file
    from ui.state import SAMPLE_RULES

    report = run(load_file("s.csv", sample_bytes).data, SAMPLE_RULES)
    assert issue(report, "duplicate_rows").affected_count == 12
    assert issue(report, "numeric_mismatch", "age").examples[0].startswith('"unknown"')
    assert has(report, "date_formats", "signup_date") and has(report, "date_ambiguous", "signup_date")
    assert has(report, "case_variants", "country") and has(report, "edge_whitespace", "country")
    assert has(report, "numeric_formatting", "annual_spend")
    age_rule = next(i for i in report.issues if i.check.startswith("rule:age"))
    assert age_rule.affected_count >= 4             # 230, -3, 999, 150 (plus repeats in duplicate rows)
    assert report.not_assessed == ()


def test_dataset_with_no_problems_still_does_not_claim_clean():
    report = run(frame(id=[str(i) for i in range(10)], name=[f"Name{i}" for i in range(10)]))
    assert report.issues == ()
    texts = " ".join(o.detail for o in report.outcomes).lower()
    assert "clean" not in texts and "perfect" not in texts
    assert outcome(report, "validity_rules").status == "not_assessed"
