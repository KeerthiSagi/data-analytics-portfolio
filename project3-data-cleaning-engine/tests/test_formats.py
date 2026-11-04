"""File type, signature and size checks that run before any parsing."""

import pytest

from refine.config import MAX_UPLOAD_BYTES
from refine.errors import LoadError
from refine.loader import OLE_SIGNATURE, detect_format, load_file
from refine.source import LoadOptions
from tests.conftest import load_csv, make_xlsx


@pytest.mark.parametrize("name, fmt", [
    ("data.csv", "csv"), ("DATA.CSV", "csv"), ("folder/report.Csv", "csv"),
    ("book.xlsx", "xlsx"), ("BOOK.XLSX", "xlsx"),
])
def test_supported_extensions(name, fmt):
    data = make_xlsx({"S": [["a"], [1]]}) if fmt == "xlsx" else b"a\n1\n"
    assert detect_format(name, data) == fmt


def test_legacy_xls_rejected_with_guidance():
    with pytest.raises(LoadError) as err:
        load_file("old.xls", OLE_SIGNATURE + b"\x00" * 100)
    assert err.value.code == "legacy_xls"
    assert "save it as" in err.value.message


@pytest.mark.parametrize("name", ["notes.txt", "data.json", "book.xlsm", "noextension", ""])
def test_unsupported_extensions(name):
    with pytest.raises(LoadError) as err:
        load_file(name, b"a,b\n1,2\n")
    assert err.value.code == "unsupported_type"


def test_zip_content_with_csv_name():
    with pytest.raises(LoadError) as err:
        load_file("data.csv", make_xlsx({"S": [["a"], [1]]}))
    assert err.value.code == "content_mismatch"


def test_csv_content_with_xlsx_name():
    with pytest.raises(LoadError) as err:
        load_file("book.xlsx", b"a,b\n1,2\n")
    assert err.value.code == "invalid_xlsx"


@pytest.mark.parametrize("name", ["book.xlsx", "data.csv"])
def test_ole_container_is_legacy_or_encrypted(name):
    # Legacy .xls files and password-protected .xlsx files are both OLE containers.
    with pytest.raises(LoadError) as err:
        load_file(name, OLE_SIGNATURE + b"\x00" * 512)
    assert err.value.code == "legacy_or_encrypted"


def test_empty_bytes():
    with pytest.raises(LoadError) as err:
        load_file("data.csv", b"")
    assert err.value.code == "empty_file"


def test_file_over_size_limit():
    with pytest.raises(LoadError) as err:
        load_file("big.csv", b"a\n" + b"1\n" * (MAX_UPLOAD_BYTES // 2 + 1))
    assert err.value.code == "file_too_large"


def test_non_bytes_input_rejected():
    with pytest.raises(LoadError) as err:
        load_file("data.csv", "a,b\n1,2\n")  # str, not bytes
    assert err.value.code == "bad_input"


def test_bytearray_and_memoryview_accepted():
    assert load_file("d.csv", bytearray(b"a\n1\n")).data.shape == (1, 1)
    assert load_file("d.csv", memoryview(b"a\n1\n")).data.shape == (1, 1)


def test_options_for_wrong_format_rejected():
    with pytest.raises(LoadError) as err:
        load_csv("a\n1\n", sheet="Sheet1")
    assert err.value.code == "bad_option"
    with pytest.raises(LoadError) as err:
        load_file("b.xlsx", make_xlsx({"S": [["a"], [1]]}),
                  options=LoadOptions(delimiter=";"))
    assert err.value.code == "bad_option"


def test_error_messages_are_plain_text():
    with pytest.raises(LoadError) as err:
        load_file("x.txt", b"1")
    assert str(err.value) == err.value.message
    assert "Traceback" not in err.value.message
