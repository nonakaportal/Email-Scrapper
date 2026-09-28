"""Businesses from Overture Maps (https://overturemaps.org).

Overture publishes its places as open GeoParquet files on Amazon S3 (free, no
account or key). We read only the columns we need, only for the bounding box of
the searched place, tile by tile, and keep businesses that are open, reasonably
confident, inside the place, and in one of the 50 industries.

Each tile's matching businesses are cached on disk (data/overture/<release>/),
so repeating or resuming a search does not download them again.

Data license: CDLA-Permissive-2.0 (credit "Overture Maps Foundation").
"""

from __future__ import annotations

import gzip
import json
import logging
import math
import time
from pathlib import Path
from typing import Callable, Iterable

from leadscout.config import Config
from leadscout.db.store import Store
from leadscout.industries import IndustryRule, category_map, classify, load_exclude, load_rules
from leadscout.location import ResolvedLocation
from leadscout.sources.base import BusinessCandidate

log = logging.getLogger(__name__)

BUCKET = "overturemaps-us-west-2"
REGION = "us-west-2"
COLUMNS = ["id", "names", "taxonomy", "websites", "emails", "phones", "socials", "brand", "addresses",
           "confidence", "operating_status", "bbox", "sources"]
# Bumped when the saved tile format changes, so old tile files are downloaded again.
TILE_FORMAT = 3
CLOSED = {"permanently_closed", "temporarily_closed", "closed"}
Tile = tuple[float, float, float, float]          # south, west, north, east


class OvertureError(RuntimeError):
    pass


class DownloadCancelled(Exception):
    """Pause / "continue with what's found" was pressed while a map section was downloading."""


def make_tiles(bounds: Tile, tile_degrees: float, max_tiles: int) -> list[Tile]:
    """Split a bounding box into a grid of at most ``max_tiles`` tiles."""
    s, w, n, e = bounds
    height, width = max(n - s, 1e-6), max(e - w, 1e-6)
    size = tile_degrees
    if math.ceil(height / size) * math.ceil(width / size) > max_tiles:
        size = math.sqrt(height * width / max_tiles)
        while math.ceil(height / size) * math.ceil(width / size) > max_tiles:
            size *= 1.05
    rows, cols = math.ceil(height / size), math.ceil(width / size)
    dh, dw = height / rows, width / cols
    return [(s + r * dh, w + c * dw, s + (r + 1) * dh, w + (c + 1) * dw)
            for r in range(rows) for c in range(cols)]


def _first(values) -> str | None:
    for v in values or []:
        if v:
            return str(v).strip()
    return None


def listing_freshness(sources) -> tuple[str | None, str | None]:
    """(date, provider) of the newest update by a data provider (Foursquare, Meta ...).
    Overture's own calculations (confidence, status signals) are not listings."""
    best = (None, None)
    for src in sources or []:
        dataset = (src or {}).get("dataset") or ""
        when = str((src or {}).get("update_time") or "")[:10]
        if not when or dataset.lower().startswith("overture"):
            continue
        if best[0] is None or when > best[0]:
            best = (when, dataset)
    return best


def compact(row: dict, industry: str) -> dict:
    """The fields LeadScout keeps from an Overture place."""
    box = row.get("bbox") or {}
    addr = (row.get("addresses") or [{}])[0] or {}
    brand = row.get("brand") or {}
    names = row.get("names") or {}
    return {
        "id": row.get("id"),
        "industry": industry,
        "name": (names.get("primary") or "").strip(),
        "lat": (box.get("ymin", 0) + box.get("ymax", 0)) / 2,
        "lng": (box.get("xmin", 0) + box.get("xmax", 0)) / 2,
        "website": _first(row.get("websites")),
        "phone": _first(row.get("phones")),
        "emails": [e for e in (row.get("emails") or []) if e],
        "socials": [s for s in (row.get("socials") or []) if s],
        "listing_updated": listing_freshness(row.get("sources"))[0],
        "listing_provider": listing_freshness(row.get("sources"))[1],
        "address": addr.get("freeform"),
        "city": addr.get("locality"),
        "region": addr.get("region"),
        "postcode": addr.get("postcode"),
        "country": (addr.get("country") or "").upper()[:2] or None,
        "brand": ((brand.get("names") or {}).get("primary")),
        "brand_wikidata": brand.get("wikidata"),
    }


def to_candidate(row: dict, location: ResolvedLocation) -> BusinessCandidate:
    return BusinessCandidate(
        name=row["name"],
        source="overture",
        source_ref=row["id"],
        industry=row["industry"],
        address=row.get("address"),
        city=row.get("city") or location.city,
        region=row.get("region") or location.region,
        postcode=row.get("postcode"),
        country=row.get("country") or location.country,
        lat=row["lat"],
        lng=row["lng"],
        phone=row.get("phone"),
        website=row.get("website"),
        emails=list(row.get("emails") or []),
        socials=list(row.get("socials") or []),
        listing_updated=row.get("listing_updated"),
        listing_provider=row.get("listing_provider"),
        tags={k: v for k, v in (("brand", row.get("brand")), ("brand:wikidata", row.get("brand_wikidata"))) if v},
    )


