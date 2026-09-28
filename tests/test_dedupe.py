"""Every company is stored once."""

import pytest

from leadscout.dedupe import dedupe_companies
from leadscout.normalize import company_domain
from leadscout.scoring import ChainDetector
from leadscout.sources.base import BusinessCandidate


def cand(ref, name, website=None, lat=None, emails=(), **kw):
    return BusinessCandidate(name=name, source="osm", source_ref=ref, website=website,
                             lat=lat, lng=0.0 if lat is not None else None, emails=list(emails), **kw)


@pytest.fixture
def detector(cfg):
    from leadscout.scoring import industry_words
    return ChainDetector(cfg.known_chains, 3, cfg.get("size_check"), industry_words(cfg.industries))


def test_generic_names(detector):
    assert detector.is_generic("accounting and tax services")
    assert detector.is_generic("family dental care")
    assert detector.is_generic("cpa")
    assert not detector.is_generic("liberty income tax")
    assert not detector.is_generic("atax accounting and financial services")
    assert not detector.is_generic("edward jones")


@pytest.mark.parametrize("url,expected", [
    ("https://local.jacksonhewitt.com/tx/", "jacksonhewitt.com"),
    ("https://www.smile.co.uk", "smile.co.uk"),
    ("https://joes.wixsite.com/plumbing", "joes.wixsite.com"),
    ("https://abc-bookkeeping.voolt.com", "abc-bookkeeping.voolt.com"),
    ("https://x.business.site", "x.business.site"),
    ("https://shop.myshopify.com", "shop.myshopify.com"),
])
def test_company_domain(url, expected):
    assert company_domain(url) == expected


def test_subdomain_is_same_company_but_site_builders_are_not(store):
    a, _ = store.upsert_business(cand("n/1", "Jackson Hewitt", "https://www.jacksonhewitt.com", lat=1.0))
    b, created = store.upsert_business(cand("n/2", "Jackson Hewitt Tax Service",
                                            "https://local.jacksonhewitt.com/tx/austin", lat=5.0))
    assert (b, created) == (a, False)
    assert store.get_business(a)["location_count"] == 2
    joe, _ = store.upsert_business(cand("n/3", "Joe Plumbing", "https://joes.wixsite.com/site"))
    sue, _ = store.upsert_business(cand("n/4", "Sue Plumbing", "https://sues.wixsite.com/site"))
    assert joe != sue


def test_listing_with_company_email_joins_that_company(store):
    store.freemail = {"gmail.com"}
    acme, _ = store.upsert_business(cand("n/1", "Acme Plumbing", "https://acmeplumbing.com", lat=1.0))
    branch, created = store.upsert_business(cand("n/2", "Acme Plumbing South", lat=9.0,
                                                 emails=["south@acmeplumbing.com"]))
    assert (branch, created) == (acme, False)
    solo, created = store.upsert_business(cand("n/3", "Joe Plumbing", lat=3.0, emails=["joe@gmail.com"]))
    assert created and solo != acme


def test_redirect_merges_into_the_target_company(store, detector):
    bt, _ = store.upsert_business(cand("n/1", "Baker Tilly", "https://www.bakertilly.com"))
    kraft, _ = store.upsert_business(cand("n/2", "Kraft CPAs", "https://kraftcpas.com"))
    store.add_email(kraft, "info@kraftcpas.com", email_type="generic", source="website", found_on_url=None)
    run_id = store.create_run(kind="search", industries=[], location_input="x", location_type="place",
                              sources=["osm"], settings={})
    store.link_business(run_id, kraft, "accountant", "x")
    store.update_business(kraft, final_url="https://www.bakertilly.com/", stage="crawled")
    merges = dedupe_companies(store, detector)
    assert [(m.kept, m.dropped) for m in merges] == [("Baker Tilly", "Kraft CPAs")]
    assert store.get_business(kraft) is None
    assert store.one("SELECT business_id FROM emails WHERE email = 'info@kraftcpas.com'")[0] == bt
    assert store.run_business_count(run_id) == 1                       # run link moved over
    assert set(store.business_sources(bt)) == {"osm"}
    assert store.scalar("SELECT COUNT(*) FROM business_sources WHERE business_id = ?", (bt,)) == 2


