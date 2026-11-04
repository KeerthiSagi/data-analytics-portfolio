"""CSV encoding detection, overrides and byte-order marks."""

import codecs

import pytest

from refine.errors import LoadError
from tests.conftest import codes, column, load_csv

TEXT = "city,note\nMünchen,café\nZürich,naïve\n"


def test_utf8_detected_without_notes():
    ds = load_csv(TEXT)
    assert ds.source.encoding == "utf-8" and ds.source.encoding_detected
    assert column(ds, "city") == ["München", "Zürich"]
    assert codes(ds) == []


def test_utf8_bom_removed_and_reported():
    ds = load_csv(codecs.BOM_UTF8 + TEXT.encode("utf-8"))
    assert ds.source.columns[0] == "city"          # BOM is not part of the header
    assert ds.source.encoding == "utf-8"
    assert "bom_removed" in codes(ds)


def test_cp1252_fallback_is_a_warning():
    data = "name,price\nCafé,€5\n".encode("cp1252")
    ds = load_csv(data)
    assert ds.source.encoding == "cp1252"
    assert column(ds, "name") == ["Café"] and column(ds, "price") == ["€5"]
    note = ds.note("encoding_fallback")
    assert note.severity == "warning" and "Windows-1252" in note.message


def test_latin1_is_final_fallback():
    # 0x81 is undefined in cp1252, so only latin-1 decodes it.
    data = b"a,b\nx\x81,\xe9\n"
    ds = load_csv(data)
    assert ds.source.encoding == "latin-1"
    assert column(ds, "b") == ["é"]
    assert ds.note("encoding_fallback").severity == "warning"


@pytest.mark.parametrize("codec", ["utf-16", "utf-16-le", "utf-16-be"])
def test_utf16_with_bom(codec):
    raw = TEXT.encode(codec)
    if codec != "utf-16":  # add the byte order mark explicitly
        raw = (codecs.BOM_UTF16_LE if codec.endswith("le") else codecs.BOM_UTF16_BE) + raw
    ds = load_csv(raw)
    assert ds.source.encoding == "utf-16"
    assert column(ds, "note") == ["café", "naïve"]


def test_utf16_without_bom_is_rejected_as_binary():
    with pytest.raises(LoadError) as err:
        load_csv(TEXT.encode("utf-16-le"))
    assert err.value.code == "binary_content"
    assert "UTF-16" in err.value.message


def test_explicit_encoding_used_and_not_marked_detected():
    data = "name\nCafé\n".encode("cp1252")
    ds = load_csv(data, encoding="cp1252")
    assert ds.source.encoding == "cp1252" and not ds.source.encoding_detected
    assert "encoding_fallback" not in codes(ds)


def test_explicit_encoding_can_change_interpretation():
    # Valid UTF-8 read as latin-1 shows mojibake: the user's choice is respected.
    ds = load_csv("x\né\n".encode("utf-8"), encoding="latin-1")
    assert column(ds, "x") == ["Ã©"]


def test_explicit_encoding_that_fails_is_an_error():
    with pytest.raises(LoadError) as err:
        load_csv("x\né\n".encode("latin-1"), encoding="utf-8")
    assert err.value.code == "decode_failed"


@pytest.mark.parametrize("alias, expected", [("UTF8", "utf-8"), ("latin1", "latin-1"),
                                             ("windows-1252", "cp1252"), ("ISO-8859-1", "latin-1")])
def test_encoding_aliases(alias, expected):
    assert load_csv("a\n1\n", encoding=alias).source.encoding == expected


def test_unknown_encoding_rejected():
    with pytest.raises(LoadError) as err:
        load_csv("a\n1\n", encoding="shift_jis")
    assert err.value.code == "unsupported_encoding"


def test_binary_content_rejected():
    with pytest.raises(LoadError) as err:
        load_csv(b"a,b\n\x00\x01\x02\n")
    assert err.value.code == "binary_content"
