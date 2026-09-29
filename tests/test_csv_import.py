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


def test_all_email_columns_and_junk_skipped(tmp_path):
    path = tmp_path / "list.csv"
    path.write_text("Business Name,website,Email,Email 2,Email 1 status\n"
                    "A,a.com,0,N/A,sent\n"
                    "B,b.com,https://www.bbb.org/x?email=primary,Info: info@b.com / sales@b.com,bounced\n",
                    encoding="utf-8")
    a, b = read_csv(path)
    assert a.emails == [] and b.emails == ["info@b.com", "sales@b.com"]
