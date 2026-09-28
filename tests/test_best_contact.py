"""Best email to contact per company, and social links in the export."""

from leadscout.export import ExportFilters, render_export, select_leads
from leadscout.location import LocationSpec
from leadscout.sources.base import BusinessCandidate
from leadscout.verify import EmailRules
from leadscout.verify.lists import contact_priority

from test_pipeline import PLACES, Counters, make_manager


def best(*emails):
    return sorted(emails, key=contact_priority)[0]["email"]


def test_priority_order():
    owner = {"email": "anna@x.com", "email_type": "personal", "decision_maker": True, "status": "valid"}
    named = {"email": "mark@x.com", "email_type": "personal", "contact_name": "Mark Chen", "status": "valid"}
    info = {"email": "info@x.com", "email_type": "generic", "status": "valid", "found_on_url": "https://x.com/"}
    sales = {"email": "sales@x.com", "email_type": "role", "status": "valid", "found_on_url": "https://x.com/"}
    social = {"email": "facebook@x.com", "email_type": "role", "status": "valid"}
    catch_all = {"email": "boss@x.com", "email_type": "personal", "status": "risky"}
    assert best(owner, named, info) == "anna@x.com"
    assert best(named, info, sales) == "mark@x.com"
    assert best(info, sales, social) == "info@x.com"
    assert best(sales, social) == "sales@x.com"
    assert best(catch_all, info) == "info@x.com"            # deliverable first


def test_bladerunner_case(cfg):
    rules = EmailRules(cfg.email_rules)
    assert rules.email_type("facebook@bladerunnerfarms.com") == "role"
    office = {"email": "office@bladerunnerfarms.com", "email_type": "generic", "status": "valid",
              "found_on_url": "https://www.bladerunnerfarms.com/"}
    facebook = {"email": "facebook@bladerunnerfarms.com", "email_type": rules.email_type("facebook@b.com"),
                "status": "valid", "found_on_url": None}
    assert best(facebook, office) == "office@bladerunnerfarms.com"


def test_better_email_replaces_weaker_one(cfg, store):
    cfg.settings["filters"]["max_contacts_per_company"] = 2
    manager = make_manager(cfg, store, Counters())
    bid, _ = store.upsert_business(BusinessCandidate("Acme", "overture", "a1", website="https://acme.com"))
    manager._add_listed_emails(bid, ["facebook@acme.com", "sales@acme.com"], "overture")
    picked = manager._pick_contacts(bid, [("anna@acme.com", "personal", "https://acme.com/team")],
                                    decision_makers={"anna@acme.com"})
    store.conn.commit()
    assert [c[0] for c in picked] == ["anna@acme.com"]
    assert {r["email"] for r in store.emails_for_business(bid)} == {"sales@acme.com"}   # facebook@ made room


def test_map_socials_and_export_columns(cfg, store, monkeypatch):
    places = [dict(p) for p in PLACES]
    places[0]["socials"] = ["https://www.facebook.com/smilestudioatx", "https://twitter.com/smilestudio"]
    monkeypatch.setattr(__import__("test_pipeline"), "PLACES", places)
    manager = make_manager(cfg, store, Counters())
    run_id = manager.create_search_run(["dentists"], [LocationSpec("place", "Austin, Texas, USA")])
    manager.execute(run_id)
    leads = select_leads(store, ExportFilters(run_id=run_id), cfg.email_rules["freemail_domains"])
    smile = next(l for l in leads if l["business_name"] == "Smile Studio")
    assert smile["facebook"] == "https://www.facebook.com/smilestudioatx"      # from the map
    assert smile["x_twitter"] == "https://twitter.com/smilestudio"
    assert smile["website"].startswith("https://smilestudio.com")
    header = render_export(leads).decode("utf-8-sig").splitlines()[0].split(",")
    for col in ("website", "facebook", "instagram", "linkedin", "x_twitter", "youtube", "tiktok"):
        assert col in header
    for template in ("instantly", "smartlead", "lemlist", "hubspot"):
        head = render_export(leads, template=template).decode("utf-8-sig").splitlines()[0].lower()
        assert "facebook" in head and "website" in head


def test_senior_people_first():
    director = {"email": "maria@x.com", "email_type": "personal", "contact_name": "Maria Lopez",
                "designation": "Director of Operations", "decision_maker": True, "status": "valid"}
    manager = {"email": "tom@x.com", "email_type": "personal", "contact_name": "Tom Wu",
               "designation": "Office Manager", "status": "valid"}
    ceo = {"email": "ann@x.com", "email_type": "personal", "contact_name": "Ann Reyes",
           "designation": "CEO", "decision_maker": True, "status": "valid"}
    info = {"email": "info@x.com", "email_type": "generic", "status": "valid", "found_on_url": "https://x.com/"}
    assert [c["email"] for c in sorted([info, manager, director, ceo], key=contact_priority)] == [
        "ann@x.com", "maria@x.com", "tom@x.com", "info@x.com"]


def test_people_list_most_senior_first():
    from leadscout.crawler.people import Person, people_list
    people = [Person("Tom Wu", "Office Manager"), Person("Sara Diaz", "Hygienist"),
              Person("Maria Lopez", "Director of Operations", "maria@x.com"), Person("Ann Reyes", "Founder")]
    listed = people_list(people)
    assert [p["name"] for p in listed] == ["Ann Reyes", "Maria Lopez", "Tom Wu", "Sara Diaz"]
    assert listed[1]["email"] == "maria@x.com"


def test_key_people_in_export(cfg, store):
    from conftest import fixture_html
    from test_pipeline import make_manager as manager_for
    manager = manager_for(cfg, store, Counters(), about=fixture_html("about.html"))
    run_id = manager.create_search_run(["dentist"], [LocationSpec("place", "Austin, Texas, USA")])
    manager.execute(run_id)
    (lead,) = [l for l in select_leads(store, ExportFilters(), cfg.email_rules["freemail_domains"])
               if l["business_name"] == "Smile Studio"]
    assert lead["key_people"].startswith("Anna Lopez (Owner & Lead Dentist) <anna@smilestudio.com>")
    assert "Mark Chen (Office Manager) <mark@smilestudio.com>" in lead["key_people"]
    assert "Sara Diaz (Dental Hygienist)" in lead["key_people"]          # listed even without an email
