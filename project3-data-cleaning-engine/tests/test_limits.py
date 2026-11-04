"""Row, column and cell limits reject oversized input instead of truncating it."""

import pytest

import refine.csv_reader as csv_reader
import refine.loader as loader
import refine.xlsx_reader as xlsx_reader
from refine.errors import LoadError
from tests.conftest import load_csv, load_xlsx, make_xlsx


def csv_rows(n: int, cols: int = 2) -> str:
    header = ",".join(f"c{j}" for j in range(cols))
    return header + "\n" + "".join(",".join(str(i) for _ in range(cols)) + "\n" for i in range(n))


def test_csv_at_row_limit_loads(monkeypatch):
    monkeypatch.setattr(csv_reader, "MAX_ROWS", 10)
    assert len(load_csv(csv_rows(10)).data) == 10


def test_csv_over_row_limit_rejected(monkeypatch):
    monkeypatch.setattr(csv_reader, "MAX_ROWS", 10)
    with pytest.raises(LoadError) as err:
        load_csv(csv_rows(11))
    assert err.value.code == "too_many_rows"


def test_skipped_rows_do_not_count_toward_row_limit(monkeypatch):
    monkeypatch.setattr(csv_reader, "MAX_ROWS", 10)
    text = "title\nsubtitle\n" + csv_rows(10)
    assert len(load_csv(text, skip_rows=2).data) == 10


def test_xlsx_over_row_limit_rejected(monkeypatch):
    monkeypatch.setattr(xlsx_reader, "MAX_ROWS", 5)
    data = make_xlsx({"S": [["a"]] + [[i] for i in range(6)]})
    with pytest.raises(LoadError) as err:
        load_xlsx(data)
    assert err.value.code == "too_many_rows"


def test_column_limit(monkeypatch):
    monkeypatch.setattr(loader, "MAX_COLUMNS", 3)
    assert load_csv(csv_rows(1, cols=3)).data.shape == (1, 3)
    with pytest.raises(LoadError) as err:
        load_csv(csv_rows(1, cols=4))
    assert err.value.code == "too_many_columns"


def test_cell_limit(monkeypatch):
    monkeypatch.setattr(loader, "MAX_CELLS", 20)
    assert load_csv(csv_rows(10, cols=2)).data.shape == (10, 2)
    with pytest.raises(LoadError) as err:
        load_csv(csv_rows(11, cols=2))
    assert err.value.code == "too_many_cells"


def test_xlsx_cell_limit_stops_early(monkeypatch):
    monkeypatch.setattr(xlsx_reader, "MAX_CELLS", 10)
    data = make_xlsx({"S": [["a", "b"]] + [[i, i] for i in range(6)]})
    with pytest.raises(LoadError) as err:
        load_xlsx(data)
    assert err.value.code == "too_many_cells"


def test_failed_load_message_states_the_limit(monkeypatch):
    monkeypatch.setattr(csv_reader, "MAX_ROWS", 3)
    with pytest.raises(LoadError) as err:
        load_csv(csv_rows(4))
    assert "3 data rows" in err.value.message
