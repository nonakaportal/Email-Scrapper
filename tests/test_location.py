"""Location search: pick the exact place from a list of matches."""

import json

import httpx

from leadscout.location import LocationResolver, LocationSpec

SPRINGFIELDS = [
    {"osm_type": "relation", "osm_id": 111, "category": "boundary", "addresstype": "city",
     "lat": "39.8", "lon": "-89.6", "boundingbox": ["39.7", "39.9", "-89.8", "-89.5"],
     "display_name": "Springfield, Sangamon County, Illinois, United States",
     "address": {"city": "Springfield", "state": "Illinois", "country_code": "us"}},
    {"osm_type": "relation", "osm_id": 222, "category": "boundary", "addresstype": "city",
     "lat": "37.2", "lon": "-93.3", "display_name": "Springfield, Greene County, Missouri, United States",
     "address": {"city": "Springfield", "state": "Missouri", "country_code": "us"}},
    {"osm_type": "node", "osm_id": 333, "category": "place", "addresstype": "village",
     "lat": "50.1", "lon": "-1.2", "display_name": "Springfield, England, United Kingdom",
     "address": {"village": "Springfield", "country_code": "gb"}},
]


def resolver(cfg, store, calls):
    def handler(request):
        calls.append((request.url.path, dict(request.url.params)))
        if request.url.path == "/lookup":
            ref = request.url.params["osm_ids"]
            return httpx.Response(200, json=[r for r in SPRINGFIELDS
                                             if f"{r['osm_type'][0].upper()}{r['osm_id']}" == ref])
        return httpx.Response(200, json=SPRINGFIELDS)
    return LocationResolver(cfg, store, transport=httpx.MockTransport(handler))


def test_suggest_lists_each_match_with_its_type(cfg, store):
    found = resolver(cfg, store, []).suggest("Springfield")
    assert [f["osm"] for f in found] == ["R111", "R222", "N333"]
    assert found[1]["label"].startswith("Springfield, Greene County, Missouri")
    assert [f["kind"] for f in found] == ["city", "city", "village"]
    assert [f["area"] for f in found] == [True, True, False]


def test_picked_place_is_searched_exactly(cfg, store):
    calls = []
    loc = resolver(cfg, store, calls).resolve(LocationSpec("place", "Springfield, Missouri", osm="R222"))
    assert calls[-1][0] == "/lookup" and calls[-1][1]["osm_ids"] == "R222"
    assert loc.kind == "area" and loc.area_id == 222 and loc.region == "Missouri"
    assert calls[-1][1]["polygon_geojson"] == "1"          # the border is fetched for the inside-check


def test_picked_point_place_searches_around_it(cfg, store):
    loc = resolver(cfg, store, []).resolve(LocationSpec("place", "Springfield, England", osm="N333"))
    assert loc.kind == "around" and loc.lat == 50.1 and loc.country == "GB"


def test_plain_place_key_is_unchanged():
    # runs created before place search keep working (their saved keys have no "osm")
    assert json.loads(LocationSpec("place", "Austin").key()) == {"query": "Austin", "radius_km": None, "type": "place"}
    spec = LocationSpec("place", "Springfield", osm="R111")
    assert LocationSpec.from_key(spec.key()) == spec
