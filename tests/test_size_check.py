"""Large-company detection: Wikidata evidence, website signals, non-commercial domains."""

import httpx
import pytest

from leadscout.crawler.extract import extract_page
from leadscout.scoring import ChainDetector
from leadscout.sources.base import BusinessCandidate
from leadscout.sources.wikidata import WikidataClient, site_matches


def entity(qid, label, sites, employees=None, listed=False, types=(), wikis=()):
    claims = {"P856": [{"mainsnak": {"datavalue": {"value": s}}} for s in sites],
              "P31": [{"mainsnak": {"datavalue": {"value": {"id": t}}}} for t in types]}
    if employees:
        claims["P1128"] = [{"mainsnak": {"datavalue": {"value": {"amount": f"+{employees}"}}}}]
    if listed:
        claims["P414"] = [{"mainsnak": {"datavalue": {"value": {"id": "Q13677"}}}}]
    return {"id": qid, "labels": {"en": {"value": label}}, "claims": claims,
            "sitelinks": {w: {} for w in wikis}}


ENTITIES = {
    "Q1": entity("Q1", "Ernst & Young", ["https://www.ey.com"], employees=300000, wikis=["enwiki", "dewiki"]),
    "Q2": entity("Q2", "Entrepreneur of the Year", ["http://www.ey.com/US/en/award"], wikis=["enwiki"]),
    "Q3": entity("Q3", "Baker Tilly", ["https://www.bakertilly.com/"], wikis=["enwiki"]),
    "Q4": entity("Q4", "Fiducial", ["https://www.fiducial.fr"], employees=17500),
    "Q5": entity("Q5", "Smile Studio (film)", ["https://smilestudiofilm.com"], wikis=["enwiki"]),
    "Q6": entity("Q6", "Category:EY", [], types=["Q4167836"]),
}
SEARCH = {"Ernst & Young": ["Q1", "Q2", "Q6"], "Baker Tilly": ["Q3"], "Fiducial": ["Q4"],
          "Smile Studio": ["Q5"]}


@pytest.fixture
def wikidata(cfg, store):
    cfg.settings["size_check"]["wikidata"] = True
    calls = []

    def handler(request):
        params = dict(request.url.params)
        calls.append(params["action"])
        assert "mailto:" in request.headers["user-agent"]      # Wikimedia policy
        if params["action"] == "wbsearchentities":
            ids = SEARCH.get(params["search"], [])
            return httpx.Response(200, json={"search": [{"id": i} for i in ids]})
        ids = params["ids"].split("|")
        return httpx.Response(200, json={"entities": {i: ENTITIES[i] for i in ids}})

    client = WikidataClient(cfg, store, transport=httpx.MockTransport(handler))
    client.calls = calls
    return client


def test_lookup_matches_by_website(wikidata):
    ey = wikidata.lookup("ey.com", "Ernst & Young")
    assert ey["employees"] == 300000 and ey["wikipedias"] == 2 and ey["label"] == "Ernst & Young"
    assert wikidata.lookup("bakertilly.com", "Baker Tilly")["wikipedias"] == 1
    # same brand on another country domain, same name
    assert wikidata.lookup("fiducial.com", "Fiducial")["employees"] == 17500
    # a film with the same name but a different website is not the business
    assert wikidata.lookup("smilestudio.com", "Smile Studio") == {}
    assert wikidata.lookup("unknown-cpa.com", "Anthony Leone CPA") == {}


def test_lookup_is_cached(wikidata):
    wikidata.lookup("ey.com", "Ernst & Young")
    n = len(wikidata.calls)
    wikidata.lookup("ey.com", "Ernst & Young")
    assert len(wikidata.calls) == n


def test_legal_suffix_retry(wikidata):
    assert wikidata.lookup("bakertilly.com", "Baker Tilly, LLP")["label"] == "Baker Tilly"


def test_site_matches():
    assert site_matches(["https://www.ey.com/en_gl"], "ey.com", "EY", "Ernst & Young")
    assert site_matches(["https://fiducial.fr"], "fiducial.com", "Fiducial", "Fiducial")
    assert not site_matches(["https://fiducial.fr"], "fiducial.com", "Fiducial Tax Help", "Fiducial")


def biz(**kw):
    base = {"name": "Smile Studio", "name_norm": "smile studio", "tags": "{}", "domain": "smilestudio.com",
            "location_count": 1, "multi_location_site": 0}
    base.update(kw)
    return base


