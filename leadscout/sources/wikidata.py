"""Company-size evidence from Wikidata (free, public).

For each business we search Wikidata by name and accept an entity only if its
official website matches the business's domain: the same registered domain,
or the same brand label on another country domain with the same name
(fiducial.com vs fiducial.fr). Evidence returned: employee count, stock
listing, entity types, and how many Wikipedias have an article about it.

Wikimedia asks for serial requests and a User-Agent with contact details, so
lookups are rate limited and only run when contact_email is set.
"""

from __future__ import annotations

import logging
import re

import httpx

from leadscout.config import Config
from leadscout.db.store import Store
from leadscout.http import api_client, limiter_for
from leadscout.normalize import domain_label, normalize_name, registered_domain

log = logging.getLogger(__name__)

API = "https://www.wikidata.org/w/api.php"
MULTINATIONAL = "Q161726"
PUBLIC_COMPANY = "Q891723"
LARGE_TYPES = {MULTINATIONAL: "multinational company", PUBLIC_COMPANY: "public company",
               "Q778575": "conglomerate", "Q1631111": "holding company"}
SKIP_TYPES = {"Q4167836", "Q4167410", "Q13406463"}   # category, disambiguation, list
NON_WIKIPEDIA = {"commonswiki", "specieswiki", "metawiki", "wikidatawiki", "mediawikiwiki",
                 "sourceswiki", "outreachwiki", "incubatorwiki"}
_LEGAL_TAIL = re.compile(r"[,\s]+(llc|l\.l\.c\.|llp|inc\.?|ltd\.?|pllc|p\.c\.|pc|plc|corp\.?|co\.|gmbh|s\.c\.)\s*$", re.I)


class WikidataError(RuntimeError):
    pass


def _values(claims: dict, prop: str) -> list:
    out = []
    for claim in claims.get(prop, []):
        value = (claim.get("mainsnak") or {}).get("datavalue", {}).get("value")
        if value is not None:
            out.append(value)
    return out


def wikipedia_count(sitelinks: dict) -> int:
    return sum(1 for key in sitelinks if key.endswith("wiki") and key not in NON_WIKIPEDIA)


def site_matches(sites: list[str], domain: str, business_name: str, entity_label: str) -> bool:
    reg = registered_domain(domain)
    label = domain_label(domain) or ""
    same_name = normalize_name(business_name) == normalize_name(entity_label)
    for url in sites:
        if not isinstance(url, str):
            continue
        if registered_domain(url) == reg:
            return True
        if same_name and len(label) >= 4 and domain_label(url) == label:
            return True
    return False


def entity_evidence(entity: dict) -> dict:
    claims = entity.get("claims") or {}
    employees = []
    for value in _values(claims, "P1128"):
        try:
            employees.append(int(float(str(value.get("amount", "0")).lstrip("+"))))
        except (TypeError, ValueError, AttributeError):
            continue
    types = [v.get("id") for v in _values(claims, "P31") if isinstance(v, dict)]
    return {
        "id": entity.get("id"),
        "label": ((entity.get("labels") or {}).get("en") or {}).get("value") or entity.get("id"),
        "employees": max(employees) if employees else None,
        "listed": bool(claims.get("P414")),
        "types": [LARGE_TYPES[t] for t in types if t in LARGE_TYPES],
        "wikipedias": wikipedia_count(entity.get("sitelinks") or {}),
    }


def merge_evidence(items: list[dict]) -> dict:
    """Several matching entities (e.g. a firm and its award): keep the strongest signals."""
    if not items:
        return {}
    best = max(items, key=lambda e: ((e["employees"] or 0), e["wikipedias"]))
    return {
        "id": best["id"],
        "label": best["label"],
        "employees": max((e["employees"] or 0 for e in items), default=0) or None,
        "listed": any(e["listed"] for e in items),
        "types": sorted({t for e in items for t in e["types"]}),
        "wikipedias": max(e["wikipedias"] for e in items),
    }


class WikidataClient:
    def __init__(self, cfg: Config, store: Store, transport: httpx.BaseTransport | None = None):
        self.cfg = cfg
        self.store = store
        self.cache_days = float(cfg.get("size_check.cache_days", 90))
        self.limiter = limiter_for(API, float(cfg.get("size_check.min_interval_seconds", 0.3)))
        self.client = api_client(cfg.user_agent, timeout=30, transport=transport)

    @property
    def available(self) -> bool:
        return bool(self.cfg.get("size_check.wikidata", True)) and bool(self.cfg.contact_email)

    def close(self) -> None:
        self.client.close()

    def _get(self, params: dict) -> dict:
        self.limiter.wait()
        try:
            resp = self.client.get(API, params={**params, "format": "json"})
        except httpx.HTTPError as exc:
            raise WikidataError(f"{type(exc).__name__}: {exc}") from exc
        if resp.status_code != 200:
            raise WikidataError(f"HTTP {resp.status_code}: {resp.text[:120]}")
        return resp.json()

    def _search(self, text: str) -> list[str]:
        data = self._get({"action": "wbsearchentities", "search": text[:120], "language": "en",
                          "limit": 5, "type": "item"})
        return [hit["id"] for hit in data.get("search", [])]

    def lookup(self, domain: str, name: str) -> dict:
        """Evidence dict for the business ({} if Wikidata has nothing matching)."""
        cached = self.store.cache_get("org", domain, self.cache_days)
        if cached is not None:
            return cached
        queries = [name.strip()]
        stripped = _LEGAL_TAIL.sub("", name).strip()
        if stripped and stripped != queries[0]:
            queries.append(stripped)
        ids: list[str] = []
        for q in queries:
            for qid in self._search(q):
                if qid not in ids:
                    ids.append(qid)
            if ids:
                break
        matches = []
        if ids:
            data = self._get({"action": "wbgetentities", "ids": "|".join(ids[:50]),
                              "props": "claims|sitelinks|labels", "languages": "en"})
            for entity in (data.get("entities") or {}).values():
                claims = entity.get("claims") or {}
                if {v.get("id") for v in _values(claims, "P31") if isinstance(v, dict)} & SKIP_TYPES:
                    continue
                label = ((entity.get("labels") or {}).get("en") or {}).get("value") or ""
                if site_matches(_values(claims, "P856"), domain, name, label):
                    matches.append(entity_evidence(entity))
        result = merge_evidence(matches)
        self.store.cache_put("org", domain, result)
        return result
