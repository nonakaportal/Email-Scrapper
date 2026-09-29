"""End-to-end run with mocked place search, Overture data, websites and DNS."""

import csv
import json
from collections import Counter

import httpx
import pytest

from conftest import fixture_html
from leadscout.export import ExportFilters, select_leads, write_export
from leadscout.location import LocationResolver, LocationSpec
from leadscout.run_manager import NullProgress, RunManager
from leadscout.sources.overture import OvertureSource

NOMINATIM = [{
    "osm_type": "relation", "osm_id": 113314, "category": "boundary", "lat": "30.26", "lon": "-97.74",
    "boundingbox": ["30.10", "30.50", "-97.95", "-97.55"],
    "display_name": "Austin, Travis County, Texas, United States",
    "address": {"city": "Austin", "state": "Texas", "country_code": "us"},
}]

DENTAL = {"primary": "dental_clinic", "hierarchy": ["health_care", "outpatient_care_facility", "dental_clinic"],
          "alternates": None}


def place(pid, name, lat, lng, website=None, phone=None, email=None, brand=None, status="open", confidence=0.9,
          taxonomy=DENTAL):
    return {"id": pid, "names": {"primary": name}, "taxonomy": taxonomy,
            "websites": [website] if website else None, "emails": [email] if email else None,
            "phones": [phone] if phone else None,
            "brand": {"names": {"primary": brand}, "wikidata": None} if brand else None,
            "addresses": [{"freeform": "1 Main St", "locality": "Austin", "region": "TX", "country": "US"}],
            "confidence": confidence, "operating_status": status,
            "bbox": {"xmin": lng, "xmax": lng, "ymin": lat, "ymax": lat}}


PLACES = [
    place("ov1", "Smile Studio", 30.27, -97.74, website="https://smilestudio.com/", phone="+1 415 555 2671"),
    place("ov2", "Aspen Dental", 30.28, -97.75, website="https://aspendental.com", brand="Aspen Dental"),
    place("ov3", "No Website Dental", 30.29, -97.76),
    place("ov4", "Map Email Dental", 30.30, -97.77, email="frontdesk@mapemaildental.com"),
    place("ov5", "Broken Site Dental", 30.31, -97.78, website="brokensite.com"),
    place("ov6", "Facebook Dental", 30.32, -97.79, website="https://facebook.com/fbdental"),
    # never used: closed, unsure, outside the place, not an industry
    place("ov7", "Closed Dental", 30.33, -97.80, website="https://closed.com", status="permanently_closed"),
    place("ov8", "Maybe Dental", 30.34, -97.81, website="https://maybe.com", confidence=0.2),
    place("ov9", "Far Away Dental", 40.0, -80.0, website="https://far.com"),
    place("ov10", "City Bank", 30.27, -97.74, website="https://citybank.com",
          taxonomy={"primary": "bank_or_credit_union",
                    "hierarchy": ["services_and_business", "financial_service", "bank_or_credit_union"]}),
]


ABOUT = "<html><body><h1>About</h1><p>Family owned since 1999.</p></body></html>"


class Counters:
    def __init__(self):
        self.api = Counter()
        self.web = Counter()


def api_transport(counters):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "nominatim.test"
        counters.api["nominatim"] += 1
        assert "LeadScout" in request.headers["user-agent"]
        return httpx.Response(200, json=NOMINATIM)
    return httpx.MockTransport(handler)


def overture_reader(counters):
    def read(tile):
        counters.api["overture"] += 1
        s, w, n, e = tile
        return [p for p in PLACES if s <= p["bbox"]["ymin"] <= n and w <= p["bbox"]["xmin"] <= e]
    return read