def test_detector_large_company_rules(cfg):
    d = ChainDetector([], 3, cfg.get("size_check"))
    assert "300,000 employees" in d.detect(biz(org_info={"label": "EY", "employees": 300000}))[1]
    assert d.detect(biz(org_info={"label": "X", "employees": 40}))[0] is False
    assert "stock exchange" in d.detect(biz(org_info={"label": "X", "listed": True}))[1]
    assert "multinational" in d.detect(biz(org_info={"label": "X", "types": ["multinational company"]}))[1]
    assert "Wikipedia" in d.detect(biz(org_info='{"label": "Baker Tilly", "wikipedias": 1}'))[1]
    assert d.detect(biz(org_info="{}")) == (False, None)
    assert "non-commercial" in d.detect(biz(domain="cashier.rice.edu"))[1]
    assert "investor relations" in d.detect(biz(investor_relations=1))[1]
    assert "9 language" in d.detect(biz(intl_languages=9))[1]
    assert d.detect(biz(intl_languages=2)) == (False, None)
    relaxed = ChainDetector([], 3, {**cfg.get("size_check"), "exclude_wikipedia_notable": False})
    assert relaxed.detect(biz(org_info={"label": "Baker Tilly", "wikipedias": 1})) == (False, None)


def test_known_large_firms(cfg):
    d = ChainDetector(cfg.known_chains, 3, cfg.get("size_check"))
    assert d.detect(biz(name="Ernst & Young", name_norm="ernst and young", domain="ey.com"))[0]
    assert d.detect(biz(name="Deloitte Tax LLP", name_norm="deloitte tax"))[0]
    assert not d.detect(biz(name="Crowe Plumbing", name_norm="crowe plumbing"))[0]


def test_website_signals():
    html = """<html><head>
      <link rel="alternate" hreflang="en-us" href="/us"><link rel="alternate" hreflang="de-de" href="/de">
      <link rel="alternate" hreflang="fr-fr" href="/fr"><link rel="alternate" hreflang="x-default" href="/">
    </head><body><a href="/investor-relations">Investor Relations</a></body></html>"""
    page = extract_page(html, "https://big.example/")
    assert page.intl_languages == 3 and page.investor_relations
    realtor = extract_page('<a href="/investors">Investors</a>', "https://realty.example/")
    assert not realtor.investor_relations


def test_run_size_stage_flags_large_companies(cfg, store, wikidata):
    from leadscout.run_manager import RunManager

    run_id = store.create_run(kind="enrich", industries=[], location_input="x", location_type="csv",
                              sources=["csv"], settings={"filters": {"exclude_chains": True}})
    for i, (name, site) in enumerate([("Ernst & Young", "https://www.ey.com"),
                                      ("Anthony Leone CPA", "https://unknown-cpa.com")]):
        bid, _ = store.upsert_business(BusinessCandidate(name=name, source="csv", source_ref=str(i),
                                                         website=site))
        store.link_business(run_id, bid, None, None)
        store.add_email(bid, f"info@{site.split('//')[1].replace('www.', '')}", email_type="generic",
                        source="website", found_on_url=None)   # only businesses with emails are checked
    cfg.known_chains = []          # prove Wikidata alone catches it
    manager = RunManager(cfg, store, wikidata=wikidata)
    manager.recheck(run_id)
    ey = store.one("SELECT * FROM businesses WHERE domain = 'ey.com'")
    cpa = store.one("SELECT * FROM businesses WHERE domain = 'unknown-cpa.com'")
    assert ey["is_chain"] == 1 and "300,000" in ey["chain_reason"]
    assert cpa["is_chain"] == 0 and cpa["org_info"] == "{}"
    assert cpa["score"] is not None


def test_size_check_needs_contact_email(cfg, wikidata):
    cfg.settings["contact_email"] = ""
    assert not wikidata.available


def test_migration_adds_columns(tmp_path):
    import sqlite3

    from leadscout.db.store import Store
    path = tmp_path / "old.db"
    Store(path).close()
    con = sqlite3.connect(path)       # simulate a database from before these columns existed
    for col in ("org_info", "intl_languages", "investor_relations"):
        con.execute(f"ALTER TABLE businesses DROP COLUMN {col}")
    con.commit()
    con.close()
    s = Store(path)
    cols = {r[1] for r in s.conn.execute("PRAGMA table_info(businesses)")}
    assert {"org_info", "intl_languages", "investor_relations"} <= cols
    s.close()
