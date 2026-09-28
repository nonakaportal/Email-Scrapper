import pytest

from leadscout.verify import EmailRules, verify_email
from leadscout.verify.dns_check import DomainChecker


@pytest.fixture
def rules(cfg):
    return EmailRules(cfg.email_rules)


@pytest.mark.parametrize("email,reason_part", [
    ("logo@2x.png", "file name"),
    ("abc@o123.ingest.sentry.io", "platform"),
    ("support@wixpress.com", "platform"),
    ("you@business.com", "placeholder"),
    ("noreply@business.com", "no-reply"),
    ("0123456789abcdef0123@business.com", "machine"),
])
def test_junk(rules, email, reason_part):
    assert reason_part in rules.is_junk(email)


def test_not_junk(rules):
    assert rules.is_junk("anna@smilestudio.com") is None


@pytest.mark.parametrize("email,etype", [
    ("info@acme.com", "generic"),
    ("hello@acme.com", "generic"),
    ("sales@acme.com", "role"),
    ("billing@acme.com", "role"),
    ("anna@acme.com", "personal"),
    ("anna.lopez@acme.com", "personal"),
])
def test_email_type(rules, email, etype):
    assert rules.email_type(email) == etype


def test_related(rules):
    site = ["https://www.smilestudioaustin.com/"]
    assert rules.is_related("anna@smilestudioaustin.com", site, "Smile Studio")
    assert rules.is_related("anna@mail.smilestudioaustin.com", site, "Smile Studio")
    assert rules.is_related("dr.lee@gmail.com", site, "Smile Studio")          # freemail
    assert rules.is_related("info@smilestudio.com", site, "Smile Studio")      # label contained
    assert not rules.is_related("hello@pixelagency.io", site, "Smile Studio")  # web agency
    # redirect target counts as the business's own domain
    assert rules.is_related("x@newbrand.com", site + ["https://newbrand.com/"], "Smile Studio")


def test_verify_statuses(rules):
    ok = ("ok", "mail server found (MX)")
    assert verify_email("anna@acme.com", rules, ok) == ("valid", "mail server found (MX)")
    assert verify_email("anna@@acme.com", rules, ok)[0] == "invalid"
    assert verify_email("anna@acme.com", rules, ("invalid", "domain does not exist")) == (
        "invalid", "domain does not exist")
    assert verify_email("anna@acme.com", rules, ("unknown", "timeout"))[0] == "unknown"
    assert verify_email("x@mailinator.com", rules, ok)[0] == "risky"
    assert verify_email("anna@acme.com", rules, None) == ("valid", "syntax ok (domain not checked)")


def test_domain_checker_caches_definitive_results(cfg, store):
    calls = []

    def lookup(domain):
        calls.append(domain)
        return {"good.com": ("ok", "mx"), "bad.com": ("invalid", "nx")}.get(domain, ("unknown", "timeout"))

    checker = DomainChecker(cfg, store, lookup=lookup)
    first = checker.check_many(["good.com", "bad.com", "flaky.com"])
    assert first == {"good.com": ("ok", "mx"), "bad.com": ("invalid", "nx"), "flaky.com": ("unknown", "timeout")}
    calls.clear()
    checker.check_many(["good.com", "bad.com", "flaky.com"])
    assert calls == ["flaky.com"]   # unknown results are retried, others cached


def test_email_type_uses_business_context(rules):
    site = ["https://boulderdentalcenter.com/"]
    assert rules.email_type("patient@boulderdentalcenter.com", "Boulder Dental Center", site) == "generic"
    assert rules.email_type("boulderdental@gmail.com", "Boulder Dental Center", site) == "generic"
    assert rules.email_type("smilestudio@gmail.com", "Smile Studio", []) == "generic"
    assert rules.email_type("anna@boulderdentalcenter.com", "Boulder Dental Center", site) == "personal"
    assert rules.email_type("officemanager@acme.com") == "role"
