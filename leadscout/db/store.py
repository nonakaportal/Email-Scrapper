"""SQLite store. Every pipeline stage reads its pending work from here and
writes results back immediately, which is what makes runs resumable."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from leadscout.normalize import (
    SHARED_HOSTING,
    company_domain,
    domain_matches,
    haversine_m,
    normalize_domain,
    normalize_name,
    normalize_phone,
    normalize_url,
)
from leadscout.sources.base import BusinessCandidate

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# Same place mapped twice (node + building outline) is usually within this
# distance; further apart with the same domain/phone means another branch.
SAME_PLACE_METERS = 100
OTHER_BRANCH_METERS = 500

_JSON_COLUMNS = {"socials", "tags", "score_detail", "stats", "settings", "industries", "sources", "people"}


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _dump(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, bool):
        return int(value)
    return value


def loads(value: str | None, default: Any = None) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _social_profiles(urls: list[str]) -> dict[str, str]:
    """Profile URLs -> {network: url} (facebook, instagram, linkedin, x, youtube, tiktok)."""
    from leadscout.crawler.extract import find_socials   # the crawler's own rules
    return find_socials([(u if "://" in u else f"https://{u}", "") for u in urls if u])


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self.conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        self._migrate()
        # Used to decide when two records are the same company (see company_domain).
        self.shared_hosting: tuple[str, ...] = SHARED_HOSTING
        self.freemail: set[str] = set()
        self.conn.commit()

    # Columns added after the first release; older databases get them here.
    _ADDED_COLUMNS = {
        "emails": {
            "contact_name": "TEXT",
            "designation": "TEXT",
            "decision_maker": "INTEGER DEFAULT 0",
            "designation_source": "TEXT",
            "mailbox_check": "TEXT",
            "seen_at": "TEXT",
        },
        "businesses": {
            "intl_languages": "INTEGER",
            "investor_relations": "INTEGER DEFAULT 0",
            "org_info": "TEXT",
            "owner_name": "TEXT",
            "listing_updated": "TEXT",
            "listing_provider": "TEXT",
            "latest_date_year": "INTEGER",
            "closed_notice": "TEXT",
            "redirected_to": "TEXT",
            "people": "TEXT",
            "activity": "TEXT",
            "activity_reason": "TEXT",
            "owner_title": "TEXT",
            "owner_basis": "TEXT",
        },
    }

    def _migrate(self) -> None:
        for table, columns in self._ADDED_COLUMNS.items():
            have = {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in columns.items():
                if name not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        # The OpenStreetMap map-search cache is no longer used (businesses come from Overture).
        self.conn.execute("DROP TABLE IF EXISTS overpass_cache")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise

    def query(self, sql: str, params: Iterable = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Iterable = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, tuple(params)).fetchone()

    def scalar(self, sql: str, params: Iterable = ()) -> Any:
        row = self.one(sql, params)
        return row[0] if row else None

    def _update(self, table: str, row_id: int, fields: dict) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        values = [_dump(v) for v in fields.values()]
        self.conn.execute(f"UPDATE {table} SET {cols} WHERE id = ?", (*values, row_id))

    # ------------------------------------------------------------------ runs

    def create_run(
        self,
        *,
        kind: str,
        industries: list[str],
        location_input: str,
        location_type: str,
        sources: list[str],
        settings: dict,
    ) -> int:
        now = utcnow()
        with self.tx():
            cur = self.conn.execute(
                """INSERT INTO runs (created_at, updated_at, kind, industries, location_input,
                   location_type, sources, settings, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
                (now, now, kind, _dump(industries), location_input, location_type,
                 _dump(sources), _dump(settings)),
            )
        return int(cur.lastrowid)

    def update_run(self, run_id: int, **fields: Any) -> None:
        fields["updated_at"] = utcnow()
        with self.tx():
            self._update("runs", run_id, fields)

    def get_run(self, run_id: int) -> dict | None:
        row = self.one("SELECT * FROM runs WHERE id = ?", (run_id,))
        return self._run_dict(row) if row else None

    def list_runs(self, limit: int = 50) -> list[dict]:
        rows = self.query("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))
        return [self._run_dict(r) for r in rows]

    @staticmethod
    def _run_dict(row: sqlite3.Row) -> dict:
        out = dict(row)
        for key in ("industries", "sources", "settings", "stats"):
            out[key] = loads(out.get(key), {} if key in ("settings", "stats") else [])
        return out

    def add_run_tasks(self, run_id: int, tasks: Iterable[tuple[str, str]]) -> None:
        with self.tx():
            self.conn.executemany(
                "INSERT OR IGNORE INTO run_tasks (run_id, location, industry) VALUES (?, ?, ?)",
                [(run_id, loc, ind) for loc, ind in tasks],
            )

    def run_tasks(self, run_id: int, status: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM run_tasks WHERE run_id = ?"
        params: list = [run_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        return self.query(sql + " ORDER BY rowid", params)

    def set_task(self, run_id: int, location: str, industry: str, *, status: str,
                 found: int = 0, error: str | None = None) -> None:
        with self.tx():
            self.conn.execute(
                """UPDATE run_tasks SET status = ?, found = ?, error = ?
                   WHERE run_id = ? AND location = ? AND industry = ?""",
                (status, found, error, run_id, location, industry),
            )

    # ------------------------------------------------------------ businesses

    def upsert_business(
        self,
        cand: BusinessCandidate,
        *,
        run_id: int | None = None,
        non_business_domains: Iterable[str] = (),
        default_country: str | None = None,
    ) -> tuple[int, bool]:
        """Insert a candidate or merge it into the business it duplicates.

        Duplicate if any match: same source object, same domain, same phone,
        or same normalized name within ~100 m. Returns (business_id, created).
        """
        now = utcnow()
        country = (cand.country or default_country or "").upper() or None
        website = normalize_url(cand.website)
        domain = company_domain(website, self.shared_hosting)
        if not website:
            website_status = "none"
        elif domain_matches(normalize_domain(website), non_business_domains):
            website_status, domain = "social_only", None
        else:
            website_status = "ok"
        phone = normalize_phone(cand.phone, country)
        name_norm = normalize_name(cand.name) or cand.name.lower().strip()
        tags = {k: v for k, v in (cand.tags or {}).items() if v}
        socials = _social_profiles(list(cand.socials or [])
                                   + ([website] if website_status == "social_only" else []))

        with self.tx():
            existing, same_object = self._find_duplicate(cand, domain, phone, name_norm)
            if existing is None:
                cur = self.conn.execute(
                    """INSERT INTO businesses (name, name_norm, industry, address, city, region,
                       postcode, country, lat, lng, phone, website, domain, website_status,
                       tags, socials, listing_updated, listing_provider, stage, first_seen, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'found', ?, ?)""",
                    (cand.name.strip(), name_norm, cand.industry, cand.address, cand.city,
                     cand.region, cand.postcode, country, cand.lat, cand.lng, phone, website,
                     domain, website_status, _dump(tags), _dump(socials or None),
                     cand.listing_updated, cand.listing_provider, now, now),
                )
                business_id, created = int(cur.lastrowid), True
            else:
                business_id, created = int(existing["id"]), False
                self._merge(existing, cand, same_object, country, phone, website, domain,
                            website_status, tags, now, socials)
            self.conn.execute(
                "INSERT OR IGNORE INTO business_sources (business_id, run_id, source, source_ref)"
                " VALUES (?, ?, ?, ?)",
                (business_id, run_id, cand.source, cand.source_ref),
            )
        return business_id, created

    def _find_duplicate(self, cand, domain, phone, name_norm):
        row = self.one(
            "SELECT b.* FROM business_sources s JOIN businesses b ON b.id = s.business_id"
            " WHERE s.source = ? AND s.source_ref = ?",
            (cand.source, cand.source_ref),
        )
        if row:
            return row, True
        if domain:
            row = self.one("SELECT * FROM businesses WHERE domain = ?", (domain,))
            if row:
                return row, False
        # A listing without a website whose email is on a known company's domain.
        for email in cand.emails or []:
            email_domain = email.rsplit("@", 1)[-1].strip().lower()
            if "@" not in email or domain_matches(email_domain, self.freemail):
                continue
            key = company_domain(email_domain, self.shared_hosting)
            row = key and self.one("SELECT * FROM businesses WHERE domain = ?", (key,))
            if row:
                return row, False
        if phone:
            row = self.one("SELECT * FROM businesses WHERE phone = ? ORDER BY id LIMIT 1", (phone,))
            if row:
                return row, False
        if name_norm and cand.lat is not None and cand.lng is not None:
            for row in self.query(
                "SELECT * FROM businesses WHERE name_norm = ? AND lat IS NOT NULL", (name_norm,)
            ):
                if haversine_m(cand.lat, cand.lng, row["lat"], row["lng"]) <= SAME_PLACE_METERS:
                    return row, False
        return None, False

    def _merge(self, row, cand, same_object, country, phone, website, domain,
               website_status, tags, now, socials=None) -> None:
        fields: dict[str, Any] = {}
        if cand.listing_updated and (row["listing_updated"] or "") < cand.listing_updated:
            fields.update(listing_updated=cand.listing_updated, listing_provider=cand.listing_provider)
        have_socials = loads(row["socials"], {}) or {}
        merged_socials = {**(socials or {}), **have_socials}
        if merged_socials != have_socials:
            fields["socials"] = merged_socials
        for col, value in (("address", cand.address), ("city", cand.city),
                           ("region", cand.region), ("postcode", cand.postcode),
                           ("country", country), ("industry", cand.industry),
                           ("lat", cand.lat), ("lng", cand.lng), ("phone", phone)):
            if value not in (None, "") and row[col] in (None, ""):
                fields[col] = value
        if website and row["website_status"] != "ok" and website_status == "ok":
            taken = domain and self.one(
                "SELECT id FROM businesses WHERE domain = ? AND id != ?", (domain, row["id"])
            )
            if not taken:
                fields.update(website=website, domain=domain, website_status="ok")
                if row["stage"] != "found":
                    fields["stage"] = "found"   # now has a site worth crawling
        elif website and row["website_status"] == "none":
            fields.update(website=website, website_status=website_status)
        merged_tags = {**tags, **loads(row["tags"], {})}
        if merged_tags != loads(row["tags"], {}):
            fields["tags"] = merged_tags
        if not same_object and cand.lat is not None and row["lat"] is not None:
            distance = haversine_m(cand.lat, cand.lng, row["lat"], row["lng"])
            if distance > OTHER_BRANCH_METERS:
                fields["location_count"] = int(row["location_count"] or 1) + 1
        if fields:
            fields["updated_at"] = now
            self._update("businesses", row["id"], fields)

    _FILL_COLUMNS = ("address", "city", "region", "postcode", "country", "lat", "lng", "phone",
                     "industry", "final_url", "socials", "platform", "copyright_year",
                     "team_size_hint", "intl_languages", "org_info")

    def merge_businesses(self, keep_id: int, drop_id: int) -> None:
        """Fold business ``drop_id`` into ``keep_id``: sources, run links and
        emails move over, missing details are filled in, locations add up."""
        if keep_id == drop_id:
            return
        keep = self.get_business(keep_id)
        drop = self.get_business(drop_id)
        if keep is None or drop is None:
            return
        fields: dict[str, Any] = {}
        for col in self._FILL_COLUMNS:
            if keep[col] in (None, "", "{}") and drop[col] not in (None, "", "{}"):
                fields[col] = drop[col]
        if keep["website_status"] != "ok" and drop["website_status"] == "ok":
            fields.update(website=drop["website"], website_status="ok")
            if drop["stage"] != "found":
                fields.update(stage=drop["stage"], error=drop["error"])
        elif keep["stage"] == "found" and drop["stage"] != "found" and not fields.get("website"):
            fields.update(stage=drop["stage"])
        for col in ("multi_location_site", "has_careers", "investor_relations"):
            if drop[col] and not keep[col]:
                fields[col] = drop[col]
        tags = {**loads(drop["tags"], {}), **loads(keep["tags"], {})}
        if tags != loads(keep["tags"], {}):
            fields["tags"] = tags
        fields["location_count"] = int(keep["location_count"] or 1) + int(drop["location_count"] or 1)
        fields["updated_at"] = utcnow()
        with self.tx():
            self.conn.execute("UPDATE business_sources SET business_id = ? WHERE business_id = ?",
                              (keep_id, drop_id))
            self.conn.execute(
                "INSERT OR IGNORE INTO run_businesses (run_id, business_id, industry, location)"
                " SELECT run_id, ?, industry, location FROM run_businesses WHERE business_id = ?",
                (keep_id, drop_id))
            self.conn.execute("DELETE FROM run_businesses WHERE business_id = ?", (drop_id,))
            self.conn.execute("UPDATE emails SET business_id = ? WHERE business_id = ?",
                              (keep_id, drop_id))
            # Free the dropped row's unique domain before the kept row takes it over.
            self.conn.execute("UPDATE businesses SET domain = NULL WHERE id = ?", (drop_id,))
            if "website" in fields:
                fields["domain"] = drop["domain"]
            self._update("businesses", keep_id, fields)
            self.conn.execute("DELETE FROM businesses WHERE id = ?", (drop_id,))

    def link_business(self, run_id: int, business_id: int, industry: str | None,
                      location: str | None) -> bool:
        with self.tx():
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO run_businesses (run_id, business_id, industry, location)"
                " VALUES (?, ?, ?, ?)",
                (run_id, business_id, industry, location),
            )
        return cur.rowcount > 0

    def discard_preview(self, run_id: int) -> dict:
        """What discarding a run would remove: businesses/emails found by no other run."""
        only_here = """SELECT rb.business_id FROM run_businesses rb WHERE rb.run_id = ?
                       AND NOT EXISTS (SELECT 1 FROM run_businesses o
                                       WHERE o.business_id = rb.business_id AND o.run_id != ?)"""
        return {
            "businesses": int(self.scalar(f"SELECT COUNT(*) FROM ({only_here})", (run_id, run_id)) or 0),
            "emails": int(self.scalar(f"SELECT COUNT(*) FROM emails WHERE business_id IN ({only_here})",
                                      (run_id, run_id)) or 0),
            "shared": int(self.scalar(
                "SELECT COUNT(*) FROM run_businesses rb WHERE rb.run_id = ? AND EXISTS "
                "(SELECT 1 FROM run_businesses o WHERE o.business_id = rb.business_id AND o.run_id != ?)",
                (run_id, run_id)) or 0),
        }

    def discard_run(self, run_id: int) -> dict:
        """Delete a run, its progress, and the businesses/emails no other run found.

        Businesses that another run also found are kept (only this run's link is
        removed). Map, DNS and company-size caches are kept so later runs stay fast.
        """
        counts = self.discard_preview(run_id)
        with self.tx():
            orphan_ids = [r[0] for r in self.conn.execute(
                """SELECT rb.business_id FROM run_businesses rb WHERE rb.run_id = ?
                   AND NOT EXISTS (SELECT 1 FROM run_businesses o
                                   WHERE o.business_id = rb.business_id AND o.run_id != ?)""",
                (run_id, run_id))]
            self.conn.execute("DELETE FROM run_businesses WHERE run_id = ?", (run_id,))
            self.conn.execute("DELETE FROM run_tasks WHERE run_id = ?", (run_id,))
            self.conn.execute("UPDATE business_sources SET run_id = NULL WHERE run_id = ?", (run_id,))
            for i in range(0, len(orphan_ids), 500):
                chunk = orphan_ids[i:i + 500]
                marks = ",".join("?" * len(chunk))
                self.conn.execute(
                    f"DELETE FROM export_items WHERE email_id IN "
                    f"(SELECT id FROM emails WHERE business_id IN ({marks}))", chunk)
                self.conn.execute(f"DELETE FROM emails WHERE business_id IN ({marks})", chunk)
                self.conn.execute(f"DELETE FROM business_sources WHERE business_id IN ({marks})", chunk)
                self.conn.execute(f"DELETE FROM businesses WHERE id IN ({marks})", chunk)
            self.conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
        return counts

    def run_business_count(self, run_id: int) -> int:
        return int(self.scalar("SELECT COUNT(*) FROM run_businesses WHERE run_id = ?", (run_id,)))

    def run_businesses(self, run_id: int, where: str = "1=1", params: Iterable = ()) -> list[sqlite3.Row]:
        return self.query(
            "SELECT b.* FROM businesses b JOIN run_businesses rb ON rb.business_id = b.id"
            f" WHERE rb.run_id = ? AND ({where}) ORDER BY b.id",
            (run_id, *params),
        )

    def get_business(self, business_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM businesses WHERE id = ?", (business_id,))

    def update_business(self, business_id: int, **fields: Any) -> None:
        fields["updated_at"] = utcnow()
        with self.tx():
            self._update("businesses", business_id, fields)

    def name_location_counts(self, names: Iterable[str]) -> dict[str, int]:
        """Total known locations per normalized name, across the whole database."""
        names = [n for n in set(names) if n]
        counts: dict[str, int] = {}
        for i in range(0, len(names), 500):
            chunk = names[i:i + 500]
            marks = ",".join("?" * len(chunk))
            for row in self.query(
                f"SELECT name_norm, SUM(location_count) FROM businesses"
                f" WHERE name_norm IN ({marks}) GROUP BY name_norm",
                chunk,
            ):
                counts[row[0]] = int(row[1])
        return counts

    def business_sources(self, business_id: int) -> list[str]:
        rows = self.query(
            "SELECT DISTINCT source FROM business_sources WHERE business_id = ? ORDER BY source",
            (business_id,),
        )
        return [r[0] for r in rows]

    # ---------------------------------------------------------------- emails

    def add_email(self, business_id: int, email: str, *, email_type: str | None,
                  source: str, found_on_url: str | None, is_freemail: bool = False) -> bool:
        with self.tx():
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO emails (business_id, email, email_type, source,
                   found_on_url, is_freemail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (business_id, email, email_type, source, found_on_url, int(is_freemail), utcnow()),
            )
        return cur.rowcount > 0

    def emails_for_business(self, business_id: int) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM emails WHERE business_id = ? ORDER BY id", (business_id,))

    def run_emails(self, run_id: int, where: str = "1=1", params: Iterable = ()) -> list[sqlite3.Row]:
        return self.query(
            "SELECT e.* FROM emails e JOIN run_businesses rb ON rb.business_id = e.business_id"
            f" WHERE rb.run_id = ? AND ({where}) ORDER BY e.id",
            (run_id, *params),
        )

    def set_email_status(self, email_id: int, status: str, reason: str | None,
                         is_catch_all: bool | None = None) -> None:
        with self.tx():
            self.conn.execute(
                "UPDATE emails SET status = ?, status_reason = ?, is_catch_all = ?, verified_at = ?"
                " WHERE id = ?",
                (status, reason, None if is_catch_all is None else int(is_catch_all),
                 utcnow(), email_id),
            )

    def set_contact(self, email_id: int, name: str | None, designation: str | None,
                    decision_maker: bool, source: str | None) -> None:
        """Who is behind an address and their role (from the website or the address)."""
        self.conn.execute(
            "UPDATE emails SET contact_name = ?, designation = ?, decision_maker = ?, designation_source = ?"
            " WHERE id = ?", (name, designation, int(bool(decision_maker)), source, email_id))

    def set_email_score(self, email_id: int, score: int, detail: dict) -> None:
        self.conn.execute(
            "UPDATE emails SET score = ?, score_detail = ? WHERE id = ?",
            (score, _dump(detail), email_id),
        )

    # ----------------------------------------------------------------- crawl

    def record_page(self, url: str, domain: str | None, http_status: int | None,
                    error: str | None) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO crawl_pages (url, domain, http_status, fetched_at, error)"
            " VALUES (?, ?, ?, ?, ?)",
            (url, domain, http_status, utcnow(), error),
        )

    # ---------------------------------------------------------------- caches

    _CACHES = {
        "geocode": ("geocode_cache", "query", "cached_at"),
        "dns": ("dns_cache", "domain", "checked_at"),
        "org": ("org_cache", "domain", "checked_at"),
        "smtp": ("smtp_cache", "domain", "checked_at"),
    }

    def cache_get(self, cache: str, key: str, max_age_days: float) -> Any:
        table, key_col, time_col = self._CACHES[cache]
        row = self.one(f"SELECT result, {time_col} FROM {table} WHERE {key_col} = ?", (key,))
        if not row:
            return None
        cached_at = datetime.fromisoformat(row[1])
        if datetime.now(timezone.utc) - cached_at > timedelta(days=max_age_days):
            return None
        return loads(row[0])

    def cache_put(self, cache: str, key: str, value: Any, query: str | None = None) -> None:
        table, key_col, time_col = self._CACHES[cache]
        with self.tx():
            self.conn.execute(
                f"INSERT OR REPLACE INTO {table} ({key_col}, result, {time_col}) VALUES (?, ?, ?)",
                (key, json.dumps(value), utcnow()),
            )

    # ----------------------------------------------------------- suppression

    def suppress(self, value: str, reason: str | None = "manual") -> str:
        value = value.strip().lower()
        if value.startswith("mailto:"):
            value = value[7:]
        if "@" in value:
            kind = "email"
        else:
            kind = "domain"
            value = normalize_domain(value) or value
        with self.tx():
            self.conn.execute(
                "INSERT OR REPLACE INTO suppression (value, kind, reason, added_at) VALUES (?, ?, ?, ?)",
                (value, kind, reason, utcnow()),
            )
        return kind

    def unsuppress(self, value: str) -> bool:
        value = value.strip().lower()
        if "@" not in value:
            value = normalize_domain(value) or value
        with self.tx():
            cur = self.conn.execute("DELETE FROM suppression WHERE value = ?", (value,))
        return cur.rowcount > 0

    def suppression_list(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM suppression ORDER BY added_at DESC, value")

    # --------------------------------------------------------------- exports

    def record_export(self, filters: dict, file_path: str, email_ids: list[int]) -> int:
        with self.tx():
            cur = self.conn.execute(
                "INSERT INTO exports (created_at, filters, file_path, row_count) VALUES (?, ?, ?, ?)",
                (utcnow(), _dump(filters), file_path, len(email_ids)),
            )
            export_id = int(cur.lastrowid)
            self.conn.executemany(
                "INSERT OR IGNORE INTO export_items (export_id, email_id) VALUES (?, ?)",
                [(export_id, eid) for eid in email_ids],
            )
        return export_id

    # ----------------------------------------------------------------- stats

    def run_stats(self, run_id: int) -> dict:
        b = self.one(
            """SELECT COUNT(*) AS businesses,
                 SUM(b.website_status = 'ok') AS with_website,
                 SUM(b.website_status = 'social_only') AS social_only,
                 SUM(b.website_status = 'none' OR b.website_status IS NULL) AS no_website,
                 SUM(b.is_chain = 1) AS chains,
                 SUM(b.stage IN ('crawled', 'verified', 'scored') AND b.website_status = 'ok') AS crawled,
                 SUM(b.error IS NOT NULL) AS errors,
                 SUM(b.stage = 'scored') AS scored,
                 SUM(EXISTS (SELECT 1 FROM emails e WHERE e.business_id = b.id)) AS with_email,
                 SUM(b.activity IN ('active', 'likely active')) AS active_businesses,
                 SUM(b.activity = 'likely closed') AS closed_businesses
               FROM businesses b JOIN run_businesses rb ON rb.business_id = b.id
               WHERE rb.run_id = ?""",
            (run_id,),
        )
        e = self.one(
            """SELECT COUNT(*) AS emails,
                 SUM(e.status IS NOT NULL) AS verified,
                 SUM(e.status = 'valid') AS valid,
                 SUM(e.status = 'risky') AS risky,
                 SUM(e.status = 'invalid') AS invalid,
                 SUM(e.status = 'unknown') AS unknown,
                 SUM(e.mailbox_check IS NOT NULL) AS mailbox_checked,
                 SUM(e.mailbox_check = 'valid') AS mailbox_confirmed,
                 SUM(e.mailbox_check IS NULL AND e.status = 'valid'
                     AND COALESCE(e.is_freemail, 0) = 0) AS mailbox_todo
               FROM emails e JOIN run_businesses rb ON rb.business_id = e.business_id
               WHERE rb.run_id = ?""",
            (run_id,),
        )
        stats = {k: int(b[k] or 0) for k in b.keys()}
        stats.update({k: int(e[k] or 0) for k in e.keys()})
        return stats
