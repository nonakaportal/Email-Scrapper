from leadscout.scoring import ChainDetector, score_lead
from leadscout.sources.base import BusinessCandidate


def cand(ref, name="Smile Studio", website="https://smilestudio.com", phone=None, lat=30.0, lng=-97.0, **kw):
    return BusinessCandidate(name=name, source="osm", source_ref=ref, website=website, phone=phone,
                             lat=lat, lng=lng, country="US", **kw)


def test_dedupe_same_object_domain_phone_and_proximity(store):
    a, created = store.upsert_business(cand("node/1"))
    assert created
    # same OSM object again -> same row, no new location
    assert store.upsert_business(cand("node/1")) == (a, False)
    # same domain, other object right next door (node + building) -> same place
    b, created = store.upsert_business(cand("way/2", website="http://www.smilestudio.com/about", lat=30.0001))
    assert (b, created) == (a, False)
    assert store.get_business(a)["location_count"] == 1
    # same domain far away -> another branch of the same business
    store.upsert_business(cand("node/3", lat=30.2))
    assert store.get_business(a)["location_count"] == 2
    # same phone, no website
    p, _ = store.upsert_business(cand("node/4", name="Lake Dental", website=None, phone="+14155552671"))
    assert store.upsert_business(cand("node/5", name="Lake Dental Care", website=None,
                                      phone="(415) 555-2671", lat=31.0))[0] == p
    # same name within 100 m, no website/phone
    n, _ = store.upsert_business(cand("node/6", name="Bright Dental LLC", website=None, lat=40.0, lng=-70.0))
    assert store.upsert_business(cand("node/7", name="Bright Dental", website=None,
                                      lat=40.0005, lng=-70.0))[0] == n
    # same name far away -> different business
    assert store.upsert_business(cand("node/8", name="Bright Dental", website=None,
                                      lat=41.0, lng=-70.0))[0] != n
    assert store.scalar("SELECT COUNT(*) FROM businesses") == 4


def test_social_only_website_is_not_a_domain(store, cfg):
    non_business = cfg.get("non_business_domains")
    b1, _ = store.upsert_business(cand("n/1", name="A", website="https://facebook.com/a"),
                                  non_business_domains=non_business)
    b2, _ = store.upsert_business(cand("n/2", name="B", website="https://facebook.com/b", lat=35.0),
                                  non_business_domains=non_business)
    assert b1 != b2
    row = store.get_business(b1)
    assert row["website_status"] == "social_only" and row["domain"] is None


def biz(**kw):
    base = {"name": "Smile Studio", "name_norm": "smile studio", "tags": "{}", "domain": "smilestudio.com",
            "location_count": 1, "multi_location_site": 0, "is_chain": 0, "phone": "+15125550123",
            "copyright_year": 2025, "socials": '{"facebook": "x"}', "team_size_hint": 4,
            "has_careers": 0}
    base.update(kw)
    return base


def test_score_full_marks():
    weights = {"email_valid": 25, "email_risky": 10, "email_personal": 15, "email_generic": 5,
               "email_role": 3, "independent": 20, "single_location": 10, "has_phone": 10,
               "website_active": 10, "website_active_years": 2, "has_social": 5, "small_team": 5,
               "small_team_max_people": 50}
    score, detail = score_lead(biz(), {"status": "valid", "email_type": "personal"}, weights, this_year=2026)
    assert score == 100
    assert sum(detail.values()) == 100
    score, detail = score_lead(
        biz(is_chain=1, location_count=4, phone=None, copyright_year=2019, socials=None,
            team_size_hint=None, has_careers=1),
        {"status": "risky", "email_type": "generic"}, weights, this_year=2026)
    assert detail == {"email_status": 10, "email_type": 5, "independent": 0, "single_location": 0,
                      "has_phone": 0, "website_active": 0, "has_social": 0, "small_team": 0}
    assert score == 15


def test_chain_detection():
    d = ChainDetector(["aspen dental", "westerndental.com", "h&r block"], repeat_threshold=3)
    assert d.detect(biz(tags='{"brand": "Aspen Dental"}'))[0]
    assert d.detect(biz(name="Aspen Dental - Austin", name_norm="aspen dental austin"))[0]
    assert d.detect(biz(name="H&R Block", name_norm="h and r block"))[0]
    assert d.detect(biz(domain="austin.westerndental.com"))[0]
    assert d.detect(biz(location_count=3))[0]
    assert d.detect(biz(), {"smile studio": 3})[0]
    assert d.detect(biz(multi_location_site=1))[0]
    assert d.detect(biz()) == (False, None)
    assert not d.detect(biz(name="Aspen Dentalworks", name_norm="aspen dentalworks"))[0]


def test_discard_run_removes_only_its_own_businesses(store):
    runs = [store.create_run(kind="search", industries=["x"], location_input="x", location_type="place",
                             sources=["osm"], settings={}) for _ in range(2)]
    only, _ = store.upsert_business(cand("n/1", name="Only Here", website="https://onlyhere.com"), run_id=runs[0])
    shared, _ = store.upsert_business(cand("n/2", name="Shared", website="https://shared.com", lat=5.0), run_id=runs[0])
    store.link_business(runs[0], only, None, None)
    store.link_business(runs[0], shared, None, None)
    store.link_business(runs[1], shared, None, None)
    store.add_email(only, "a@onlyhere.com", email_type="personal", source="website", found_on_url=None)
    store.add_email(shared, "b@shared.com", email_type="personal", source="website", found_on_url=None)
    store.record_export({}, "x.csv", [store.one("SELECT id FROM emails WHERE email = 'a@onlyhere.com'")[0]])
    store.add_run_tasks(runs[0], [("loc", "x")])

    assert store.discard_preview(runs[0]) == {"businesses": 1, "emails": 1, "shared": 1}
    store.discard_run(runs[0])
    assert store.get_run(runs[0]) is None
    assert store.get_business(only) is None
    assert store.get_business(shared) is not None
    assert [r["email"] for r in store.query("SELECT email FROM emails")] == ["b@shared.com"]
    assert store.run_business_count(runs[1]) == 1
    assert store.run_tasks(runs[0]) == []


def test_backup_and_restore(store, tmp_path):
    import pytest
    from leadscout.db.store import Store
    from leadscout.sources.base import BusinessCandidate
    bid, _ = store.upsert_business(BusinessCandidate("Acme", "overture", "a1", website="https://acme.com"))
    store.add_email(bid, "info@acme.com", email_type="generic", source="website",
                    found_on_url="https://acme.com/", is_freemail=False)
    data = store.backup_bytes()

    fresh = Store(tmp_path / "fresh.db")                  # e.g. the app after a restart: empty
    assert fresh.scalar("SELECT COUNT(*) FROM emails") == 0
    counts = fresh.restore_bytes(data)
    assert counts == {"businesses": 1, "emails": 1, "runs": 0}
    assert fresh.one("SELECT email FROM emails")["email"] == "info@acme.com"
    with pytest.raises(ValueError):
        fresh.restore_bytes(b"not a database at all")
    assert fresh.scalar("SELECT COUNT(*) FROM emails") == 1   # a bad file changes nothing
