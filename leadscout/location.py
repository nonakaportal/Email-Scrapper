"""Location resolution: place names -> border (polygon) + bounding box, points -> radius.

Uses the public Nominatim place-search service (max 1 request/second, an
identifying User-Agent, results cached in the database). Businesses themselves
come from Overture Maps; this only turns what the user typed into an area.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass

import httpx

from leadscout.config import Config
from leadscout.db.store import Store
from leadscout.http import api_client, limiter_for
from leadscout.normalize import haversine_m

# Border detail kept from Nominatim, in degrees (~500 m); enough to tell inside from outside.
POLYGON_THRESHOLD = 0.005
_POINT_RE = re.compile(r"^\s*(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$")


class LocationError(RuntimeError):
    pass


@dataclass
class LocationSpec:
    type: str                      # place | radius
    query: str                     # place name, address, or "lat,lng"
    radius_km: float | None = None
    osm: str | None = None         # exact place picked from search results, e.g. "R113314"

    def key(self) -> str:
        data = asdict(self)
        if data["osm"] is None:
            del data["osm"]         # keep keys of plain searches unchanged
        return json.dumps(data, sort_keys=True)

    @classmethod
    def from_key(cls, key: str) -> "LocationSpec":
        return cls(**json.loads(key))

    def label(self) -> str:
        if self.type == "radius":
            return f"{self.query} (+{self.radius_km:g} km)"
        return self.query


@dataclass
class ResolvedLocation:
    label: str
    kind: str                      # area | around
    display_name: str
    area_id: int | None = None
    lat: float | None = None
    lng: float | None = None
    radius_m: int | None = None
    country: str | None = None     # ISO alpha-2, upper case
    city: str | None = None
    region: str | None = None
    note: str | None = None
    bbox: tuple[float, float, float, float] | None = None   # south, west, north, east
    polygon: dict | None = None    # GeoJSON border of an area (from Nominatim)

    def contains(self, lat: float, lng: float) -> bool:
        """Is this point inside the place (its border, or the radius)?"""
        if self.kind == "around":
            return haversine_m(self.lat, self.lng, lat, lng) <= (self.radius_m or 0)
        box = self.bbox
        if box and not (box[0] <= lat <= box[2] and box[1] <= lng <= box[3]):
            return False
        shape = self._shape()
        if shape is None:
            return True
        from shapely.geometry import Point
        return shape.contains(Point(lng, lat))

    def _shape(self):
        if not self.polygon or self.polygon.get("type") not in ("Polygon", "MultiPolygon"):
            return None
        cached = getattr(self, "_prepared", None)
        if cached is None:
            from shapely.geometry import shape
            from shapely.prepared import prep
            cached = prep(shape(self.polygon))
            object.__setattr__(self, "_prepared", cached)
        return cached

    def intersects_box(self, south: float, west: float, north: float, east: float) -> bool:
        """Could this tile contain anything inside the place? (skips ocean tiles)"""
        shape = self._shape()
        if shape is None:
            return True
        from shapely.geometry import box
        return shape.intersects(box(west, south, east, north))

    def bounds(self) -> tuple[float, float, float, float] | None:
        """Bounding box of the search area, used to split big searches into tiles."""
        if self.kind == "around" and self.lat is not None and self.radius_m:
            dlat = self.radius_m / 111_000
            dlng = self.radius_m / (111_000 * max(0.1, math.cos(math.radians(self.lat))))
            return (self.lat - dlat, self.lng - dlng, self.lat + dlat, self.lng + dlng)
        return self.bbox


def parse_point(text: str) -> tuple[float, float] | None:
    m = _POINT_RE.match(text or "")
    if not m:
        return None
    lat, lng = float(m.group(1)), float(m.group(2))
    if -90 <= lat <= 90 and -180 <= lng <= 180:
        return lat, lng
    return None


class LocationResolver:
    def __init__(self, cfg: Config, store: Store, transport: httpx.BaseTransport | None = None):
        self.cfg = cfg
        self.store = store
        self.base_url = cfg.get("nominatim.base_url").rstrip("/")
        self.limiter = limiter_for(self.base_url, cfg.get("nominatim.min_interval_seconds", 1.1))
        self.client = api_client(cfg.user_agent, timeout=30, transport=transport)

    def close(self) -> None:
        self.client.close()

    def resolve(self, spec: LocationSpec) -> ResolvedLocation:
        if spec.type == "radius":
            return self._resolve_radius(spec)
        if spec.osm:
            return self._resolve_place(spec, self.lookup(spec.osm))
        return self._resolve_place(spec)

    def _resolve_radius(self, spec: LocationSpec) -> ResolvedLocation:
        radius_m = int(float(spec.radius_km or 10) * 1000)
        point = parse_point(spec.query)
        if point:
            return ResolvedLocation(label=spec.label(), kind="around", display_name=spec.query,
                                    lat=point[0], lng=point[1], radius_m=radius_m)
        results = self.search(spec.query)
        if not results:
            raise LocationError(f"Could not find '{spec.query}'. Check the spelling or use lat,lng.")
        top = results[0]
        loc = self._from_result(top, spec.label())
        loc.kind, loc.area_id, loc.radius_m = "around", None, radius_m
        return loc

    def _resolve_place(self, spec: LocationSpec, results: list[dict] | None = None) -> ResolvedLocation:
        point = parse_point(spec.query)
        if point and results is None:
            radius = float(self.cfg.get("nominatim.fallback_radius_km", 10))
            return self._resolve_radius(LocationSpec("radius", spec.query, radius))
        if results is None:
            results = self.search(spec.query)
        if not results:
            raise LocationError(
                f"Could not find '{spec.query}'. Check the spelling, add the region and "
                "country (e.g. 'Austin, Texas, USA'), or use a point + radius search."
            )
        for res in results:
            if res.get("osm_type") == "relation" and res.get("category", res.get("class")) in (
                "boundary", "place"
            ):
                loc = self._from_result(res, spec.label())
                loc.kind = "area"
                loc.area_id = int(res["osm_id"])
                return loc
        # No boundary polygon (small towns are often mapped as a single point).
        top = results[0]
        radius_km = float(self.cfg.get("nominatim.fallback_radius_km", 10))
        loc = self._from_result(top, spec.label())
        loc.kind, loc.radius_m = "around", int(radius_km * 1000)
        loc.note = (f"'{spec.query}' has no known border; "
                    f"searching {radius_km:g} km around its centre instead.")
        return loc

    @staticmethod
    def _from_result(res: dict, label: str) -> ResolvedLocation:
        addr = res.get("address") or {}
        city = next((addr[k] for k in ("city", "town", "village", "municipality", "hamlet")
                     if addr.get(k)), None)
        if city is None and res.get("addresstype") in ("city", "town", "village"):
            city = res.get("name")
        bbox = None
        try:
            south, north, west, east = (float(v) for v in res.get("boundingbox") or [])
            bbox = (south, west, north, east)
        except (TypeError, ValueError):
            pass
        return ResolvedLocation(
            label=label,
            kind="area",
            bbox=bbox,
            polygon=res.get("geojson"),
            display_name=res.get("display_name") or label,
            lat=float(res["lat"]) if res.get("lat") else None,
            lng=float(res["lon"]) if res.get("lon") else None,
            country=(addr.get("country_code") or "").upper() or None,
            city=city,
            region=addr.get("state") or addr.get("region") or addr.get("county"),
        )

    def suggest(self, query: str) -> list[dict]:
        """Places matching what the user typed, for them to pick the exact one.

        Each: {"osm": "R113314", "label": display name, "kind": "city" / "state" /
        "country" ..., "area": True if it has a boundary to search inside}.
        """
        out, seen = [], set()
        for res in self.search(query):
            osm_type, osm_id = res.get("osm_type"), res.get("osm_id")
            if not osm_type or osm_id is None:
                continue
            ref = f"{osm_type[0].upper()}{osm_id}"
            if ref in seen:
                continue
            seen.add(ref)
            out.append({"osm": ref, "label": res.get("display_name") or query,
                        "kind": (res.get("addresstype") or res.get("type") or "place").replace("_", " "),
                        "area": osm_type == "relation"})
        return out

    def lookup(self, osm: str) -> list[dict]:
        """Details of one exact place ("R113314", "N123", "W456")."""
        return self._get("lookup", {"osm_ids": osm}, cache_key=f"v2:lookup:{osm.upper()}", what=osm)

    def search(self, query: str) -> list[dict]:
        key = re.sub(r"\s+", " ", query.strip().lower())
        return self._get("search", {"q": query, "limit": 8}, cache_key=f"v2:{key}", what=query)

    def _get(self, endpoint: str, params: dict, *, cache_key: str, what: str) -> list[dict]:
        cached = self.store.cache_get("geocode", cache_key, self.cfg.get("nominatim.cache_days", 90))
        if cached is not None:
            return cached
        params = {**params, "format": "jsonv2", "addressdetails": 1,
                  "polygon_geojson": 1, "polygon_threshold": POLYGON_THRESHOLD}
        query = what
        if self.cfg.contact_email:
            params["email"] = self.cfg.contact_email
        self.limiter.wait()
        try:
            resp = self.client.get(f"{self.base_url}/{endpoint}", params=params)
            resp.raise_for_status()
            results = resp.json()
        except httpx.HTTPStatusError as exc:
            raise LocationError(
                f"Nominatim returned HTTP {exc.response.status_code} for '{query}'. "
                "If this is 403/429, wait a few minutes and set contact_email in config/settings.yaml."
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise LocationError(f"Could not reach Nominatim to look up '{query}': {exc}") from exc
        self.store.cache_put("geocode", cache_key, results)
        return results
