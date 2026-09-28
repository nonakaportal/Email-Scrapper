import pytest

from leadscout.normalize import (
    domain_matches,
    haversine_m,
    normalize_domain,
    normalize_name,
    normalize_phone,
    normalize_url,
    registered_domain,
)


@pytest.mark.parametrize("raw,expected", [
    ("https://www.SmileStudio.com/contact", "smilestudio.com"),
    ("smilestudio.com", "smilestudio.com"),
    ("http://shop.smile.co.uk:8080/x", "shop.smile.co.uk"),
    ("", None),
    ("localhost", None),
])
def test_normalize_domain(raw, expected):
    assert normalize_domain(raw) == expected


def test_registered_domain_uses_public_suffix():
    assert registered_domain("https://shop.smile.co.uk/") == "smile.co.uk"
    assert registered_domain("www.example.com") == "example.com"


@pytest.mark.parametrize("raw,expected", [
    ("www.example.com", "https://www.example.com/"),
    ("http://Example.com/about", "http://example.com/about"),
    ("https://a.com;https://b.com", "https://a.com/"),
    ("mailto:x@y.com", None),
    ("not a url", None),
])
def test_normalize_url(raw, expected):
    assert normalize_url(raw) == expected


def test_normalize_phone_e164():
    assert normalize_phone("(415) 555-2671", "US") == "+14155552671"
    assert normalize_phone("+44 20 7946 0958") == "+442079460958"
    assert normalize_phone("020 7946 0958", "GB") == "+442079460958"
    assert normalize_phone("12", "US") is None
    assert normalize_phone("tel:+14155552671") == "+14155552671"


@pytest.mark.parametrize("raw,expected", [
    ("Smile Studio, LLC", "smile studio"),
    ("Smile Studio Inc.", "smile studio"),
    ("Café Olé Ltd", "cafe ole"),
    ("Smith & Sons Plumbing Co.", "smith and sons plumbing"),
])
def test_normalize_name(raw, expected):
    assert normalize_name(raw) == expected


def test_domain_matches_subdomains():
    assert domain_matches("sentry.wixpress.com", ["wixpress.com"])
    assert domain_matches("wixpress.com", ["wixpress.com"])
    assert not domain_matches("notwixpress.com", ["wixpress.com"])


def test_haversine():
    # ~111 km per degree of latitude
    assert 110_000 < haversine_m(0, 0, 1, 0) < 112_000