class OvertureSource:
    name = "overture"

    def __init__(self, cfg: Config, store: Store,
                 reader: Callable[[Tile], Iterable[dict]] | None = None):
        self.cfg = cfg
        self.store = store
        self.rules = load_rules(cfg.industries)
        self.categories = category_map(self.rules)
        self.exclude = load_exclude(cfg.industries)
        self.min_confidence = float(cfg.get("overture.min_confidence", 0.5))
        self.tile_degrees = float(cfg.get("overture.tile_degrees", 0.5))
        self.max_tiles = int(cfg.get("overture.max_tiles", 64))
        self.cache_days = float(cfg.get("overture.cache_days", 30))
        self.cache_dir = cfg.db_path.parent / "overture"
        self._release = cfg.get("overture.release") or None
        self._reader = reader            # tests inject raw Overture rows per tile
        self._dataset = None
        self.failed: dict[str, str] = {}
        self.should_stop: Callable[[], bool] = lambda: False   # set by the run

    def close(self) -> None:
        self._dataset = None

    # ------------------------------------------------------------- reading

    @property
    def release(self) -> str:
        if self._release is None:
            if self._reader is not None:
                self._release = "test"
            else:
                try:
                    from overturemaps import core
                    self._release = core.get_latest_release()
                except Exception as exc:  # noqa: BLE001 - network or catalog problems
                    raise OvertureError(f"Could not reach Overture Maps to find the latest data: {exc}") from exc
        return self._release

    def _dataset_for_release(self):
        if self._dataset is None:
            import pyarrow.dataset as ds
            from pyarrow import fs
            path = f"{BUCKET}/release/{self.release}/theme=places/type=place/"
            try:
                self._dataset = ds.dataset(path, filesystem=fs.S3FileSystem(anonymous=True, region=REGION,
                                                                           connect_timeout=20,
                                                                           request_timeout=120))
            except Exception as exc:  # noqa: BLE001
                raise OvertureError(f"Could not open Overture Maps data: {exc}") from exc
        return self._dataset

    def _raw_rows(self, tile: Tile) -> Iterable[dict]:
        if self._reader is not None:
            yield from self._reader(tile)
            return
        import pyarrow.compute as pc
        s, w, n, e = tile
        where = ((pc.field("bbox", "xmin") < e) & (pc.field("bbox", "xmax") > w)
                 & (pc.field("bbox", "ymin") < n) & (pc.field("bbox", "ymax") > s))
        # Streamed in small batches with little read-ahead, so memory stays low enough for
        # small hosting plans (the whole tile at once peaked near 900 MB).
        try:
            scanner = self._dataset_for_release().scanner(columns=COLUMNS, filter=where, batch_size=4096,
                                                          batch_readahead=1, fragment_readahead=1)
            for batch in scanner.to_batches():
                if self.should_stop():             # Pause works mid-download, not only between sections
                    raise DownloadCancelled()
                yield from batch.to_pylist()
        except DownloadCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - S3/network errors
            raise OvertureError(f"Could not download Overture Maps data: {exc}") from exc

    def _cache_file(self, tile: Tile) -> Path:
        name = "_".join(f"{v:.4f}" for v in tile)
        return self.cache_dir / self.release / f"{name}.v{TILE_FORMAT}.json.gz"

    def tile_rows(self, tile: Tile) -> list[dict]:
        """Businesses in a tile that belong to any industry (cached on disk)."""
        path = self._cache_file(tile)
        if path.exists() and time.time() - path.stat().st_mtime < self.cache_days * 86400:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                return json.load(fh)
        started = time.time()
        rows = []
        for row in self._raw_rows(tile):
            if (row.get("operating_status") or "") in CLOSED:
                continue
            if (row.get("confidence") or 0) < self.min_confidence:
                continue
            taxonomy = row.get("taxonomy") or {}
            industry = classify(taxonomy.get("hierarchy"), self.categories, self.exclude,
                                taxonomy.get("alternates"))
            if industry is None:
                continue
            item = compact(row, industry)
            if item["name"]:
                rows.append(item)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            json.dump(rows, fh)
        tmp.replace(path)
        log.info("Overture tile %s: %d businesses in %.1fs", tile, len(rows), time.time() - started)
        return rows

    # ------------------------------------------------------------- search

    def tiles(self, location: ResolvedLocation) -> list[Tile]:
        bounds = location.bounds()
        if not bounds:
            raise OvertureError(f"No area known for '{location.label}'. Try a more specific place.")
        return [t for t in make_tiles(bounds, self.tile_degrees, self.max_tiles)
                if location.intersects_box(*t)]

    def search(self, location: ResolvedLocation, rule: IndustryRule) -> list[BusinessCandidate]:
        return self.search_many(location, [rule])[rule.key]

    def search_many(self, location: ResolvedLocation, rules: list[IndustryRule],
                    on_tile: Callable[[int, int, dict[str, list[BusinessCandidate]]], None] | None = None,
                    ) -> dict[str, list[BusinessCandidate]]:
        """Businesses of the given industries inside the place.

        ``on_tile(done, total, found_in_tile)`` is called after every tile, so
        callers can save results and show progress as they arrive.
        """
        self.failed = {}
        wanted = {r.key for r in rules}
        out: dict[str, list[BusinessCandidate]] = {k: [] for k in wanted}
        tiles = self.tiles(location)
        seen: set[str] = set()
        for i, tile in enumerate(tiles, start=1):
            found: dict[str, list[BusinessCandidate]] = {k: [] for k in wanted}
            for row in self.tile_rows(tile):
                if row["industry"] not in wanted or row["id"] in seen:
                    continue
                if not location.contains(row["lat"], row["lng"]):
                    continue
                seen.add(row["id"])
                found[row["industry"]].append(to_candidate(row, location))
            for key, cands in found.items():
                out[key].extend(cands)
            if on_tile:
                on_tile(i, len(tiles), found)
        return out
