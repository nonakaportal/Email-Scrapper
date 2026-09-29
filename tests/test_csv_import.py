"""Importing the "Enrich my list" CSV."""

from leadscout.sources.csv_import import read_csv


def test_excel_windows_encoding(tmp_path):
    """Excel's "CSV" saves in Windows-1252: é and the non-breaking space (0xA0) are single bytes."""
    path = tmp_path / "excel.csv"
    path.write_bytes("name,website\r\nCafé Roma,cafe-roma.com\r\n".encode("cp1252"))
    (row,) = read_csv(path)
    assert row.name == "Café Roma".strip() and row.website == "https://cafe-roma.com/"


def test_headers_with_spaces_and_camel_case(tmp_path):
    path = tmp_path / "list.csv"
    path.write_text("Business Name,countryCode,Website\nAtlas Construction,US,atlas.com\n", encoding="utf-8")
    (row,) = read_csv(path)
    assert (row.name, row.country) == ("Atlas Construction", "US")
