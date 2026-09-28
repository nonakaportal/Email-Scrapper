"""Run orchestration. Stages: discover -> flag chains -> crawl -> verify -> score.

Each stage reads its pending work from the database and writes results back
as it goes, so a run can be interrupted at any point and resumed later
without losing or duplicating work.
"""

from __future__ import annotations

import asyncio
import logging
from itertools import zip_longest
from dataclasses import asdict
from pathlib import Path
from typing import Protocol

import httpx

from leadscout.config import Config
from leadscout.crawler.extract import EMAIL_RE, clean_email
from leadscout.crawler.fetcher import Fetcher
from leadscout.crawler.people import inbox_designation, match_person, not_the_business, owner_of, people_list
from leadscout.crawler.pages import SiteResult, crawl_site
from leadscout.db.store import Store, loads, utcnow
from leadscout.dedupe import Merge, dedupe_companies
from leadscout.industries import (
    IndustryRule,
    group_members,
    load_groups,
    load_rules,
    resolve_industry,
    resolve_many,
)
from leadscout.location import LocationError, LocationResolver, LocationSpec
from leadscout.normalize import SHARED_HOSTING, normalize_domain, normalize_phone, redirect_kind, registered_domain
from leadscout.scoring import ChainDetector, industry_words, score_lead
from leadscout.scoring.score import business_activity
from leadscout.sources.csv_import import read_csv
from leadscout.sources.overture import DownloadCancelled, OvertureError, OvertureSource
from leadscout.sources.wikidata import WikidataClient, WikidataError
from leadscout.verify import EmailRules, verify_email
from leadscout.verify.guess import candidates, infer_pattern
from leadscout.verify.lists import contact_priority
from leadscout.verify.dns_check import DomainChecker, lookup_domain
from leadscout.verify.smtp_check import MailboxChecker

log = logging.getLogger("leadscout")

STAGES = ("discover", "crawl", "size", "verify", "score")
DNS_BATCH = 50


def run_stats(store: Store, run_id: int) -> dict:
    """Counts for a run, always derived from the database."""
    stats = store.run_stats(run_id)
    stats["found"] = int(store.scalar(
        "SELECT COALESCE(SUM(found), 0) FROM run_tasks WHERE run_id = ?", (run_id,)) or 0)
    stats["tasks_failed"] = int(store.scalar(
        "SELECT COUNT(*) FROM run_tasks WHERE run_id = ? AND status = 'failed'", (run_id,)) or 0)
    return stats


class _StopFinding(Exception):
    """Stop the business search early (max results reached or 'continue with what's found')."""


class RunPaused(Exception):
    """Raised when a stop was requested; the run can be resumed."""


class Progress(Protocol):
    def stage(self, name: str, total: int, done: int = 0) -> None: ...
    def advance(self, name: str, n: int = 1) -> None: ...
    def note(self, text: str) -> None: ...


class NullProgress:
    def stage(self, name: str, total: int, done: int = 0) -> None:
        pass

    def advance(self, name: str, n: int = 1) -> None:
        pass

    def note(self, text: str) -> None:
        pass


