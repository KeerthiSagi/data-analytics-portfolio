"""The synthetic sample dataset and the project configuration."""

import importlib.util
import tomllib

import pandas as pd

from refine.config import MAX_UPLOAD_MB
from refine.loader import load_file
from tests.conftest import PROJECT_DIR
from ui import theme


def _generator():
    path = PROJECT_DIR / "sample_data" / "generate_messy_customers.py"
    spec = importlib.util.spec_from_file_location("generate_messy_customers", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------------------
# Sample dataset
# ------------------------------------------------------------------


def test_sample_file_matches_generator_byte_for_byte(sample_bytes):
    assert _generator().build_csv_bytes() == sample_bytes


def test_sample_loads_cleanly(sample_bytes):
    ds = load_file("messy_customers.csv", sample_bytes)
    assert ds.data.shape == (500, 9)
    assert ds.notes == ()
    assert ds.source.encoding == "utf-8" and ds.source.delimiter == ","


def test_sample_contains_the_documented_defects(sample_bytes):
    gen = _generator()
    df = load_file("messy_customers.csv", sample_bytes).data
    expected = gen.EXPECTED
    assert int(df.duplicated().sum()) == expected["exact_duplicate_rows"]
    assert int(df["customer_id"].duplicated().sum()) == (
        expected["exact_duplicate_rows"] + expected["key_duplicate_rows"]
    )
    for token in ["NA", "N/A", "null", "-", ""]:
        assert (df == token).any().any(), f"missing-value marker {token!r} not present"
    assert {"USA", "usa", "U.S.", " United States"} <= set(df["country"])
    assert {"Yes", "N", "true", "0"} <= set(df["is_active"])
    assert df["annual_spend"].str.startswith("$").any()
    # Count per customer: duplicate rows may repeat an outlier value.
    per_customer = df.drop_duplicates(subset="customer_id")
    ages = pd.to_numeric(per_customer["age"], errors="coerce")
    assert int(((ages > 120) | (ages < 0)).sum()) == expected["age_outliers"]


def test_sample_is_synthetic():
    text = (PROJECT_DIR / "sample_data" / "messy_customers.csv").read_text("utf-8")
    emails = [line.split(",")[2] for line in text.splitlines()[1:]]
    assert all(e in {"", "NA", "N/A", "null", "-"} or e.lower().endswith("@example.com") for e in emails)


# ------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------


def _config() -> dict:
    return tomllib.loads((PROJECT_DIR / ".streamlit" / "config.toml").read_text("utf-8"))


def test_upload_limit_matches_engine_limit():
    assert _config()["server"]["maxUploadSize"] == MAX_UPLOAD_MB


def test_streamlit_theme_matches_design_tokens():
    t = _config()["theme"]
    assert t["base"] == "dark"
    assert t["backgroundColor"] == theme.BACKGROUND
    assert t["secondaryBackgroundColor"] == theme.CARD
    assert t["primaryColor"] == theme.ACCENT
    assert t["textColor"] == theme.TEXT
    assert t["borderColor"] == theme.BORDER
    assert _config()["theme"]["sidebar"]["backgroundColor"] == theme.SIDEBAR


def test_requirements_pin_supported_ranges():
    """Every dependency has an upper bound, and every tested version is inside its range."""
    from packaging.requirements import Requirement

    lines = (PROJECT_DIR / "requirements.txt").read_text("utf-8").splitlines()
    reqs = {}
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if line:
            req = Requirement(line)
            reqs[req.name] = req.specifier
    assert set(reqs) == {"streamlit", "pandas", "pyarrow", "openpyxl", "plotly", "numpy"}
    for name, spec in reqs.items():
        assert any(s.operator == "<" for s in spec), f"{name} has no upper bound"
    tested = {"pandas": ["2.2.3", "3.0.6", "3.1.1"], "streamlit": ["1.65.0", "1.67.0"],
              "pyarrow": ["25.0.1", "26.0.1"], "numpy": ["2.5.4", "2.5.5"], "openpyxl": ["3.1.5"],
              "plotly": ["7.1.0"]}
    for name, versions in tested.items():
        for version in versions:
            assert version in reqs[name], f"{name} {version} is tested but outside {reqs[name]}"
