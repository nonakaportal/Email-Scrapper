"""Is the business still operating, and which email is the latest."""

from datetime import date

import pytest

from leadscout.crawler.extract import find_closed_notice, find_latest_date_year
from leadscout.export import ExportFilters, select_leads
from leadscout.location import LocationSpec
from leadscout.scoring.score import business_activity
from leadscout.sources.overture import listing_freshness
from leadscout.verify.lists import contact_priority

from test_pipeline import PLACES, Counters, make_manager

TODAY = date(2026, 9, 28)


@pytest.mark.parametrize("text,year", [
    ("Holiday hours 2025: closed Dec 25", 2025),
    ("Posted March 12, 2026 by Anna", 2026),
    ("Updated 12 Mar 2024", 2024),
    ("Event on 03/14/2026", 2026),
    ("Visit us at 2024 Main St, call 555-2019", None),     # street numbers are not dates
    ("See you in March 2031", None),                        # not in the future
])
def test_latest_date_year(text, year):
    assert find_latest_date_year(text, this_year=2026) == year


@pytest.mark.parametrize("text,kind", [
    ("After 30 wonderful years we are permanently closed. Thank you!", "closed"),
    ("This domain is for sale! Buy this domain today", "parked"),
    ("This Account has been suspended. Contact your hosting provider.", "suspended"),
    ("Open Mon-Fri, closed Sundays", None),
    ("Temporarily closed for renovation", None),
])
def test_closed_notice(text, kind):
    found = find_closed_notice(text)
    assert (found is None) if kind is None else (kind in found)


def site(**kw):
    base = {"website_status": "ok", "error": None, "copyright_year": None, "latest_date_year": None,
            "closed_notice": None, "listing_updated": None, "listing_provider": None}
    return {**base, **kw}


@pytest.mark.parametrize("business,label", [
    (site(copyright_year=2026), "active"),
    (site(listing_updated="2026-01-10", listing_provider="meta"), "active"),
    (site(), "likely active"),
    (site(error="HTTP 403"), "likely active"),                            # site exists, just blocked us
    (site(website_status="none", listing_updated="2025-12-01"), "likely active"),
    (site(website_status="none", listing_updated="2021-05-01"), "unclear"),
    (site(error="ConnectTimeout: "), "unclear"),
    (site(error="website domain does not exist (NXDOMAIN)"), "likely closed"),
    (site(error="ConnectError: [Errno 11001] getaddrinfo failed"), "unclear"),   # not confirmed by DNS
    (site(copyright_year=2026, closed_notice="the website says the business has closed"), "likely closed"),
])
def test_business_activity(business, label):
    assert business_activity(business, today=TODAY)[0] == label


def test_mailbox_confirmed_counts_as_active():
    assert business_activity(site(), [{"mailbox_check": "valid"}], today=TODAY)[0] == "active"


def test_listing_freshness_ignores_overture_calculations():
    sources = [{"dataset": "Foursquare", "update_time": "2025-06-25T00:00:00.000"},
               {"dataset": "meta", "update_time": "2026-02-01T00:00:00Z"},
               {"dataset": "Overture-signals", "update_time": "2026-09-14T15:43:16Z"}]
    assert listing_freshness(sources) == ("2026-02-01", "meta")
    assert listing_freshness(None) == (None, None)


def test_latest_email_first():
    old = {"email": "shop@x.com", "email_type": "generic", "status": "valid", "seen_at": "2021-03-01"}
    new = {"email": "hello@x.com", "email_type": "generic", "status": "valid", "seen_at": "2026-09-28"}
    assert sorted([old, new], key=contact_priority)[0]["email"] == "hello@x.com"


