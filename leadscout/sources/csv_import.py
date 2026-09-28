"""Import an existing list of businesses/websites from CSV for enrichment."""

from __future__ import annotations

import csv
from pathlib import Path

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


def _match_columns(fieldnames: list[str]) -> dict[str, str]:
    lookup = {f.strip().lower().replace("-", "_"): f for f in fieldnames if f}
    mapping = {}
    for target, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            key = alias.replace(" ", "_")
            if key in lookup:
                mapping[target] = lookup[key]
                break
            if alias in lookup:
                mapping[target] = lookup[alias]
                break
    return mapping


def read_csv(path: str | Path) -> list[BusinessCandidate]:
    path = Path(path)
    if not path.exists():
        raise CSVImportError(f"File not found: {path}")
    with path.open(newline="", encoding="utf-8-sig") as fh:
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
                emails=[e.strip() for e in get("email").replace(",", ";").split(";") if e.strip()],
            ))
    return out
