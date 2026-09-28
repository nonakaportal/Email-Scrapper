"""The 50 industries, and matching Overture Maps places to them."""

import pytest

from leadscout.industries import (
    NotOnMap,
    UnknownIndustry,
    category_map,
    classify,
    group_options,
    load_exclude,
    load_groups,
    load_rules,
    resolve_industry,
    resolve_many,
)
from leadscout.location import ResolvedLocation
from leadscout.sources.overture import OvertureSource, compact, make_tiles, to_candidate


@pytest.fixture
def rules(cfg):
    return load_rules(cfg.industries)


@pytest.fixture
def cats(rules):
    return category_map(rules)


def test_fifty_ranked_industries(cfg, rules):
    assert len(rules) == 50
    assert sorted(r.rank for r in rules.values()) == list(range(1, 51))
    assert min(rules.values(), key=lambda r: r.rank).key == "construction"
    assert all(r.on_map and r.overture for r in rules.values())
    assert {r.group for r in rules.values()} == set(load_groups(cfg.industries))


def test_each_overture_category_belongs_to_one_industry(rules):
    seen = {}
    for rule in rules.values():
        for node in rule.overture:
            assert node not in seen, f"{node} in {seen[node]} and {rule.key}"
            seen[node] = rule.key


@pytest.mark.parametrize("word,key", [
    ("construction", "construction"),
    ("dentist", "dental"),
    ("Dentists", "dental"),
    ("vet", "pets"),
    ("Real Estate", "real_estate"),
    ("hair salon", "beauty"),
    ("IT company", "it_tech"),
    ("attorneys", "legal"),
    ("Bakeries", "bakeries"),
    ("plumber", "construction"),
    ("roofers", "construction"),
    ("seo agency", "marketing"),
    ("barber", "beauty"),
    ("bookkeeping", "accounting"),
    ("motel", "lodging"),
])
def test_resolve(rules, word, key):
    assert resolve_industry(word, rules).key == key


def test_unknown_gives_suggestions(rules):
    with pytest.raises(UnknownIndustry) as exc:
        resolve_industry("dentl", rules)
    assert "dental" in exc.value.suggestions


def test_not_searchable():
    rules = load_rules({"etsy_seller": {"label": "Etsy sellers", "searchable": False,
                                        "note": "Online businesses aren't listed."}})
    with pytest.raises(NotOnMap) as exc:
        resolve_many(["etsy sellers"], rules)
    assert "Enrich" in str(exc.value)


def test_groups(cfg, rules):
    groups = load_groups(cfg.industries)
    section = resolve_many(["group:construction"], rules, groups)
    assert [r.key for r in section] == ["construction", "home_services"]
    assert resolve_many(["construction"], rules, groups)[0].key == "construction"   # the industry itself
    assert resolve_many(["Construction & Trades"], rules, groups) == section
    top10 = resolve_many(["top10", "dentist"], rules, groups)
    assert [r.rank for r in top10[:10]] == list(range(1, 11)) and len({r.key for r in top10}) == len(top10)
    assert group_options(rules, groups)[0][0] == "construction"


@pytest.mark.parametrize("path,industry", [
    (["services_and_business", "home_service", "plumbing"], "construction"),
    (["services_and_business", "home_service", "ceiling_and_roofing_repair_and_service"], "construction"),
    (["services_and_business", "home_service", "key_and_locksmith"], "home_services"),
    (["services_and_business", "home_service", "something_new"], "home_services"),   # parent category
    (["services_and_business", "home_service", "home_cleaning"], "cleaning"),
    (["services_and_business", "financial_service", "accountant"], "accounting"),
    (["services_and_business", "financial_service", "insurance_agency"], "financial"),
    (["health_care", "outpatient_care_facility", "dental_clinic"], "dental"),
    (["food_and_drink", "casual_eatery", "bakery"], "bakeries"),
    (["food_and_drink", "casual_eatery", "cafe"], "restaurants"),
    (["lifestyle_services", "personal_or_beauty_service", "barber"], "beauty"),
    (["services_and_business", "real_estate_service", "builder"], "construction"),
    (["education", "place_of_learning", "school", "preschool"], "childcare"),
])
def test_classify(cats, cfg, path, industry):
    assert classify(path, cats, load_exclude(cfg.industries)) == industry


@pytest.mark.parametrize("path", [
    ["services_and_business", "financial_service", "bank_or_credit_union"],
    ["services_and_business", "financial_service", "atm"],
    ["health_care", "hospital"],
    ["education", "place_of_learning", "college_university"],
    ["community_and_government", "government_office"],
    ["geographic_entities", "water_feature"],
    [],
])
def test_never_leads(cats, cfg, path):
    assert classify(path, cats, load_exclude(cfg.industries)) is None


def raw(pid, name, lat, lng, path, **kw):
    return {"id": pid, "names": {"primary": name}, "taxonomy": {"hierarchy": path},
            "websites": kw.get("websites"), "emails": kw.get("emails"), "phones": kw.get("phones"),
            "brand": kw.get("brand"), "addresses": kw.get("addresses"),
            "confidence": kw.get("confidence", 0.9), "operating_status": kw.get("status", "open"),
            "bbox": {"xmin": lng, "xmax": lng, "ymin": lat, "ymax": lat}}