def test_redirect_to_a_social_site_is_not_a_merge(store, detector):
    store.upsert_business(cand("n/1", "Some Page", "https://facebook.com/x"),
                          non_business_domains=["facebook.com"])
    other, _ = store.upsert_business(cand("n/2", "Tiny Cafe", "https://tinycafe.com"))
    store.update_business(other, final_url="https://www.facebook.com/tinycafe")
    assert dedupe_companies(store, detector, ["facebook.com"]) == []


def test_chain_branches_become_one_company(store, detector):
    ids = [store.upsert_business(cand(f"n/{i}", "Liberty Income Tax", lat=float(i * 3)))[0] for i in range(4)]
    store.upsert_business(cand("n/9", "Liberty Income Tax", "https://www.libertytax.com", lat=40.0))
    # two unrelated firms that happen to share a generic name
    store.upsert_business(cand("n/20", "CPA", lat=10.0))
    store.upsert_business(cand("n/21", "CPA", lat=20.0))
    # a generic trade name used by unrelated firms, even 3 times, is not a chain
    for i in range(3):
        store.upsert_business(cand(f"n/4{i}", "Accounting & Tax Services", lat=50.0 + i * 5))
    # an independent business name used in two cities is not a chain
    store.upsert_business(cand("n/30", "Main Street Dental", lat=30.0))
    store.upsert_business(cand("n/31", "Main Street Dental", lat=35.0))
    merges = dedupe_companies(store, detector)
    liberty = store.query("SELECT * FROM businesses WHERE name = 'Liberty Income Tax'")
    assert len(liberty) == 1
    assert liberty[0]["domain"] == "libertytax.com" and liberty[0]["location_count"] == 5
    assert len(merges) == 4
    assert store.scalar("SELECT COUNT(*) FROM businesses WHERE name = 'CPA'") == 2
    assert store.scalar("SELECT COUNT(*) FROM businesses WHERE name = 'Main Street Dental'") == 2
    assert store.scalar("SELECT COUNT(*) FROM businesses WHERE name = 'Accounting & Tax Services'") == 3
    generic = store.one("SELECT * FROM businesses WHERE name = 'Accounting & Tax Services'")
    assert detector.detect(generic, {"accounting and tax services": 3}) == (False, None)
    assert ids  # noqa


def test_dedupe_database_fixes_old_subdomain_rows(cfg, store):
    """Rows stored before company domains existed (full host names) get merged."""
    from leadscout.run_manager import RunManager

    a, _ = store.upsert_business(cand("n/1", "LPL Financial", "https://www.lpl.com"))
    b, _ = store.upsert_business(cand("n/2", "LPL Financial Advisor", "https://advisor.lpl.com"))
    assert a == b                                       # new rows dedupe at insert time
    c, _ = store.upsert_business(cand("n/3", "Old Row", "https://branch.example-firm.com"))
    d, _ = store.upsert_business(cand("n/4", "Example Firm", "https://www.example-firm.com"))
    assert c == d
    # simulate an old database: full host names in the domain column
    store.conn.execute("UPDATE businesses SET domain = 'www-legacy.example-firm.com' WHERE id = ?", (c,))
    e, _ = store.upsert_business(cand("n/5", "Legacy", "https://legacy.other-firm.com"))
    store.conn.execute("UPDATE businesses SET domain = 'legacy.other-firm.com' WHERE id = ?", (e,))
    f = store.conn.execute(
        "INSERT INTO businesses (name, name_norm, website, domain, website_status, stage, first_seen, updated_at)"
        " VALUES ('Other Firm', 'other firm', 'https://other-firm.com/', 'other-firm.com', 'ok', 'found', 'x', 'x')"
    ).lastrowid
    store.conn.commit()
    merges = RunManager(cfg, store).dedupe_database()
    assert any("other-firm.com" in m.reason for m in merges)
    assert store.scalar("SELECT COUNT(*) FROM businesses WHERE domain LIKE '%other-firm.com'") == 1
    assert store.get_business(c)["domain"] == "example-firm.com"
    assert f