def test_run_sets_activity_and_export_filters(cfg, store, monkeypatch):
    places = [dict(p) for p in PLACES]
    places[3]["sources"] = [{"dataset": "Foursquare", "update_time": "2019-01-01T00:00:00"}]  # Map Email Dental
    monkeypatch.setattr(__import__("test_pipeline"), "PLACES", places)
    manager = make_manager(cfg, store, Counters())
    run_id = manager.create_search_run(["dentists"], [LocationSpec("place", "Austin, Texas, USA")])
    manager.execute(run_id)
    activity = {r["name"]: r["activity"] for r in store.query("SELECT name, activity FROM businesses")}
    assert activity["Smile Studio"] in ("active", "likely active")
    assert activity["Map Email Dental"] == "unclear"         # no website, listing from 2019
    smile = store.one("SELECT seen_at FROM emails WHERE email = 'anna@smilestudio.com'")
    assert smile["seen_at"] == date.today().isoformat()      # seen on the website today

    freemail = cfg.email_rules["freemail_domains"]
    everyone = select_leads(store, ExportFilters(statuses=["valid", "invalid", "risky", "unknown"]), freemail)
    working = select_leads(store, ExportFilters(statuses=["valid", "invalid", "risky", "unknown"],
                                                activity=["active", "likely active"]), freemail)
    assert {l["business_name"] for l in everyone} - {l["business_name"] for l in working} == {"Map Email Dental"}
    lead = next(l for l in everyone if l["business_name"] == "Map Email Dental")
    assert lead["email_found_in"] == "map listing (Foursquare, 2019-01-01)"
    assert lead["business_activity"] == "unclear"


def test_dead_domain_needs_dns_confirmation(cfg, store):
    manager = make_manager(cfg, store, Counters())       # fake DNS: only mapemaildental.com is gone
    err = "ConnectError: [Errno 11001] getaddrinfo failed"
    assert manager._confirm_dead_domain(err, "mapemaildental.com") == manager.DOMAIN_GONE
    assert manager._confirm_dead_domain(err, "smilestudio.com") == err
    assert manager._confirm_dead_domain("HTTP 404", "mapemaildental.com") == "HTTP 404"


@pytest.mark.parametrize("old,new,name,kind", [
    ("statenlaw.com", "https://statenlaw.net/", "Scamardella Gervasi", "related"),
    ("walshec.com", "https://www.hugedomains.com/domain_profile.cfm?d=walshec.com", "Walsh Electrical", "parked"),
    ("garden-of-eder.com", "https://www.asconsulting.top/video-qr-codes/", "Garden of Eden", "other"),
    ("a-1servicemasters.com", "https://jimhallhvac.com/", "A-1 Service Masters", "other"),
    ("smilestudio.com", "https://www.smilestudio.com/home", "Smile Studio", None),
])
def test_redirect_kind(old, new, name, kind):
    from leadscout.normalize import redirect_kind
    assert redirect_kind(old, new, name) == kind


def test_taken_over_website_gives_nothing(cfg, store):
    from leadscout.crawler.pages import SiteResult
    from leadscout.crawler.people import Person
    from leadscout.sources.base import BusinessCandidate
    manager = make_manager(cfg, store, Counters())
    bid, _ = store.upsert_business(BusinessCandidate("Garden of Eden", "overture", "g1", city="Mission",
                                                     website="http://garden-of-eder.com/garden.html"))
    res = SiteResult(start_url="http://garden-of-eder.com/garden.html",
                     final_url="https://www.asconsulting.top/video-qr-codes/",
                     emails={"simon.weiner@asconsulting.top": "https://www.asconsulting.top/video-qr-codes/"},
                     people=[Person("Simon Weiner", "Founder", "simon.weiner@asconsulting.top")],
                     phones=["+15555550123"], copyright_year=2026)
    manager._save_crawl(store.get_business(bid), res)
    b = store.get_business(bid)
    assert store.emails_for_business(bid) == []
    assert b["redirected_to"] == "asconsulting.top" and b["owner_name"] is None and b["phone"] is None
    assert business_activity(b, today=TODAY)[0] == "unclear"


@pytest.mark.parametrize("old,new,name", [
    ("kellyfrancisillumination.com", "https://kfillum.com/", "Kelly Francis Illumination"),
    ("awirrigation.com", "https://aw-landscapeservice.com/", "A&W Irrigation Service"),
    ("bonsaiarbor.com", "https://bonsaiarbor.wixsite.com/home", "Bonsai Arbor"),
])
def test_renamed_site_is_related(old, new, name):
    from leadscout.normalize import redirect_kind
    assert redirect_kind(old, new, name) == "related"