PLUMBER = ["services_and_business", "home_service", "plumbing"]
DENTIST = ["health_care", "outpatient_care_facility", "dental_clinic"]


def test_compact_and_candidate(cats):
    row = compact(raw("x1", "Pipe Pros", 21.3, -157.8, PLUMBER, websites=["https://pipepros.com", None],
                      emails=["info@pipepros.com"], phones=["+18085550100"],
                      brand={"names": {"primary": "Roto"}, "wikidata": "Q1"},
                      addresses=[{"freeform": "1 Main", "locality": "Honolulu", "region": "HI",
                                  "postcode": "96813", "country": "us"}]), "construction")
    assert row["website"] == "https://pipepros.com" and row["emails"] == ["info@pipepros.com"]
    assert row["city"] == "Honolulu" and row["country"] == "US" and row["brand"] == "Roto"
    loc = ResolvedLocation(label="Honolulu", kind="around", display_name="x", lat=21.3, lng=-157.8, radius_m=5000)
    cand = to_candidate(row, loc)
    assert cand.source == "overture" and cand.source_ref == "x1" and cand.industry == "construction"
    assert cand.brand == "Roto" and cand.phone == "+18085550100"


def test_make_tiles_caps_the_number():
    assert len(make_tiles((21.0, -158.0, 21.4, -157.6), 0.5, 64)) == 1
    tiles = make_tiles((18.9, -178.4, 28.5, -154.8), 0.5, 64)      # all of Hawaii's box
    assert 1 < len(tiles) <= 64
    assert min(t[0] for t in tiles) == pytest.approx(18.9) and max(t[3] for t in tiles) == pytest.approx(-154.8)


def test_search_keeps_open_confident_businesses_inside_the_place(cfg, store, rules):
    rows = [
        raw("a", "Pipe Pros", 21.30, -157.80, PLUMBER, websites=["https://pipepros.com"]),
        raw("b", "Smile Dental", 21.31, -157.81, DENTIST),
        raw("c", "Closed Pipes", 21.30, -157.80, PLUMBER, status="permanently_closed"),
        raw("d", "Maybe Pipes", 21.30, -157.80, PLUMBER, confidence=0.1),
        raw("e", "Far Pipes", 21.60, -157.50, PLUMBER),                 # outside the 5 km radius
        raw("f", "", 21.30, -157.80, PLUMBER),                           # no name
        raw("g", "Lake", 21.30, -157.80, ["geographic_entities", "water_feature"]),
    ]
    calls = []

    def reader(tile):
        calls.append(tile)
        return rows

    src = OvertureSource(cfg, store, reader=reader)
    loc = ResolvedLocation(label="Honolulu", kind="around", display_name="x", lat=21.3, lng=-157.8, radius_m=5000)
    tiles_seen = []
    found = src.search_many(loc, [rules["construction"]],
                            on_tile=lambda done, total, f: tiles_seen.append((done, total)))
    assert [c.name for c in found["construction"]] == ["Pipe Pros"]
    assert tiles_seen == [(1, 1)]
    # the dentist was kept in the cache, so another industry needs no new download
    assert [c.name for c in src.search_many(loc, [rules["dental"]])["dental"]] == ["Smile Dental"]
    assert len(calls) == 1


def test_border_filter(cfg, store, rules):
    square = {"type": "Polygon", "coordinates": [[[-158.0, 21.0], [-157.5, 21.0], [-157.5, 21.5],
                                                   [-158.0, 21.5], [-158.0, 21.0]]]}
    loc = ResolvedLocation(label="Oahu", kind="area", display_name="Oahu", bbox=(20.0, -159.0, 22.0, -157.0),
                           polygon=square)
    assert loc.contains(21.3, -157.8) and not loc.contains(21.8, -157.8)     # inside the box, outside the border
    assert loc.intersects_box(21.2, -157.9, 21.3, -157.7)
    assert not loc.intersects_box(20.0, -159.0, 20.5, -158.5)                # "ocean" tile is skipped
    src = OvertureSource(cfg, store, reader=lambda tile: [])
    assert all(loc.intersects_box(*t) for t in src.tiles(loc))


def test_pause_interrupts_a_download(cfg, store):
    """Pause takes effect between small batches of a map section, and a half-downloaded
    section is never saved as if complete."""
    from leadscout.sources.overture import DownloadCancelled

    class Batch:
        def __init__(self, rows):
            self.rows = rows

        def to_pylist(self):
            return self.rows

    source = OvertureSource(cfg, store)
    source._release = "test"
    batches_read = []
    stop = {"now": False}

    class Scanner:
        def to_batches(self):
            for i in range(100):
                batches_read.append(i)
                if i == 2:
                    stop["now"] = True          # the user presses Pause during the download
                yield Batch([])

    class Dataset:
        def scanner(self, **kw):
            return Scanner()

    source._dataset = Dataset()
    source.should_stop = lambda: stop["now"]
    tile = (40.0, -74.0, 40.5, -73.5)
    with pytest.raises(DownloadCancelled):
        source.tile_rows(tile)
    assert len(batches_read) <= 4                  # stopped right away, not after all 100 batches
    assert not source._cache_file(tile).exists()   # nothing half-done is cached