def site_transport(counters, about=ABOUT):
    pages = {
        ("smilestudio.com", "/robots.txt"): (200, "text/plain", "User-agent: *\nDisallow: /private\n"),
        ("smilestudio.com", "/"): (200, "text/html", fixture_html("home.html")),
        ("smilestudio.com", "/contact/"): (200, "text/html", fixture_html("contact.html")),
        ("smilestudio.com", "/our-team"): (200, "text/html", fixture_html("team.html")),
        ("smilestudio.com", "/about-us/"): (200, "text/html", about),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        counters.web[f"{host}{path}"] += 1
        if host == "brokensite.com":
            raise httpx.ConnectError("connection refused", request=request)
        if host == "aspendental.com":
            raise AssertionError("chains must not be crawled")
        status, ctype, body = pages.get((host, path), (404, "text/html", "not found"))
        return httpx.Response(status, headers={"content-type": ctype}, text=body)
    return httpx.MockTransport(handler)


def fake_dns(domain):
    return ("invalid", "domain does not exist") if domain == "mapemaildental.com" else ("ok", "mail server found (MX)")


@pytest.fixture
def counters():
    return Counters()


def make_manager(cfg, store, counters, progress=None, about=ABOUT):
    api = api_transport(counters)
    return RunManager(
        cfg, store, progress,
        source=OvertureSource(cfg, store, reader=overture_reader(counters)),
        resolver=LocationResolver(cfg, store, transport=api),
        fetch_transport=site_transport(counters, about),
        dns_lookup=fake_dns,
    )


def test_full_run(cfg, store, counters):
    manager = make_manager(cfg, store, counters)
    run_id = manager.create_search_run(["dentists"], [LocationSpec("place", "Austin, Texas, USA")])
    stats = manager.execute(run_id)

    assert store.get_run(run_id)["status"] == "done"
    assert stats["found"] == 6
    # kept: Smile (site), Aspen (site, chain), Map Email (email listed), Broken (site);
    # closed, low-confidence, outside-the-place and bank listings are never even counted
    assert stats["businesses"] == 4
    assert stats["chains"] == 1
    assert stats["crawled"] == 2                      # Aspen skipped as a chain
    assert counters.web["smilestudio.com/"] == 1
    assert counters.web["smilestudio.com/robots.txt"] == 1

    emails = {r["email"]: r for r in store.query("SELECT * FROM emails")}
    assert set(emails) == {"anna@smilestudio.com", "info@smilestudio.com", "billing@smilestudio.com",
                           "dr.lee@gmail.com", "frontdesk@mapemaildental.com"}
    assert emails["anna@smilestudio.com"]["email_type"] == "personal"
    assert emails["info@smilestudio.com"]["found_on_url"] == "https://smilestudio.com/contact/"
    assert emails["billing@smilestudio.com"]["email_type"] == "role"
    assert emails["dr.lee@gmail.com"]["is_freemail"] == 1
    assert emails["frontdesk@mapemaildental.com"]["status"] == "invalid"
    assert emails["anna@smilestudio.com"]["status"] == "valid"

    smile = store.one("SELECT * FROM businesses WHERE name = 'Smile Studio'")
    assert smile["stage"] == "scored"
    assert smile["platform"] == "wordpress"
    assert smile["team_size_hint"] == 4 and smile["has_careers"] == 1
    assert json.loads(smile["socials"])["facebook"].endswith("smilestudioatx")
    assert smile["score"] == 100  # personal valid email + every business signal
    broken = store.one("SELECT * FROM businesses WHERE name = 'Broken Site Dental'")
    assert broken["error"] and "ConnectError" in broken["error"]
    assert (cfg.log_dir / f"run_{run_id}.log").exists()

    leads = select_leads(store, ExportFilters(mode="email", max_contacts=None))
    assert {lead["email"] for lead in leads} == {"anna@smilestudio.com", "info@smilestudio.com",
                                                 "billing@smilestudio.com", "dr.lee@gmail.com"}
    assert leads[0]["email"] == "anna@smilestudio.com"   # personal email ranks first
    # default: one lead per company, colleagues grouped into it
    (lead,) = select_leads(store, ExportFilters(), cfg.email_rules["freemail_domains"])
    assert lead["email"] == "anna@smilestudio.com"
    # 3 contacts max: the department inbox billing@ is the one left out
    assert sorted(lead["other_emails"]) == ["dr.lee@gmail.com", "info@smilestudio.com"]
    assert leads[0]["first_name"] == "Anna"
    assert leads[0]["source"] == "overture"


def test_rerun_adds_no_duplicates_and_uses_caches(cfg, store, counters):
    manager = make_manager(cfg, store, counters)
    first = manager.create_search_run(["dentist"], [LocationSpec("place", "Austin, Texas, USA")])
    manager.execute(first)
    businesses = store.scalar("SELECT COUNT(*) FROM businesses")
    emails = store.scalar("SELECT COUNT(*) FROM emails")

    second = manager.create_search_run(["dentist"], [LocationSpec("place", "austin, texas, usa")])
    manager.execute(second)
    assert store.scalar("SELECT COUNT(*) FROM businesses") == businesses
    assert store.scalar("SELECT COUNT(*) FROM emails") == emails
    assert counters.api == {"nominatim": 1, "overture": 1}   # second run: place + tile from cache
    assert counters.web["smilestudio.com/"] == 1        # already crawled, not fetched again


class StopAfterFirstCrawl(NullProgress):
    def __init__(self):
        self.manager = None

    def advance(self, name, n=1):
        if name == "crawl":
            self.manager.request_stop()


def test_pause_and_resume(cfg, store, counters):
    progress = StopAfterFirstCrawl()
    manager = make_manager(cfg, store, counters, progress)
    progress.manager = manager
    cfg.settings["crawler"]["global_concurrency"] = 1
    run_id = manager.create_search_run(["dentist"], [LocationSpec("place", "Austin, Texas, USA")])
    manager.execute(run_id)
    run = store.get_run(run_id)
    assert run["status"] == "paused" and run["stage"] == "crawl"
    crawled = store.scalar("SELECT COUNT(*) FROM businesses WHERE stage != 'found' AND website_status = 'ok'")
    assert crawled == 1

    manager.progress = NullProgress()
    stats = manager.execute(run_id)
    assert store.get_run(run_id)["status"] == "done"
    assert stats["crawled"] == 2
    assert counters.web["smilestudio.com/"] == 1
    assert store.scalar("SELECT COUNT(*) FROM emails") == 5


def test_export_suppression_only_new_and_templates(cfg, store, counters, tmp_path):
    manager = make_manager(cfg, store, counters)
    run_id = manager.create_search_run(["dentist"], [LocationSpec("place", "Austin, Texas, USA")])
    manager.execute(run_id)

    store.suppress("dr.lee@gmail.com", "unsubscribed")
    path, count = write_export(store, ExportFilters(run_id=run_id, mode="email"), export_dir=tmp_path)
    assert count == 3
    with path.open(encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    assert rows[0]["business_name"] == "Smile Studio"
    assert "dr.lee@gmail.com" not in {r["email"] for r in rows}

    # only-new skips what was exported before
    _, count = write_export(store, ExportFilters(only_new=True, mode="email"), export_dir=tmp_path)
    assert count == 0
    # the company was exported, so in company mode its remaining email is not a "new" lead either
    store.unsuppress("dr.lee@gmail.com")
    _, count = write_export(store, ExportFilters(only_new=True), export_dir=tmp_path)
    assert count == 0
    _, count = write_export(store, ExportFilters(only_new=True, mode="email"), export_dir=tmp_path)
    assert count == 1   # per-email mode: dr.lee was never exported

    # domain suppression, best-email mode, template and xlsx
    path, count = write_export(store, ExportFilters(), export_dir=tmp_path,
                               template="instantly", fmt="xlsx")
    assert count == 1 and path.suffix == ".xlsx"
    store.suppress("smilestudio.com")
    leads = select_leads(store, ExportFilters(statuses=["valid", "risky", "invalid"], mode="email"))
    assert [lead["email"] for lead in leads] == ["frontdesk@mapemaildental.com"]


def test_enrich_from_csv(cfg, store, counters, tmp_path):
    src = tmp_path / "sites.csv"
    src.write_text("Company,Website,Country\nSmile Studio,smilestudio.com,US\nNo site,,US\n"
                   "Broken,https://brokensite.com,US\n", encoding="utf-8")
    manager = make_manager(cfg, store, counters)
    run_id = manager.create_enrich_run(src, industry="dentist")
    stats = manager.execute(run_id)
    assert stats["businesses"] == 2          # rows without a website are skipped
    assert stats["crawled"] == 2
    assert stats["valid"] == 4
    assert counters.api == {}                 # no place or business lookups for enrich runs
    row = store.one("SELECT * FROM businesses WHERE domain = 'smilestudio.com'")
    assert row["industry"] == "dental" and store.business_sources(row["id"]) == ["csv"]


def test_company_grouping_across_listings(store):
    """Two map listings of one company (no website, same email domain) are one lead;
    two unrelated businesses on gmail stay separate."""
    from leadscout.export.exporter import company_key
    from leadscout.sources.base import BusinessCandidate

    def biz(ref, name, lat):
        bid, _ = store.upsert_business(BusinessCandidate(name=name, source="osm", source_ref=ref,
                                                         lat=lat, lng=0.0))
        return bid

    a = biz("n/1", "Acme Plumbing North", 1.0)
    b = biz("n/2", "Acme Plumbing South", 2.0)
    c = biz("n/3", "Joe Plumbing", 3.0)
    d = biz("n/4", "Sue Plumbing", 4.0)
    for bid, email in ((a, "jane@acmeplumbing.com"), (b, "mark@acmeplumbing.com"),
                       (c, "joeplumbs@gmail.com"), (d, "sueplumbs@gmail.com")):
        store.add_email(bid, email, email_type="personal", source="osm", found_on_url=None)
        store.conn.execute("UPDATE emails SET status = 'valid', score = 50 WHERE email = ?", (email,))
    store.conn.commit()
    freemail = {"gmail.com"}
    leads = select_leads(store, ExportFilters(), freemail)
    assert len(leads) == 3
    acme = next(l for l in leads if "acmeplumbing.com" in l["email"])
    assert len(acme["other_emails"]) == 1
    assert company_key(1, None, "x@gmail.com", freemail) != company_key(2, None, "y@gmail.com", freemail)


def test_contact_limit_when_collecting(cfg, store, counters):
    """Only the best 3 contacts per company are collected: people, then info@, then sales@."""
    cfg.settings["filters"]["max_contacts_per_company"] = 3
    manager = make_manager(cfg, store, counters)
    run_id = manager.create_search_run(["dentist"], [LocationSpec("place", "Austin, Texas, USA")])
    manager.execute(run_id)
    smile = store.one("SELECT id FROM businesses WHERE name = 'Smile Studio'")["id"]
    kept = {r["email"] for r in store.emails_for_business(smile)}
    # anna@ and dr.lee@ are people, info@ is the front desk; billing@ (department) is dropped
    assert kept == {"anna@smilestudio.com", "dr.lee@gmail.com", "info@smilestudio.com"}


def test_contact_limit_when_exporting(store):
    """Even if a company has more stored (e.g. after merging records), exports cap it."""
    from leadscout.sources.base import BusinessCandidate

    bid, _ = store.upsert_business(BusinessCandidate(name="Acme", source="osm", source_ref="n/1",
                                                     website="https://acme.com"))
    for i, (email, score) in enumerate([("a@acme.com", 90), ("b@acme.com", 80), ("c@acme.com", 70),
                                        ("d@acme.com", 60), ("e@acme.com", 50)]):
        store.add_email(bid, email, email_type="personal", source="website", found_on_url=None)
        store.conn.execute("UPDATE emails SET status = 'valid', score = ? WHERE email = ?", (score, email))
    store.conn.commit()
    (lead,) = select_leads(store, ExportFilters())
    assert lead["email"] == "a@acme.com" and lead["other_emails"] == ["b@acme.com", "c@acme.com"]
    rows = select_leads(store, ExportFilters(mode="email"))
    assert [r["email"] for r in rows] == ["a@acme.com", "b@acme.com", "c@acme.com"]
    assert len(select_leads(store, ExportFilters(mode="email", max_contacts=None))) == 5


def test_contacts_with_designations(cfg, store, counters):
    """The owner named on the website becomes the lead; colleagues keep their designations."""
    manager = make_manager(cfg, store, counters, about=fixture_html("about.html"))
    run_id = manager.create_search_run(["dentist"], [LocationSpec("place", "Austin, Texas, USA")])
    manager.execute(run_id)
    (lead,) = [l for l in select_leads(store, ExportFilters(max_contacts=None), cfg.email_rules["freemail_domains"])
               if l["business_name"] == "Smile Studio"]
    assert lead["email"] == "anna@smilestudio.com"
    assert (lead["contact_name"], lead["designation"], lead["decision_maker"]) == (
        "Anna Lopez", "Owner & Lead Dentist", "yes")
    assert lead["first_name"] == "Anna"
    others = lead["other_contacts"]
    assert "Mark Chen (Office Manager) <mark@smilestudio.com>" in others
    assert "Billing / Accounts <billing@smilestudio.com>" in others
    assert "General inbox <info@smilestudio.com>" in others
    assert "dr.lee@gmail.com" in others                 # no name on the site for Dr. Lee: left plain
    (limited,) = [l for l in select_leads(store, ExportFilters(), cfg.email_rules["freemail_domains"])
                  if l["business_name"] == "Smile Studio"]
    assert limited["email"] == "anna@smilestudio.com" and len(limited["other_contacts"]) == 2   # 3 contacts max
    data = write_export(store, ExportFilters(only_new=False), export_dir=cfg.export_dir)[0].read_text(
        encoding="utf-8-sig")
    header = data.splitlines()[0].split(",")
    assert header[:13] == ["business_name", "email", "contact_name", "designation", "decision_maker",
                           "owner_name", "owner_title", "email_2", "name_2", "designation_2",
                           "email_3", "name_3", "designation_3"]
    assert (lead["owner_name"], lead["owner_title"]) == ("Anna Lopez", "Owner & Lead Dentist")


def test_own_email_never_a_lead_and_listed_email_gets_page(cfg, store, counters, monkeypatch):
    # The map lists anna@ for Smile Studio; the crawler then also sees it on the website.
    places = [dict(p) for p in PLACES]
    places[0]["emails"] = ["anna@smilestudio.com"]
    monkeypatch.setattr(__import__("test_pipeline"), "PLACES", places)
    manager = make_manager(cfg, store, counters)
    run_id = manager.create_search_run(["dentists"], [LocationSpec("place", "Austin, Texas, USA")])
    manager.execute(run_id)
    anna = store.one("SELECT source, found_on_url FROM emails WHERE email = 'anna@smilestudio.com'")
    assert anna["source"] == "overture"
    assert anna["found_on_url"].startswith("https://smilestudio.com/")

    business = store.one("SELECT id FROM businesses WHERE name = 'Smile Studio'")["id"]
    assert cfg.contact_email in manager.own_emails
    kept = manager._pick_contacts(business, [(cfg.contact_email, "personal", "https://smilestudio.com/contact"),
                                             ("new@smilestudio.com", "personal", None)])
    assert [c[0] for c in kept] == ["new@smilestudio.com"]
