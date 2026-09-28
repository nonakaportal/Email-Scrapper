"""Chain, franchise and large-company detection.

A business flagged here is not a small independent business: it is left out
of crawling and exports unless chains are included, and loses the
"independent" score points.
"""

from __future__ import annotations

import json
from typing import Mapping

from leadscout.normalize import domain_matches, normalize_name


# Words that describe a trade rather than name a company. A name made only of
# these ("Accounting & Tax Services", "Family Dental") is used by many unrelated
# businesses, so repeating it proves nothing. Industry words from
# industries.yaml are added at runtime.
GENERIC_WORDS = set("""
and the of a an at in on for by to & services service group associates associated company
companies co inc llc ltd solutions professional professionals business businesses consulting
consultants accounting accountancy tax taxes bookkeeping bookkeepers financial finance finances
cpa cpas certified public accountants accountant advisors advisers advisory management
partners firm office offices center centre clinic clinics dental dentistry family care medical
health law legal insurance agency agencies realty real estate home homes auto automotive repair
plumbing electric electrical cleaning income preparation prep studio salon restaurant cafe bar
shop store local city express plus pro best quality first national american united general
new main street community premier elite total complete smart easy quick fast
""".split())


def is_generic_name(name_norm: str, extra_words=()) -> bool:
    """True if every word of the normalized name is a generic trade word."""
    words = [w for w in (name_norm or "").split() if len(w) > 1]
    vocab = GENERIC_WORDS | set(extra_words)
    return not words or all(w in vocab for w in words)


def industry_words(industries: Mapping | None) -> set[str]:
    """Words from industry keys, labels and aliases (config/industries.yaml)."""
    words: set[str] = set()
    for key, spec in (industries or {}).items():
        if str(key).startswith("_"):
            continue
        spec = spec or {}
        for text in [key, spec.get("label", ""), *(spec.get("aliases") or [])]:
            words |= set(normalize_name(str(text).replace("_", " ")).split())
    return words


def _get(row: Mapping, key: str, default=None):
    try:
        value = row[key]
    except (KeyError, IndexError):
        return default
    return default if value is None else value


def _json(value) -> dict:
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value or "{}") or {}
    except (TypeError, ValueError):
        return {}


class ChainDetector:
    def __init__(self, known_chains: list[str], repeat_threshold: int = 3,
                 size: Mapping | None = None, generic_words=()):
        self.known_domains = [c for c in known_chains if "." in c and " " not in c]
        self.known_names = [normalize_name(c) for c in known_chains if c not in self.known_domains]
        self.known_names = [n for n in self.known_names if n]
        self.threshold = int(repeat_threshold)
        self.generic_words = set(generic_words)
        size = size or {}
        self.max_employees = int(size.get("max_employees", 250))
        self.exclude_notable = bool(size.get("exclude_wikipedia_notable", True))
        self.intl_languages = int(size.get("intl_site_languages", 6))
        self.use_investor = bool(size.get("investor_relations", True))
        self.non_commercial = [s.lower() for s in size.get("non_commercial_suffixes", [])]

    def detect(self, business: Mapping, name_counts: Mapping[str, int] | None = None) -> tuple[bool, str | None]:
        """(flagged, reason). ``business`` is a businesses row (or dict)."""
        tags = _json(_get(business, "tags"))
        if tags.get("brand") or tags.get("brand:wikidata"):
            return True, f"part of a brand ({tags.get('brand') or tags.get('brand:wikidata')})"
        name_norm = _get(business, "name_norm") or normalize_name(_get(business, "name", ""))
        padded = f" {name_norm} "
        for chain in self.known_names:
            if f" {chain} " in padded:
                return True, f"known chain or large firm ({chain})"
        domain = _get(business, "domain")
        if domain and domain_matches(domain, self.known_domains):
            return True, "known chain or large firm (website)"
        if domain and any(domain.lower().endswith(sfx) for sfx in self.non_commercial):
            return True, "government, university or other non-commercial website"
        locations = int(_get(business, "location_count", 1) or 1)
        if name_counts and name_norm and not self.is_generic(name_norm):
            locations = max(locations, int(name_counts.get(name_norm, 0)))
        if locations >= self.threshold:
            return True, f"same name/website at {locations} locations"
        if _get(business, "multi_location_site"):
            return True, "website has a locations finder"
        large = self.large_company(_json(_get(business, "org_info")))
        if large:
            return True, large
        if self.use_investor and _get(business, "investor_relations"):
            return True, "large company: website has an investor relations section"
        langs = int(_get(business, "intl_languages", 0) or 0)
        if self.intl_languages and langs >= self.intl_languages:
            return True, f"large company: international website in {langs} language/country versions"
        return False, None

    def is_generic(self, name_norm: str) -> bool:
        return is_generic_name(name_norm, self.generic_words)

    def large_company(self, info: dict) -> str | None:
        """Reason the Wikidata evidence shows a large company, else None."""
        if not info:
            return None
        who = info.get("label") or "company"
        employees = info.get("employees")
        if employees and employees > self.max_employees:
            return f"large company: {who} has about {employees:,} employees (Wikidata)"
        if info.get("listed"):
            return f"large company: {who} is listed on a stock exchange (Wikidata)"
        if info.get("types"):
            return f"large company: {who} is a {info['types'][0]} (Wikidata)"
        if self.exclude_notable and (info.get("wikipedias") or 0) >= 1:
            n = info["wikipedias"]
            return f"well-known company: {who} has Wikipedia articles in {n} language{'s' if n > 1 else ''}"
        return None
