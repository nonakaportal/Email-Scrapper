"""Import an existing list of businesses/websites from CSV for enrichment."""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path

from leadscout.crawler.extract import EMAIL_RE, clean_email
from leadscout.normalize import normalize_domain, normalize_url
from leadscout.sources.base import BusinessCandidate

COLUMN_ALIASES = {
    "website": ("website", "url", "site", "domain", "web", "homepage", "company website"),
    "name": ("name", "business_name", "business", "company", "company_name", "organization"),
    "phone": ("phone", "phone_number", "telephone", "tel"),
    "address": ("address", "street", "street_address"),
    "city": ("city", "town", "locality"),
    "region": ("region", "state", "province"),
    "country": ("country", "country_code"),
    "industry": ("industry", "category"),
    "email": ("email", "emails"),
}


class CSVImportError(ValueError):
    pass


# "Email", "Emails", "Email 2", "email_3", "E-mail" (not "Email 1 status").
EMAIL_COLUMN = re.compile(r"e_?mails?(?:_?\d+)?")


def _emails(cells) -> list[str]:
    """Real addresses from the email cells; "0", "N/A", links and stray text are skipped."""
    found: list[str] = []
    for cell in cells:
        for m in EMAIL_RE.finditer(cell):
            email = clean_email(m.group(1))
            if email and email not in found:
                found.append(email)
    return found


def _column_key(name: str) -> str:
    """"Business Name", "business-name", "businessName" and "business_name" all match."""
    name = re.sub(r"(?<=[a-z])(?=[A-Z])", "_", name.strip())
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _match_columns(fieldnames: list[str]) -> dict[str, str]:
    lookup = {}
    for f in fieldnames:
        if f and _column_key(f):
            lookup.setdefault(_column_key(f), f)
    mapping = {}
    for target, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if _column_key(alias) in lookup:
                mapping[target] = lookup[_column_key(alias)]
                break
    return mapping


def _decode(data: bytes) -> str:
    """CSV text as UTF-8, or as Excel's Windows encoding when it isn't (e.g. byte 0xA0,
    Excel's non-breaking space)."""
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def read_csv(path: str | Path) -> list[BusinessCandidate]:
    path = Path(path)
    if not path.exists():
        raise CSVImportError(f"File not found: {path}")
    with io.StringIO(_decode(path.read_bytes()), newline="") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(fh, dialect=dialect)
        cols = _match_columns(reader.fieldnames or [])
        if "website" not in cols:
            raise CSVImportError(
                f"{path.name} needs a website column (one of: {', '.join(COLUMN_ALIASES['website'])})."
            )
        email_cols = [f for f in reader.fieldnames or [] if f and EMAIL_COLUMN.fullmatch(_column_key(f))]
        out = []
        for i, row in enumerate(reader, start=2):
            get = lambda k: (row.get(cols[k]) or "").strip() if k in cols else ""  # noqa: E731
            website = normalize_url(get("website"))
            if not website:
                continue
            name = get("name") or normalize_domain(website) or website
            country = get("country").upper()
            out.append(BusinessCandidate(
                name=name,
                source="csv",
                source_ref=f"{path.name}:{i}",
                industry=get("industry") or None,
                address=get("address") or None,
                city=get("city") or None,
                region=get("region") or None,
                country=country if len(country) == 2 else None,
                phone=get("phone") or None,
                website=website,
                emails=_emails(row[c] or "" for c in email_cols),
            ))
    return out