class RunManager:
    def __init__(self, cfg: Config, store: Store, progress: Progress | None = None, *,
                 source: OvertureSource | None = None,
                 resolver: LocationResolver | None = None,
                 fetch_transport: httpx.AsyncBaseTransport | None = None,
                 dns_lookup=None,
                 wikidata: WikidataClient | None = None,
                 mailbox_checker: MailboxChecker | None = None):
        self.cfg = cfg
        self.store = store
        self.progress = progress or NullProgress()
        self.rules = load_rules(cfg.industries)
        self.groups = load_groups(cfg.industries)
        self.email_rules = EmailRules(cfg.email_rules)
        self._source = source
        self._resolver = resolver
        self._fetch_transport = fetch_transport
        self._dns_lookup = dns_lookup
        self._wikidata = wikidata
        self._mailbox_checker = mailbox_checker
        self.stop_requested = False
        self.skip_discovery = False     # "continue with the businesses found so far"
        # What counts as "the same company" when records are added or merged.
        store.shared_hosting = tuple(SHARED_HOSTING) + tuple(cfg.get("shared_hosting_domains") or ())
        store.freemail = set(self.email_rules.freemail)

    # -------------------------------------------------------------- helpers

    @property
    def resolver(self) -> LocationResolver:
        if self._resolver is None:
            self._resolver = LocationResolver(self.cfg, self.store)
        return self._resolver

    @property
    def source(self) -> OvertureSource:
        if self._source is None:
            self._source = OvertureSource(self.cfg, self.store)
        return self._source

    @property
    def wikidata(self) -> WikidataClient:
        if self._wikidata is None:
            self._wikidata = WikidataClient(self.cfg, self.store)
        return self._wikidata

    def close(self) -> None:
        for client in (self._resolver, self._source, self._wikidata):
            if client is not None:
                client.close()

    def request_skip_discovery(self) -> None:
        """Stop the map search early and continue the run with what was found."""
        self.skip_discovery = True

    def request_stop(self) -> None:
        self.stop_requested = True

    def _check_stop(self) -> None:
        if self.stop_requested:
            raise RunPaused()

    def _verification_mode(self, mode: str | None) -> str:
        """basic = syntax + mail domain; full = also ask the mail server if the mailbox exists."""
        mode = (mode or ("full" if self.cfg.get("email.smtp_check", False) else "basic")).strip().lower()
        if mode not in ("basic", "full"):
            raise ValueError(f"Verification must be 'basic' or 'full', not '{mode}'.")
        return mode

    def _default_filters(self, **overrides) -> dict:
        filters = dict(self.cfg.get("filters"))
        filters.update({k: v for k, v in overrides.items() if v is not None})
        return filters

    # ------------------------------------------------------------- creation

    def resolve_industries(self, words: list[str]) -> list[IndustryRule]:
        """Industry words and groups (tier1, top40) -> rules, in the order given."""
        return resolve_many(words, self.rules, self.groups)

    def create_search_run(self, industries: list[str], locations: list[LocationSpec], *,
                          max_results: int | None = None, require_website: bool | None = None,
                          exclude_chains: bool | None = None, verification: str | None = None) -> int:
        if not industries:
            raise ValueError("Give at least one industry.")
        if not locations:
            raise ValueError("Give at least one location.")
        rules = self.resolve_industries(industries)
        filters = self._default_filters(max_results=max_results, require_website=require_website,
                                        exclude_chains=exclude_chains)
        settings = {
            "filters": filters,
            "industries": [asdict(r) for r in rules],
            "locations": [asdict(loc) for loc in locations],
            "verification": self._verification_mode(verification),
        }
        # Show groups as typed ("tier1") and single industries by their key.
        shown = []
        for word in industries:
            if group_members(word, self.rules, self.groups) is not None:
                shown.append(word.strip().lower().replace(" ", ""))
            else:
                shown.append(resolve_industry(word, self.rules).key)
        location_type = "batch" if len(locations) > 1 else locations[0].type
        run_id = self.store.create_run(
            kind="search",
            industries=list(dict.fromkeys(shown)),
            location_input="; ".join(loc.label() for loc in locations),
            location_type=location_type,
            sources=["overture"],
            settings=settings,
        )
        self.store.add_run_tasks(run_id, [(loc.key(), r.key) for loc in locations for r in rules])
        self._fresh_log(run_id)
        return run_id

    def create_enrich_run(self, csv_path: str | Path, industry: str | None = None,
                          verification: str | None = None) -> int:
        candidates = read_csv(csv_path)
        rule = resolve_industry(industry, self.rules) if industry else None
        filters = self._default_filters(exclude_chains=False, max_results=None)
        run_id = self.store.create_run(
            kind="enrich",
            industries=[rule.key] if rule else [],
            location_input=str(csv_path),
            location_type="csv",
            sources=["csv"],
            settings={"filters": filters, "industries": [asdict(rule)] if rule else [],
                      "verification": self._verification_mode(verification)},
        )
        non_business = self.cfg.get("non_business_domains") or []
        for cand in candidates:
            if rule and not cand.industry:
                cand.industry = rule.key
            bid, _ = self.store.upsert_business(cand, run_id=run_id,
                                                non_business_domains=non_business)
            self._add_listed_emails(bid, cand.emails, "csv")
            self.store.link_business(run_id, bid, cand.industry, cand.city)
        self._fresh_log(run_id)
        return run_id

    def _fresh_log(self, run_id: int) -> None:
        """A new run may reuse the number of a deleted one; don't show that run's old log."""
        (self.cfg.log_dir / f"run_{run_id}.log").unlink(missing_ok=True)

    @property
    def own_emails(self) -> set[str]:
        """Your own addresses (contact email, anything in the User-Agent). Some websites echo
        the visitor's User-Agent into the page, e.g. a hidden contact-form field; those must
        never become leads."""
        own = {self.cfg.contact_email.lower()} if self.cfg.contact_email else set()
        own |= {m.group(1).lower() for m in EMAIL_RE.finditer(self.cfg.user_agent)}
        return own

    @property
    def max_contacts(self) -> int | None:
        value = self.cfg.get("filters.max_contacts_per_company")
        return int(value) if value else None

    def _pick_contacts(self, business_id: int, candidates: list[tuple[str, str, str | None]],
                       decision_makers: set[str] = frozenset()) -> list[tuple[str, str, str | None]]:
        """Which new (email, type, found_on) candidates to save: the company keeps its best
        emails to contact (see contact_priority) up to the per-company limit. A better new
        email replaces a weaker saved one, unless that one was already exported."""
        existing = self.store.emails_for_business(business_id)
        have = {r["email"] for r in existing}
        fresh = [c for c in candidates if c[0] not in have and c[0].lower() not in self.own_emails]
        limit = self.max_contacts
        if limit is None or not fresh:
            return fresh
        pool = [(contact_priority({**dict(r), "decision_maker": r["decision_maker"] or r["email"] in decision_makers}),
                 "old", r) for r in existing]
        pool += [(contact_priority({"email": c[0], "email_type": c[1], "found_on_url": c[2],
                                    "decision_maker": c[0] in decision_makers,
                                    "is_freemail": self.email_rules.is_freemail(c[0])}), "new", c)
                 for c in fresh]
        pool.sort(key=lambda t: t[0])
        exported = {r[0] for r in self.store.query(
            "SELECT DISTINCT email_id FROM export_items WHERE email_id IN (SELECT id FROM emails WHERE business_id = ?)",
            (business_id,))}
        keep: list = []
        for _, kind, item in pool:            # exported emails always stay
            if kind == "old" and item["id"] in exported:
                keep.append((kind, item))
        for _, kind, item in pool:
            if len(keep) >= limit:
                break
            if not (kind == "old" and item["id"] in exported):
                keep.append((kind, item))
        kept_old = {item["id"] for kind, item in keep if kind == "old"}
        for r in existing:
            if r["id"] not in kept_old:
                log.info("Business %s: %s replaced by a better email to contact", business_id, r["email"])
                self.store.conn.execute("DELETE FROM emails WHERE id = ?", (r["id"],))
        return [item for kind, item in keep if kind == "new"]

    def _add_listed_emails(self, business_id: int, emails: list[str], source: str) -> None:
        if not emails:
            return
        business = self.store.get_business(business_id)
        site_urls = [business["website"]] if business["website_status"] == "ok" else []
        candidates = []
        for raw in emails:
            email = clean_email(raw)
            if email and not self.email_rules.is_junk(email):
                candidates.append((email, self.email_rules.email_type(email, business["name"], site_urls), None))
        for email, email_type, _ in self._pick_contacts(business_id, candidates):
            self.store.add_email(business_id, email, email_type=email_type, source=source,
                                 found_on_url=None, is_freemail=self.email_rules.is_freemail(email))
        with self.store.tx():
            if source != "csv" and business["listing_updated"]:
                self.store.conn.execute(
                    "UPDATE emails SET seen_at = ? WHERE business_id = ? AND source = ? AND seen_at IS NULL",
                    (business["listing_updated"], business_id, source))
            self._assign_contacts(business_id, [])

    # --------------------------------------------------------------- preview

    def preview(self, industries: list[str], locations: list[LocationSpec]) -> list[dict]:
        """Map search only: how many businesses exist, before crawling."""
        rules = self.resolve_industries(industries)
        rows = []
        for spec in locations:
            try:
                loc = self.resolver.resolve(spec)
                found = self.source.search_many(loc, rules)
            except (LocationError, OvertureError) as exc:
                rows.append({"location": spec.label(), "error": str(exc)})
                continue
            failed = getattr(self.source, "failed", {}) or {}
            for rule in rules:
                if rule.key in failed:
                    rows.append({"location": spec.label(), "industry": rule.label, "rank": rule.rank,
                                 "error": failed[rule.key]})
                    continue
                cands = found[rule.key]
                rows.append({
                    "location": spec.label(),
                    "resolved": loc.display_name,
                    "note": loc.note,
                    "industry": rule.label,
                    "rank": rule.rank,
                    "businesses": len(cands),
                    "with_website": sum(1 for c in cands if c.website),
                    "with_email": sum(1 for c in cands if c.emails),
                    "branded": sum(1 for c in cands if c.brand),
                })
        return rows

    # ------------------------------------------------------------- execution

    def execute(self, run_id: int) -> dict:
        run = self.store.get_run(run_id)
        if run is None:
            raise ValueError(f"Run #{run_id} does not exist.")
        handler = self._attach_log(run_id)
        self.stop_requested = False
        self.skip_discovery = False
        self.store.update_run(run_id, status="running", error=None)
        log.info("Run #%s started (%s: %s / %s)", run_id, run["kind"],
                 ", ".join(run["industries"]) or "-", run["location_input"])
        status = "running"
        try:
            if run["kind"] == "search":
                self.store.update_run(run_id, stage="discover")
                self._discover(run)
            self._dedupe()
            self._flag_chains(run_id)
            self._check_stop()
            self.store.update_run(run_id, stage="crawl")
            self._crawl(run)
            self._dedupe()          # websites that turned out to redirect to a known company
            self._check_stop()
            # Only businesses with an email can become leads, so only those are
            # looked up for company size (much faster than checking everything).
            self.store.update_run(run_id, stage="size")
            self._size_check(run_id)
            self._flag_chains(run_id)
            self._check_stop()
            self.store.update_run(run_id, stage="verify")
            self._verify(run_id)
            self._check_stop()
            self.store.update_run(run_id, stage="score")
            self._score(run_id)
            status = "done"
        except (RunPaused, KeyboardInterrupt, asyncio.CancelledError):
            status = "paused"
            log.info("Run #%s paused; resume with: leadscout resume %s", run_id, run_id)
        except Exception as exc:
            status = "failed"
            log.exception("Run #%s failed", run_id)
            self.store.update_run(run_id, error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            stats = self.stats(run_id)
            self.store.update_run(run_id, status=status, stats=stats)
            log.info("Run #%s %s: %s", run_id, status, stats)
            logging.getLogger("leadscout").removeHandler(handler)
            handler.close()
        return stats

    def stats(self, run_id: int) -> dict:
        return run_stats(self.store, run_id)

    def _attach_log(self, run_id: int) -> logging.Handler:
        log_dir = self.cfg.log_dir
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_dir / f"run_{run_id}.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root = logging.getLogger("leadscout")
        root.setLevel(logging.INFO)
        root.addHandler(handler)
        return handler

    # ---------------------------------------------------------- stage: find

    def _rule(self, key: str, saved: dict[str, dict]) -> IndustryRule:
        """The industry's current definition (older runs saved other fields)."""
        if key in self.rules:
            return self.rules[key]
        known = set(IndustryRule.__dataclass_fields__)
        spec = saved.get(key)
        if spec:
            return IndustryRule(**{k: v for k, v in spec.items() if k in known})
        return resolve_industry(key, self.rules)

    def _discover(self, run: dict) -> None:
        """Find businesses for each place (Overture Maps), saving them tile by tile."""
        run_id = run["id"]
        settings = run["settings"]
        filters = settings.get("filters", {})
        saved = {r["key"]: r for r in settings.get("industries", []) if isinstance(r, dict) and r.get("key")}
        tasks = self.store.run_tasks(run_id)
        todo = [t for t in tasks if t["status"] in ("pending", "failed")]
        self.progress.stage("discover", total=len(tasks), done=len(tasks) - len(todo))

        by_location: dict[str, list[str]] = {}
        for task in todo:
            by_location.setdefault(task["location"], []).append(task["industry"])

        for loc_key, industry_keys in by_location.items():
            self._check_stop()
            spec = LocationSpec.from_key(loc_key)
            if self.skip_discovery:
                for key in industry_keys:
                    self.store.set_task(run_id, loc_key, key, status="skipped", error="skipped by user")
                self.progress.advance("discover", len(industry_keys))
                continue
            loc_rules = [self._rule(k, saved) for k in industry_keys]
            if self._full(run_id, filters):
                for rule in loc_rules:
                    self.store.set_task(run_id, loc_key, rule.key, status="skipped",
                                        error="max results reached")
                self.progress.advance("discover", len(loc_rules))
                continue
            found_counts = {r.key: 0 for r in loc_rules}
            try:
                loc = self.resolver.resolve(spec)
                if loc.note:
                    self.progress.note(loc.note)
                    log.warning(loc.note)
                tiles = self.source.tiles(loc)
                self.progress.stage("discover", total=max(1, len(tiles)))
                if len(tiles) > 4:
                    self.progress.note(f"{spec.label()} is a big area: downloading it in {len(tiles)} "
                                       "parts (businesses are saved as each part finishes).")

                def on_tile(done: int, total: int, found: dict) -> None:
                    self._absorb(run_id, filters, spec, loc, loc_rules, found, found_counts)
                    self.progress.advance("discover")
                    self._check_stop()
                    if self.skip_discovery or self._full(run_id, filters):
                        raise _StopFinding()

                self.source.should_stop = lambda: self.stop_requested or self.skip_discovery
                try:
                    self.source.search_many(loc, loc_rules, on_tile=on_tile)
                except (_StopFinding, DownloadCancelled):
                    pass
                self._check_stop()          # paused mid-download: stop now; resume redoes this place
            except (LocationError, OvertureError) as exc:
                # Whatever was found before the error is already saved; a resume retries the rest.
                log.error("discover %s: %s", spec.label(), exc)
                self.progress.note(f"{spec.label()}: {exc}")
                for rule in loc_rules:
                    self.store.set_task(run_id, loc_key, rule.key, status="failed", error=str(exc))
                continue

            total = sum(found_counts.values())
            log.info("discover %s: %d businesses across %d industries",
                     spec.label(), total, len(loc_rules))
            if total == 0:
                self.progress.note(
                    f"No businesses found in {spec.label()}. Try a larger place or another industry.")
            for rule in loc_rules:
                self.store.set_task(run_id, loc_key, rule.key, status="done", found=found_counts[rule.key])

    def _full(self, run_id: int, filters: dict) -> bool:
        max_results = filters.get("max_results")
        return bool(max_results) and self.store.run_business_count(run_id) >= max_results

    def _absorb(self, run_id, filters, spec, loc, loc_rules, found, found_counts) -> None:
        """Save one map search's businesses right away (so progress is never lost)."""
        require_website = filters.get("require_website", True)
        non_business = self.cfg.get("non_business_domains") or []
        # Reachable businesses first, and industries taken in turn, so a
        # max-results cap is not filled by the first industry alone.
        ordered = [sorted(found.get(r.key, []), key=lambda c: (not c.website, not c.emails))
                   for r in loc_rules]
        for rule in loc_rules:
            found_counts[rule.key] = found_counts.get(rule.key, 0) + len(found.get(rule.key, []))
        for batch in zip_longest(*ordered):
            for cand in batch:
                if cand is None:
                    continue
                if self._full(run_id, filters):
                    return
                bid, _ = self.store.upsert_business(
                    cand, run_id=run_id, non_business_domains=non_business,
                    default_country=loc.country)
                self._add_listed_emails(bid, cand.emails, cand.source)
                business = self.store.get_business(bid)
                keep = (business["website_status"] == "ok"
                        or not require_website
                        or self.store.emails_for_business(bid))
                if keep:
                    self.store.link_business(run_id, bid, cand.industry, spec.label())

    # -------------------------------------------------------- stage: chains

    def _chain_detector(self) -> ChainDetector:
        return ChainDetector(self.cfg.known_chains,
                             self.cfg.get("filters.chain_name_repeat_threshold", 3),
                             size=self.cfg.get("size_check") or {},
                             generic_words=industry_words(self.cfg.industries))

    # ---------------------------------------------------------- stage: size

    def _size_check(self, run_id: int) -> None:
        """Look up businesses on Wikidata to catch large and multinational companies."""
        if not self.cfg.get("size_check.wikidata", True):
            return
        if not self.wikidata.available:
            self.progress.note("Large-company check skipped: set contact_email in Settings "
                               "(Wikidata requires a contact address).")
            return
        has_email = "EXISTS (SELECT 1 FROM emails e WHERE e.business_id = b.id)"
        todo = self.store.run_businesses(
            run_id, f"b.domain IS NOT NULL AND b.org_info IS NULL AND COALESCE(b.is_chain, 0) = 0 AND {has_email}")
        done = len(self.store.run_businesses(
            run_id, f"b.domain IS NOT NULL AND b.org_info IS NOT NULL AND {has_email}"))
        self.progress.stage("size", total=done + len(todo), done=done)
        failures = 0
        for row in todo:
            self._check_stop()
            try:
                info = self.wikidata.lookup(row["domain"], row["name"])
            except WikidataError as exc:
                failures += 1
                log.warning("size check %s: %s", row["domain"], exc)
                if failures >= 3:
                    self.progress.note(f"Wikidata is not responding ({exc}); large-company check "
                                       "skipped for the rest of this run. Re-check it later.")
                    return
                continue
            failures = 0
            self.store.update_business(row["id"], org_info=info)
            if info:
                log.info("size check %s: %s", row["domain"], info)
            self.progress.advance("size")

    def _dedupe(self) -> list[Merge]:
        merges = dedupe_companies(self.store, self._chain_detector(),
                                  self.cfg.get("non_business_domains") or [])
        for m in merges:
            log.info("merged '%s' into '%s': %s", m.dropped, m.kept, m.reason)
        if merges:
            self.progress.note(f"Merged {len(merges)} duplicate listings into existing companies.")
        return merges

    def dedupe_database(self) -> list[Merge]:
        """One-time cleanup of the whole database, then re-flag and rescore every run."""
        merges = self._dedupe()
        for run in self.store.list_runs(100000):
            self._flag_chains(run["id"])
            self._score(run["id"])
            self.store.update_run(run["id"], stats=self.stats(run["id"]))
        return merges

    def recheck(self, run_id: int) -> dict:
        """Re-run the chain / large-company checks and scoring for an existing run."""
        run = self.store.get_run(run_id)
        if run is None:
            raise ValueError(f"Run #{run_id} does not exist.")
        handler = self._attach_log(run_id)
        self.stop_requested = False
        try:
            self._dedupe()
            self._flag_chains(run_id)
            self._size_check(run_id)
            self._score(run_id)
        except (RunPaused, KeyboardInterrupt):
            log.info("Re-check of run #%s stopped", run_id)
        finally:
            stats = self.stats(run_id)
            self.store.update_run(run_id, stats=stats)
            log.info("Run #%s re-checked: %s", run_id, stats)
            logging.getLogger("leadscout").removeHandler(handler)
            handler.close()
        return stats

    def _flag_chains(self, run_id: int) -> None:
        detector = self._chain_detector()
        rows = self.store.run_businesses(run_id)
        counts = self.store.name_location_counts(r["name_norm"] for r in rows)
        with self.store.tx():
            for row in rows:
                is_chain, reason = detector.detect(row, counts)
                if bool(row["is_chain"]) != is_chain or row["chain_reason"] != reason:
                    self.store.conn.execute(
                        "UPDATE businesses SET is_chain = ?, chain_reason = ? WHERE id = ?",
                        (int(is_chain), reason, row["id"]))

    # --------------------------------------------------------- stage: crawl

    def _crawl(self, run: dict) -> None:
        run_id = run["id"]
        exclude_chains = run["settings"].get("filters", {}).get("exclude_chains", True)
        chain_sql = " AND COALESCE(b.is_chain, 0) = 0" if exclude_chains else ""
        todo = self.store.run_businesses(
            run_id, "b.stage = 'found' AND b.website_status = 'ok'" + chain_sql)
        done = self.store.run_businesses(
            run_id, "b.stage != 'found' AND b.website_status = 'ok'" + chain_sql)
        self.progress.stage("crawl", total=len(todo) + len(done), done=len(done))
        if todo:
            asyncio.run(self._crawl_async(todo))
        self._check_stop()

    async def _crawl_async(self, rows) -> None:
        c = self.cfg.get("crawler")
        fetcher = Fetcher(self.cfg, transport=self._fetch_transport)
        sem = asyncio.Semaphore(int(c["global_concurrency"]))
        max_pages = int(c["max_pages_per_site"])

        async def one(row) -> None:
            async with sem:
                if self.stop_requested:
                    return
                try:
                    res = await crawl_site(fetcher, row["website"], max_pages=max_pages,
                                           region=row["country"])
                except Exception as exc:  # noqa: BLE001 - one bad site must not stop the run
                    log.exception("crawl %s crashed", row["website"])
                    res = SiteResult(start_url=row["website"], error=f"crawler error: {exc}")
                self._save_crawl(row, res)
                self.progress.advance("crawl")

        try:
            await asyncio.gather(*(one(r) for r in rows))
        finally:
            await fetcher.aclose()

    DOMAIN_GONE = "website domain does not exist (NXDOMAIN)"

    def _confirm_dead_domain(self, error: str | None, domain: str | None) -> str | None:
        """A failed name lookup can be a hiccup; only a DNS "no such domain" means the domain is gone."""
        if not error or not domain or "getaddrinfo" not in error:
            return error
        status, reason = (self._dns_lookup or lookup_domain)(domain)
        return self.DOMAIN_GONE if reason == "domain does not exist" else error

    def _save_crawl(self, row, res: SiteResult) -> None:
        rules = self.email_rules
        site_urls = [u for u in (row["website"], res.final_url) if u]
        kept = 0
        with self.store.tx():
            for page in res.pages:
                self.store.record_page(page.final_url or page.url,
                                       normalize_domain(page.final_url or page.url),
                                       page.status, page.error)
            moved = redirect_kind(row["website"], res.final_url, row["name"])
            redirected_to = registered_domain(res.final_url) if moved in ("parked", "other") else None
            if redirected_to:
                # The website now belongs to someone else (sold, merged, expired, taken over):
                # nothing on it is about this business.
                log.info("%s: website now goes to %s (%s); its contents are ignored",
                         row["name"], redirected_to, moved)
                own = registered_domain(row["website"])     # addresses on its own old domain still count
                res.emails = {e: u for e, u in res.emails.items() if registered_domain(e.split("@")[-1]) == own}
                res.people, res.phones, res.socials = [], [], {}
                res.copyright_year = res.latest_date_year = res.team_size_hint = None
                res.closed_notice = (f"the website's domain expired and is for sale ({redirected_to})"
                                     if moved == "parked" else None)
            found = dict(res.emails)
            for person in res.people:          # addresses only given in structured data
                if person.email and person.email not in found:
                    found[person.email] = person.url
            # Addresses the map already listed, now also seen on the website: record the page.
            for email, page_url in found.items():
                if page_url:
                    self.store.conn.execute(
                        "UPDATE emails SET found_on_url = COALESCE(found_on_url, ?), seen_at = ?"
                        " WHERE business_id = ? AND email = ?", (page_url, utcnow()[:10], row["id"], email))
            candidates = [(email, rules.email_type(email, row["name"], site_urls), page_url)
                          for email, page_url in found.items()
                          if not rules.is_junk(email) and rules.is_related(email, site_urls, row["name"])]
            res.people = not_the_business(res.people, row["name"])
            owner = owner_of(res.people, row["name"], row["domain"])
            people = list(res.people)
            if owner and owner[0] not in people:
                people.append(owner[0])
            owner_person = owner[0] if owner else None
            deciders = {email for email, _, _ in candidates
                        if (p := match_person(email, people)) and (p.decision_maker or p is owner_person)}
            for email, email_type, page_url in self._pick_contacts(row["id"], candidates, deciders):
                cur = self.store.conn.execute(
                    """INSERT OR IGNORE INTO emails (business_id, email, email_type, source,
                       found_on_url, is_freemail, seen_at, created_at)
                       VALUES (?, ?, ?, 'website', ?, ?, ?, ?)""",
                    (row["id"], email, email_type, page_url, int(rules.is_freemail(email)),
                     utcnow()[:10], utcnow()))
                kept += cur.rowcount
            fields = {
                "final_url": res.final_url,
                "platform": res.platform,
                "socials": {**(loads(row["socials"], {}) or {}), **(res.socials or {})} or None,
                "copyright_year": res.copyright_year,
                "latest_date_year": res.latest_date_year,
                "closed_notice": res.closed_notice,
                "redirected_to": redirected_to,
                "team_size_hint": res.team_size_hint,
                "has_careers": res.has_careers,
                "multi_location_site": res.multi_location,
                "intl_languages": res.intl_languages,
                "investor_relations": res.investor_relations,
                "stage": "crawled",
                "error": self._confirm_dead_domain(res.error, row["domain"]),
            }
            if not row["phone"] and res.phones:
                fields["phone"] = normalize_phone(res.phones[0], row["country"]) or res.phones[0]
            if owner:
                fields.update(owner_name=owner[0].name, owner_title=owner[0].title, owner_basis=owner[1])
            if people:
                for email in found:                      # anna@ -> Anna Lopez, when not listed beside her
                    person = match_person(email, people)
                    if person is not None and not person.email:
                        person.email = email
                fields["people"] = people_list(people, owner_person)
            self.store._update("businesses", row["id"], fields)
            self._assign_contacts(row["id"], people, owner_person)
        if res.error:
            log.warning("crawl %s: %s", row["website"], res.error)
        else:
            log.info("crawl %s: %d pages, %d emails kept", row["website"], len(res.pages), kept)

    def _assign_contacts(self, business_id: int, people: list, owner=None) -> None:
        """Name and designation for each of the company's addresses: the person named on the
        website, else the inbox's role from the address (sales@ -> Sales). Never guessed."""
        for e in self.store.emails_for_business(business_id):
            person = match_person(e["email"], people) if people else None
            if person:
                self.store.set_contact(e["id"], person.name, person.title,
                                       person.decision_maker or person is owner, "website")
            elif not e["contact_name"]:
                role = inbox_designation(e["email"])
                if role and not e["designation"]:
                    self.store.set_contact(e["id"], None, role, False, "address")

    # -------------------------------------------------------- stage: verify

    def _verify(self, run_id: int) -> None:
        todo = self.store.run_emails(run_id, "e.status IS NULL")
        done = int(self.store.scalar(
            "SELECT COUNT(*) FROM emails e JOIN run_businesses rb ON rb.business_id = e.business_id"
            " WHERE rb.run_id = ? AND e.status IS NOT NULL", (run_id,)) or 0)
        self.progress.stage("verify", total=done + len(todo), done=done)
        check_mx = bool(self.cfg.get("email.verify_mx", True))
        checker = DomainChecker(self.cfg, self.store, lookup=self._dns_lookup)

        by_domain: dict[str, list] = {}
        for row in todo:
            by_domain.setdefault(row["email"].rsplit("@", 1)[-1], []).append(row)
        domains = list(by_domain)
        for i in range(0, len(domains), DNS_BATCH):
            self._check_stop()
            batch = domains[i:i + DNS_BATCH]
            results = checker.check_many(batch) if check_mx else {}
            for domain in batch:
                for row in by_domain[domain]:
                    status, reason = verify_email(row["email"], self.email_rules,
                                                  results.get(domain) if check_mx else None)
                    self.store.set_email_status(row["id"], status, reason)
                    self.progress.advance("verify")
        run = self.store.get_run(run_id)
        if (run["settings"] or {}).get("verification") == "full":
            self._mailbox_check(run_id)
            self._guess_emails(run_id)
        with self.store.tx():
            self.store.conn.execute(
                "UPDATE businesses SET stage = 'verified' WHERE stage = 'crawled' AND id IN"
                " (SELECT business_id FROM run_businesses WHERE run_id = ?)", (run_id,))

    @property
    def mailbox_checker(self) -> MailboxChecker:
        if self._mailbox_checker is None:
            self._mailbox_checker = MailboxChecker(self.cfg, self.store)
        return self._mailbox_checker

    def _mailbox_check(self, run_id: int, retry_unknown: bool = False) -> int:
        """Ask the mail servers whether valid-looking company addresses really exist.
        Free-mail addresses (gmail.com ...) are skipped: those servers don't answer."""
        where = "e.status = 'valid' AND (e.mailbox_check IS NULL"
        where += " OR e.mailbox_check = 'unknown')" if retry_unknown else ")"
        todo = [r for r in self.store.run_emails(run_id, where)
                if not self.email_rules.is_freemail(r["email"])]
        if not todo:
            return 0
        checker = self.mailbox_checker
        if not checker.available():
            self.progress.note("Mailbox check skipped: this network blocks outbound port 25 (most home "
                               "internet and cloud hosts do). Emails keep the basic check.")
            return 0
        rows: dict[str, list] = {}
        for row in todo:
            rows.setdefault(row["email"].rsplit("@", 1)[-1].lower(), []).append(row)
        self.progress.stage("mailbox", total=len(todo))
        log.info("Mailbox check: %d addresses on %d domains", len(todo), len(rows))
        checked = 0
        by_domain = {d: [r["email"] for r in rs] for d, rs in rows.items()}
        for domain, results in checker.check_many(by_domain, should_stop=lambda: self.stop_requested):
            for row in rows[domain]:
                if row["email"] not in results:
                    continue
                answer, why = results[row["email"]]
                if answer == "unknown":             # inconclusive: keep the basic result
                    status, reason = row["status"], f"{row['status_reason']}; mailbox not confirmed: {why}"
                else:
                    status, reason = answer, why
                self.store.set_email_status(row["id"], status, reason,
                                            is_catch_all=True if answer == "risky" else
                                            (False if answer in ("valid", "invalid") else None))
                with self.store.tx():
                    self.store._update("emails", row["id"], {"mailbox_check": answer})
                checked += 1
                self.progress.advance("mailbox")
        self._check_stop()
        return checked

    def _guess_emails(self, run_id: int) -> int:
        """Emails for decision makers named on the website without a published address:
        candidates from the company's own format (rebecca@ -> chris@), kept only when the
        company's mail server confirms the mailbox exists. Catch-all servers confirm
        nothing, so nothing is kept for them."""
        if not self.cfg.get("email.guess_emails", True):
            return 0
        targets = []
        rows = self.store.run_businesses(
            run_id, "b.website_status = 'ok' AND b.people IS NOT NULL AND COALESCE(b.is_chain, 0) = 0"
                    " AND b.redirected_to IS NULL AND b.domain IS NOT NULL")
        for b in rows:
            domain = b["domain"]
            if self.email_rules.is_freemail(f"x@{domain}"):
                continue
            emails = self.store.emails_for_business(b["id"])
            have = {e["email"] for e in emails}
            named = {(e["contact_name"] or "").lower() for e in emails}
            written = [(e["email"], e["contact_name"]) for e in emails if e["source"] != "guessed"
                       and registered_domain(e["email"].rsplit("@", 1)[-1]) == registered_domain(domain)]
            learned = infer_pattern(written)
            leaders = [p for p in loads(b["people"], []) or []
                       if (p.get("level") or 9) <= 2 and not p.get("email") and p["name"].lower() not in named]
            for person in leaders[:2]:
                cands = [c for c in candidates(person["name"], domain, learned[0] if learned else None, limit=5)
                         if c[0] not in have]
                if cands:
                    targets.append((b, person, cands, learned))
        if not targets:
            return 0
        checker = self.mailbox_checker
        if not checker.available():
            self.progress.note("Guessing decision makers' emails skipped: port 25 is blocked, so the "
                               "mail servers can't confirm them.")
            return 0
        by_domain: dict[str, list[str]] = {}
        for b, _, cands, _ in targets:
            by_domain.setdefault(b["domain"], []).extend(e for e, _ in cands)
        self.progress.stage("guess", total=len(by_domain))
        log.info("Guessing emails for %d decision makers at %d companies", len(targets), len(by_domain))
        answers: dict[str, tuple[str, str]] = {}
        for _, results in checker.check_many(by_domain, should_stop=lambda: self.stop_requested):
            answers.update(results)
            self.progress.advance("guess")
        saved = 0
        today = utcnow()[:10]
        with self.store.tx():
            for b, person, cands, learned in targets:
                hit = next(((e, p) for e, p in cands if answers.get(e, ("",))[0] == "valid"), None)
                if hit is None:
                    continue
                email, pattern = hit
                basis = (f"the company's format ({pattern}@, like {learned[1]})" if learned and learned[0] == pattern
                         else f"the common {pattern}@ format")
                cur = self.store.conn.execute(
                    """INSERT OR IGNORE INTO emails (business_id, email, email_type, source, is_freemail,
                       seen_at, created_at) VALUES (?, ?, 'personal', 'guessed', 0, ?, ?)""",
                    (b["id"], email, today, utcnow()))
                if not cur.rowcount:
                    continue
                email_id = cur.lastrowid
                self.store.conn.execute(
                    "UPDATE emails SET contact_name = ?, designation = ?, decision_maker = 1,"
                    " designation_source = 'website', status = 'valid', status_reason = ?, is_catch_all = 0,"
                    " mailbox_check = 'valid', verified_at = ? WHERE id = ?",
                    (person["name"], person.get("title") or None,
                     f"guessed from {basis}; the mail server confirmed it exists", utcnow(), email_id))
                log.info("%s: %s (%s) -> %s, confirmed by the mail server", b["name"], person["name"],
                         person.get("title"), email)
                saved += 1
        self._check_stop()
        return saved

    def check_mailboxes(self, run_id: int) -> dict:
        """Mailbox check for an existing run (also retries inconclusive answers), then rescore."""
        run = self.store.get_run(run_id)
        if run is None:
            raise ValueError(f"Run #{run_id} does not exist.")
        handler = self._attach_log(run_id)
        self.stop_requested = False
        try:
            checked = self._mailbox_check(run_id, retry_unknown=True)
            log.info("Mailbox check of run #%s: %d addresses asked", run_id, checked)
            guessed = self._guess_emails(run_id)
            log.info("Run #%s: %d decision makers' emails guessed and confirmed", run_id, guessed)
            self._score(run_id)
        except (RunPaused, KeyboardInterrupt):
            log.info("Mailbox check of run #%s stopped", run_id)
        finally:
            stats = self.stats(run_id)
            self.store.update_run(run_id, stats=stats)
            logging.getLogger("leadscout").removeHandler(handler)
            handler.close()
        return stats

    # --------------------------------------------------------- stage: score

    def _score(self, run_id: int) -> None:
        self._flag_chains(run_id)   # crawl may have found a locations page
        weights = self.cfg.get("scoring")
        rows = self.store.run_businesses(run_id)
        self.progress.stage("score", total=len(rows))
        with self.store.tx():
            for b in rows:
                emails = self.store.emails_for_business(b["id"])
                best_score, best_detail = score_lead(b, None, weights)
                for e in emails:
                    s, detail = score_lead(b, e, weights)
                    self.store.set_email_score(e["id"], s, detail)
                    if s > best_score or best_detail is None:
                        best_score, best_detail = s, detail
                stage = b["stage"]
                if stage in ("crawled", "verified") or b["website_status"] != "ok":
                    stage = "scored"
                activity, why = business_activity(
                    b, emails, active_years=int(weights.get("website_active_years", 2)),
                    listing_months=int(self.cfg.get("filters.listing_fresh_months") or 18))
                self.store._update("businesses", b["id"], {
                    "score": best_score, "score_detail": best_detail, "stage": stage,
                    "activity": activity, "activity_reason": why})
                self.progress.advance("score")
