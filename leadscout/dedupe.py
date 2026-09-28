"""Company-level deduplication: every company is stored once.

Rules, applied in order across the whole database:

1. Same company domain: ``local.jacksonhewitt.com`` and ``jacksonhewitt.com`` are
   one company (subdomains on shared site builders such as ``*.wixsite.com``
   stay separate).
2. Redirects: a website that now redirects to another known company's site
   (kraftcpas.com -> bakertilly.com after an acquisition) is that company.
3. Company email: a listing without a website whose email is on a known
   company's domain belongs to that company.
4. Chain branches: listings with the same distinctive name that are flagged as
   a chain (brand tag, known chain, or 3+ places) are one company with several
   locations. Generic names made only of trade words ("Accounting & Tax
   Services") and different websites under one name are never merged.

The same-phone and same-name-within-100 m rules run when a listing is added
(see Store.upsert_business).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from leadscout.db.store import Store
from leadscout.normalize import company_domain, domain_matches
from leadscout.scoring.chains import ChainDetector

MIN_NAME_LENGTH = 6          # "cpa", "dental" ... are not distinctive enough to merge on


@dataclass
class Merge:
    kept: str
    dropped: str
    reason: str


def _preference(store: Store, row) -> tuple:
    emails = store.scalar("SELECT COUNT(*) FROM emails WHERE business_id = ?", (row["id"],)) or 0
    return (row["domain"] is not None, row["stage"] != "found", emails, -row["id"])


def _merge(store: Store, keep, drop, reason: str, merges: list[Merge]) -> None:
    store.merge_businesses(keep["id"], drop["id"])
    merges.append(Merge(keep["name"], drop["name"], reason))


def dedupe_companies(store: Store, detector: ChainDetector | None = None,
                     non_business_domains=()) -> list[Merge]:
    merges: list[Merge] = []

    # 1. company domain (collapse subdomains)
    for row in store.query("SELECT * FROM businesses WHERE website_status = 'ok' AND website IS NOT NULL"):
        row = store.get_business(row["id"])
        if row is None:
            continue
        target = company_domain(row["website"], store.shared_hosting)
        if not target or target == row["domain"]:
            continue
        other = store.one("SELECT * FROM businesses WHERE domain = ? AND id != ?", (target, row["id"]))
        if other is None:
            store.conn.execute("UPDATE businesses SET domain = ? WHERE id = ?", (target, row["id"]))
            store.conn.commit()
            continue
        keep, drop = sorted([row, other], key=lambda r: _preference(store, r), reverse=True)
        _merge(store, keep, drop, f"same website ({target})", merges)

    # 2. redirects to another known company's website
    for row in store.query("SELECT * FROM businesses WHERE final_url IS NOT NULL AND domain IS NOT NULL"):
        row = store.get_business(row["id"])
        if row is None or not row["final_url"]:
            continue
        target = company_domain(row["final_url"], store.shared_hosting)
        if not target or target == row["domain"] or domain_matches(target, non_business_domains):
            continue
        other = store.one("SELECT * FROM businesses WHERE domain = ?", (target,))
        if other is not None:
            _merge(store, other, row, f"{row['domain']} redirects to {target}", merges)

    # 3. listings without a website, emailing from a known company's domain
    for row in store.query("SELECT * FROM businesses WHERE domain IS NULL"):
        emails = store.query("SELECT email FROM emails WHERE business_id = ?", (row["id"],))
        for e in emails:
            email_domain = e["email"].rsplit("@", 1)[-1]
            if domain_matches(email_domain, store.freemail):
                continue
            target = company_domain(email_domain, store.shared_hosting)
            other = target and store.one("SELECT * FROM businesses WHERE domain = ?", (target,))
            if other is not None and store.get_business(row["id"]) is not None:
                _merge(store, other, row, f"uses the company email domain {target}", merges)
                break

    # 4. branches of a chain listed separately
    if detector is not None:
        groups: dict[str, list] = defaultdict(list)
        for row in store.query("SELECT * FROM businesses WHERE LENGTH(name_norm) >= ?", (MIN_NAME_LENGTH,)):
            groups[row["name_norm"]].append(row)
        for name_norm, rows in groups.items():
            if len(rows) < 2 or detector.is_generic(name_norm):
                continue    # "Accounting & Tax Services" in two towns are two companies
            total = sum(int(r["location_count"] or 1) for r in rows)
            if not any(detector.detect(r, {name_norm: total})[0] for r in rows):
                continue
            rows = sorted(rows, key=lambda r: _preference(store, r), reverse=True)
            keep = rows[0]
            for drop in rows[1:]:
                if drop["domain"] and drop["domain"] != keep["domain"]:
                    continue        # a different website under the same name: not provably the same
                if drop["domain"] and keep["domain"] == drop["domain"]:
                    continue        # impossible (unique), kept for clarity
                _merge(store, keep, drop, f"branch of the chain '{keep['name']}'", merges)
                keep = store.get_business(keep["id"])
    return merges
