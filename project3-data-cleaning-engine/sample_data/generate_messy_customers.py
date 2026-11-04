"""Generate sample_data/messy_customers.csv: a synthetic, deliberately messy
customer table for demonstrating REFINE.

All people, emails and values are invented. The output is deterministic
(fixed seed, Python's `random.Random`, fixed line endings), so running
this script again reproduces the committed file byte for byte; a test
checks that.

Run from the project folder:
    python sample_data/generate_messy_customers.py
"""

from __future__ import annotations

import csv
import io
import random
from pathlib import Path

SEED = 20261029
N_BASE_ROWS = 480
OUTPUT = Path(__file__).resolve().parent / "messy_customers.csv"

COLUMNS = [
    "customer_id", "full_name", "email", "signup_date", "country",
    "age", "annual_spend", "is_active", "segment",
]

FIRST = ["Ava", "Liam", "Noah", "Emma", "Mia", "Lucas", "Zoe", "Ethan", "Priya", "Omar",
         "Sofia", "Mateo", "Chen", "Aisha", "Kofi", "Hana", "Jonas", "Elena", "Ravi", "Nina"]
LAST = ["Smith", "Garcia", "Nguyen", "Patel", "Kim", "Okafor", "Müller", "Rossi", "Silva",
        "Cohen", "Haddad", "Ivanova", "Johnson", "Tanaka", "Dubois", "O'Brien"]

# Spelling variants of the same country, as found in hand-entered data.
COUNTRY_VARIANTS = {
    "United States": ["United States", "USA", "usa", "U.S.", " United States", "united states"],
    "Canada": ["Canada", "CANADA", "Canada "],
    "United Kingdom": ["United Kingdom", "UK", "U.K.", "united kingdom"],
    "Germany": ["Germany", "germany", "Deutschland"],
    "India": ["India", "india ", "INDIA"],
}
SEGMENTS = ["Consumer", "Corporate", "Small Business"]
SEGMENT_VARIANTS = {"Consumer": ["Consumer", "consumer", "CONSUMER "],
                    "Corporate": ["Corporate", "corporate"],
                    "Small Business": ["Small Business", "small business", "Small  Business"]}
ACTIVE_VARIANTS = {True: ["Yes", "Y", "true", "1", "yes"], False: ["No", "N", "false", "0", "no"]}
NULL_TOKENS = ["", "NA", "N/A", "null", "-"]

# Defects injected on purpose. Tests assert these exact counts.
EXPECTED = {
    "exact_duplicate_rows": 12,      # Extra copies of earlier rows
    "key_duplicate_rows": 8,         # Same customer_id, other fields differ
    "age_outliers": 4,               # Impossible ages (e.g. 230, -3)
    "spend_outliers": 3,             # Extreme annual spend
}


def _date_text(rng: random.Random, year: int, month: int, day: int) -> str:
    style = rng.choices(["iso", "us", "eu", "long", "blank"], weights=[60, 15, 10, 10, 5])[0]
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    if style == "iso":
        return f"{year:04d}-{month:02d}-{day:02d}"
    if style == "us":
        return f"{month:02d}/{day:02d}/{year:04d}"
    if style == "eu":
        return f"{day:02d}.{month:02d}.{year:04d}"
    if style == "long":
        return f"{months[month - 1]} {day}, {year}"
    return rng.choice(NULL_TOKENS)


def _spend_text(rng: random.Random, value: float) -> str:
    style = rng.choices(["plain", "currency", "thousands", "null"], weights=[55, 25, 12, 8])[0]
    if style == "plain":
        return f"{value:.2f}"
    if style == "currency":
        return f"${value:,.2f}"
    if style == "thousands":
        return f"{value:,.0f}"
    return rng.choice(NULL_TOKENS)


def build_rows() -> list[list[str]]:
    rng = random.Random(SEED)
    rows: list[list[str]] = []
    for i in range(1, N_BASE_ROWS + 1):
        first, last = rng.choice(FIRST), rng.choice(LAST)
        name = f"{first} {last}"
        name = rng.choices([name, name.upper(), name.lower(), f"  {name}", f"{name} ", f"{first}  {last}"],
                           weights=[70, 6, 6, 6, 6, 6])[0]
        email = f"{first}.{last}{i}@example.com".lower().replace("'", "").replace("ü", "u")
        email = rng.choices([email, email.upper(), rng.choice(NULL_TOKENS)], weights=[85, 7, 8])[0]
        country = rng.choice(list(COUNTRY_VARIANTS))
        age = rng.randint(18, 82)
        age_text = rng.choices([str(age), f"{age}.0", "unknown", rng.choice(NULL_TOKENS)],
                               weights=[85, 5, 3, 7])[0]
        spend = round(rng.lognormvariate(7.2, 0.6), 2)
        segment = rng.choice(SEGMENTS)
        rows.append([
            f"C-{i:04d}",
            name,
            email,
            _date_text(rng, rng.randint(2019, 2025), rng.randint(1, 12), rng.randint(1, 28)),
            rng.choice(COUNTRY_VARIANTS[country]),
            age_text,
            _spend_text(rng, spend),
            rng.choice(ACTIVE_VARIANTS[rng.random() < 0.7]),
            rng.choice(SEGMENT_VARIANTS[segment]),
        ])

    # Outliers on distinct rows (positions chosen once, deterministically).
    picks = rng.sample(range(N_BASE_ROWS), EXPECTED["age_outliers"] + EXPECTED["spend_outliers"])
    for idx, bad in zip(picks[:EXPECTED["age_outliers"]], ["230", "-3", "999", "150"]):
        rows[idx][5] = bad
    for idx, bad in zip(picks[EXPECTED["age_outliers"]:], ["985000.00", "$1,250,000.00", "740000"]):
        rows[idx][6] = bad

    base = [list(r) for r in rows]

    # Key-based duplicates: reuse an existing customer_id with changed details
    # (email case, name spacing, active flag), so the rows are not identical.
    for idx in rng.sample(range(N_BASE_ROWS), EXPECTED["key_duplicate_rows"]):
        src = list(base[idx])
        src[1] = src[1].strip().title()
        src[2] = src[2].upper() if src[2] not in NULL_TOKENS else "duplicate@example.com"
        src[7] = rng.choice(ACTIVE_VARIANTS[src[7] not in ACTIVE_VARIANTS[True]])
        rows.insert(rng.randint(0, len(rows)), src)

    # Exact duplicates: verbatim copies of distinct original rows.
    for idx in sorted(rng.sample(range(N_BASE_ROWS), EXPECTED["exact_duplicate_rows"])):
        rows.insert(rng.randint(0, len(rows)), list(base[idx]))
    return rows


def build_csv_bytes() -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(COLUMNS)
    writer.writerows(build_rows())
    return buffer.getvalue().encode("utf-8")


if __name__ == "__main__":
    OUTPUT.write_bytes(build_csv_bytes())
    print(f"Wrote {OUTPUT} ({OUTPUT.stat().st_size:,} bytes)")
