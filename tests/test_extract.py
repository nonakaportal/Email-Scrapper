import pytest

from conftest import fixture_html
from leadscout.crawler.extract import (
    clean_email,
    decode_cfemail,
    extract_page,
    find_copyright_year,
    find_emails,
    link_priority,
)


def test_homepage_fixture():
    page = extract_page(fixture_html("home.html"), "https://smilestudio.com/", "US")
    assert "anna@smilestudio.com" in page.emails            # [at]/[dot] obfuscation
    assert page.emails["anna@smilestudio.com"] == "obfuscated"
    assert not any(e.endswith(".png") for e in page.emails)  # logo@2x.png is not an email
    assert page.platform == "wordpress"
    assert page.copyright_year == 2025
    assert page.socials["facebook"] == "https://www.facebook.com/smilestudioatx"
    assert page.socials["instagram"].startswith("https://www.instagram.com/smilestudioatx")
    assert "linkedin" in page.socials
    assert "x" not in page.socials


def test_contact_fixture():
    page = extract_page(fixture_html("contact.html"), "https://smilestudio.com/contact/", "US")
    assert page.emails["info@smilestudio.com"] == "mailto"
    assert page.emails["billing@smilestudio.com"] == "cloudflare"
    assert "dr.lee@gmail.com" in page.emails
    assert "hello@pixelagency.io" in page.emails   # filtered later as unrelated
    assert "+15125550199" in page.phones


def test_team_page_counts_members_and_careers():
    page = extract_page(fixture_html("team.html"), "https://smilestudio.com/our-team", "US")
    assert page.team_count == 4
    assert page.has_careers


def test_decode_cfemail():
    assert decode_cfemail("1a7873767673747d5a697773767f696e6f7e737534797577") == "billing@smilestudio.com"
    assert decode_cfemail("zz") is None


@pytest.mark.parametrize("text,expected", [
    ("john (at) acme (dot) co (dot) uk", "john@acme.co.uk"),
    ("mary{at}acme{dot}com", "mary@acme.com"),
    ("sales at acme dot com", "sales@acme.com"),
    ("info @ acme.com", "info@acme.com"),
])
def test_obfuscated_emails(text, expected):
    assert expected in find_emails("", text)


def test_ambiguous_plain_at_is_ignored():
    found = find_emails("", "Meet us at acme.com for coffee")
    assert found == {}


def test_html_entities_and_escapes():
    raw = 'x = "\\u003einfo@acme.com\\u003c"; y = "sales&#64;acme.com"; z = "hr%40acme.com"'
    found = find_emails(raw, "")
    assert {"info@acme.com", "sales@acme.com", "hr@acme.com"} <= set(found)


@pytest.mark.parametrize("raw,expected", [
    ("Info@Acme.COM.", "info@acme.com"),
    ("mailto:bob@acme.com?subject=hi", "bob@acme.com"),
    ("a..b@acme.com", None),
    ("nope", None),
    ("u003ebob@acme.com", "bob@acme.com"),
])
def test_clean_email(raw, expected):
    assert clean_email(raw) == expected


def test_copyright_year_ignores_future():
    assert find_copyright_year("© 2019-2023 Acme", this_year=2026) == 2023
    assert find_copyright_year("Copyright 2031 Acme", this_year=2026) is None


def test_link_priority():
    assert link_priority("https://a.com/contact-us", "") == 0
    assert link_priority("https://a.com/x", "Kontakt") == 0
    assert link_priority("https://a.com/about", "") == 1
    assert link_priority("https://a.com/services", "Services") is None
    assert link_priority("https://a.com/contact.pdf", "") is None


def test_multi_location_needs_locator_or_several_location_pages():
    from leadscout.crawler.extract import detect_multi_location
    assert not detect_multi_location([("https://a.com/locations", "Locations")])
    assert not detect_multi_location([("https://a.com/location/", "Our Location")])
    assert detect_multi_location([("https://a.com/find-a-location", "Find a location")])
    assert detect_multi_location([("https://a.com/x", "Store Locator")])
    assert detect_multi_location([(f"https://a.com/locations/{c}", c) for c in ("austin", "dallas", "waco")])
    assert not detect_multi_location([(f"https://a.com/locations/{c}", c) for c in ("austin", "dallas")])


@pytest.mark.parametrize("raw,email", [
    ("294-3993emailinfo@secrethawaiitours.com", "info@secrethawaiitours.com"),
    ("808-879-0080vacationrentals@destinationmaui.net", "vacationrentals@destinationmaui.net"),
    ("(808) 555-1234info@a.com", "info@a.com"),
    ("2019hes@gmail.com", "2019hes@gmail.com"),          # digits without a phone shape are kept
    ("5551234john@x.com", "5551234john@x.com"),
])
def test_phone_glued_to_email(raw, email):
    assert clean_email(raw) == email
