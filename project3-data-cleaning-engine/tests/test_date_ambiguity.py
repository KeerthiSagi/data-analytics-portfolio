"""Regression tests: dotted dates are not assumed to be day-first.

Before Phase 3, 05.01.2024 was classified as DD.MM.YYYY (5 January) and
never flagged, although it can also be read as 1 May. Dotted dates are
now treated like slashed dates: ambiguous when both readings are valid
calendar dates and differ. Nothing is converted.
"""

import pandas as pd
import pytest

from refine.profile import classify_date, profile_column, profile_dataset
from refine.quality import assess_quality
from refine.source import TEXT_DTYPE
from tests.conftest import load_csv

DOTTED = "DD.MM.YYYY or MM.DD.YYYY"


def text(values):
    return pd.Series(pd.array(values, dtype=TEXT_DTYPE), name="d")


@pytest.mark.parametrize("value, valid, ambiguous", [
    ("05.01.2024", True, True),          # 5 January or 1 May
    ("01.05.2024", True, True),
    ("13.05.2024", True, False),         # Only day-first is a real date
    ("05.13.2024", True, False),         # Only month-first is a real date
    ("05.05.2024", True, False),         # Same date either way
    ("31.02.2024", False, False),        # Neither reading is a real date
    ("32.01.2024", False, False),
    ("05.01.2024 14:30", True, True),    # With a time
])
def test_dotted_dates(value, valid, ambiguous):
    label, ok, amb, _ = classify_date(value)
    assert (label, ok, amb) == (DOTTED, valid, ambiguous)


def test_column_of_dotted_dates_is_flagged_not_converted():
    values = ["05.01.2024", "06.02.2024", "13.03.2024", "07.07.2024", "25.12.2024"]
    c = profile_column(text(values))
    assert c.date_count == 5 and c.date_ambiguous_count == 2
    assert [f.label for f in c.date_formats] == [DOTTED]
    frame = pd.DataFrame({"d": pd.array(values, dtype=TEXT_DTYPE)})
    report = assess_quality(frame, profile_dataset(frame))
    issue = next(i for i in report.issues if i.check == "date_ambiguous")
    assert issue.affected_count == 2 and "01.05.2024" in issue.explanation
    assert frame["d"].tolist() == values                 # Values unchanged


def test_dotted_and_slashed_ambiguity_are_counted_together():
    c = profile_column(text(["05.01.2024", "01/05/2024", "2024-01-05"]))
    assert c.date_ambiguous_count == 2


def test_sample_dataset_dotted_dates_are_now_flagged(sample_bytes):
    ds = load_csv(sample_bytes)
    c = profile_column(ds.data["signup_date"])
    labels = [f.label for f in c.date_formats]
    assert DOTTED in labels and "DD.MM.YYYY" not in labels
    dotted = [v for v in ds.data["signup_date"].dropna() if classify_date(v)[0] == DOTTED]
    expected = sum(classify_date(v)[2] for v in dotted)
    assert expected > 0                                  # The sample has ambiguous dotted dates
    slashed = sum(classify_date(v)[2] for v in ds.data["signup_date"].dropna() if classify_date(v)[0] != DOTTED)
    assert c.date_ambiguous_count == expected + slashed
