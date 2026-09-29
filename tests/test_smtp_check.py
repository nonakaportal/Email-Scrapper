"""Mailbox check (full verification) with a fake mail server: no network."""

import pytest

from leadscout.location import LocationSpec
from leadscout.verify import smtp_check
from leadscout.verify.smtp_check import MailboxChecker, _reply, provider_key, smtp_probe

from test_pipeline import Counters, make_manager


class FakeSMTP:
    """Stands in for smtplib.SMTP: knows some mailboxes, optionally accepts everything."""
    mailboxes = {"anna@acme.com", "info@acme.com"}
    catch_all = False
    sender_reply = (250, b"OK")
    sessions = []

    def __init__(self, host, port, timeout=None, local_hostname=None):
        assert port == 25
        self.host, self.helo, self.asked = host, local_hostname, []
        FakeSMTP.sessions.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo_or_helo_if_needed(self):
        pass

    def mail(self, sender):
        self.sender = sender
        return self.sender_reply

    def rcpt(self, address):
        self.asked.append(address)
        if self.catch_all or address in self.mailboxes:
            return 250, b"2.1.5 OK"
        return 550, b"5.1.1 The email account that you tried to reach does not exist"


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSMTP.catch_all = False
    FakeSMTP.sender_reply = (250, b"OK")
    FakeSMTP.sessions = []
    monkeypatch.setattr(smtp_check.smtplib, "SMTP", FakeSMTP)
    return FakeSMTP


def test_probe_one_connection_per_domain(fake_smtp):
    results = smtp_probe("mx.acme.com", ["anna@acme.com", "left@acme.com"], "me.com", "verify@me.com")
    assert results["anna@acme.com"][0] == "valid"
    assert results["left@acme.com"][0] == "invalid"
    (session,) = fake_smtp.sessions
    assert session.helo == "me.com" and session.sender == "verify@me.com"
    assert len(session.asked) == 3                      # two real addresses + one made-up


def test_catch_all_is_risky(fake_smtp):
    fake_smtp.catch_all = True
    results = smtp_probe("mx.acme.com", ["anyone@acme.com"], "me.com", "verify@me.com")
    assert results["anyone@acme.com"] == ("risky", "catch-all: the mail server accepts any address")


def test_refused_sender_is_unknown(fake_smtp):
    fake_smtp.sender_reply = (554, b"5.7.1 Client host blocked using Spamhaus")
    results = smtp_probe("mx.acme.com", ["anna@acme.com"], "me.com", "verify@me.com")
    assert results["anna@acme.com"][0] == "unknown"


def test_connection_failure_is_unknown(monkeypatch):
    def refuse(*a, **k):
        raise ConnectionRefusedError("port 25 closed")
    monkeypatch.setattr(smtp_check.smtplib, "SMTP", refuse)
    assert smtp_probe("mx.acme.com", ["a@acme.com"], "me.com", "v@me.com")["a@acme.com"][0] == "unknown"


@pytest.mark.parametrize("code,text,status", [
    (250, "2.1.5 OK", "valid"),
    (550, "5.1.1 <x@acme.com>: Recipient address rejected: User unknown", "invalid"),
    (550, "5.4.1 Recipient address rejected: Access denied. AS(201806281)", "invalid"),   # Microsoft 365
    (550, "No such user here", "invalid"),
    (554, "5.7.1 Service unavailable; Client host [1.2.3.4] blocked using Spamhaus", "unknown"),
    (550, "Rejected due to poor IP reputation", "unknown"),
    (550, "Access denied - Invalid HELO name (See RFC5321 4.1.1.1)", "unknown"),   # about us, not the mailbox
    (450, "4.2.1 Try again later", "unknown"),
    (421, "Too many connections", "unknown"),
])
def test_reply_meaning(code, text, status):
    assert _reply(code, text.encode())[0] == status


def test_provider_key():
    assert provider_key("aspmx.l.google.com") == provider_key("alt1.aspmx.l.google.com") == "google.com"


def make_checker(cfg, store, answers, sleeps=None, port=True):
    calls = []

    def probe(host, emails, helo, sender, timeout):
        calls.append((host, list(emails)))
        return {e: answers.get(e, ("invalid", "mailbox does not exist (550)")) for e in emails}

    checker = MailboxChecker(cfg, store, probe=probe, mx_lookup=lambda d: "mx1.mailhost.net",
                             port_check=lambda: port,
                             sleep=(sleeps.append if sleeps is not None else lambda s: None))
    return checker, calls


def test_checker_caches_answers(cfg, store):
    answers = {"anna@acme.com": ("valid", "mailbox exists (mail server confirmed)"),
               "x@slow.com": ("unknown", "mail server said try later (451)")}
    checker, calls = make_checker(cfg, store, answers)
    first = dict(checker.check_many({"acme.com": ["anna@acme.com", "left@acme.com"], "slow.com": ["x@slow.com"]}))
    assert first["acme.com"]["anna@acme.com"][0] == "valid"
    assert first["acme.com"]["left@acme.com"][0] == "invalid"
    assert len(calls) == 2
    again = dict(checker.check_many({"acme.com": ["anna@acme.com"], "slow.com": ["x@slow.com"]}))
    assert again["acme.com"]["anna@acme.com"][0] == "valid"
    assert len(calls) == 3                               # acme from cache; the "try later" is asked again


def test_rate_limit_per_provider(cfg, store):
    sleeps = []
    checker, _ = make_checker(cfg, store, {}, sleeps)
    checker.workers = 1
    list(checker.check_many({f"d{i}.com": [f"a@d{i}.com"] for i in range(3)}))
    assert len(sleeps) == 2 and all(s > 5 for s in sleeps)   # 6 a minute to the same provider


def test_full_run_asks_mail_servers(cfg, store):
    answers = {"anna@smilestudio.com": ("valid", "mailbox exists (mail server confirmed)"),
               "info@smilestudio.com": ("risky", "catch-all: the mail server accepts any address")}
    checker, calls = make_checker(cfg, store, answers)
    manager = make_manager(cfg, store, Counters())
    manager._mailbox_checker = checker
    run_id = manager.create_search_run(["dentists"], [LocationSpec("place", "Austin, Texas, USA")],
                                       verification="full")
    stats = manager.execute(run_id)

    emails = {r["email"]: r for r in store.query("SELECT * FROM emails")}
    assert emails["anna@smilestudio.com"]["status"] == "valid"
    assert emails["anna@smilestudio.com"]["mailbox_check"] == "valid"
    assert emails["info@smilestudio.com"]["status"] == "risky"
    assert emails["info@smilestudio.com"]["is_catch_all"] == 1
    assert emails["billing@smilestudio.com"]["status"] == "invalid"          # server: no such mailbox
    assert emails["dr.lee@gmail.com"]["mailbox_check"] is None               # Gmail is never asked
    assert emails["frontdesk@mapemaildental.com"]["mailbox_check"] is None   # already invalid (no domain)
    assert [host for host, _ in calls] == ["mx1.mailhost.net"]               # one connection per domain
    assert stats["mailbox_checked"] == 3 and stats["mailbox_confirmed"] == 1 and stats["mailbox_todo"] == 0


def test_basic_run_does_not_ask(cfg, store):
    checker, calls = make_checker(cfg, store, {})
    manager = make_manager(cfg, store, Counters())
    manager._mailbox_checker = checker
    run_id = manager.create_search_run(["dentists"], [LocationSpec("place", "Austin, Texas, USA")])
    manager.execute(run_id)
    assert calls == []
    assert store.get_run(run_id)["settings"]["verification"] == "basic"


def test_blocked_port_keeps_basic_result(cfg, store):
    checker, calls = make_checker(cfg, store, {}, port=False)
    manager = make_manager(cfg, store, Counters())
    manager._mailbox_checker = checker
    run_id = manager.create_search_run(["dentists"], [LocationSpec("place", "Austin, Texas, USA")],
                                       verification="full")
    stats = manager.execute(run_id)
    assert calls == [] and stats["mailbox_checked"] == 0
    assert store.scalar("SELECT status FROM emails WHERE email = 'billing@smilestudio.com'") == "valid"


def test_check_mailboxes_on_old_run(cfg, store):
    manager = make_manager(cfg, store, Counters())
    run_id = manager.create_search_run(["dentists"], [LocationSpec("place", "Austin, Texas, USA")])
    manager.execute(run_id)
    checker, calls = make_checker(cfg, store, {"anna@smilestudio.com": ("valid", "ok")})
    manager._mailbox_checker = checker
    stats = manager.check_mailboxes(run_id)
    assert stats["mailbox_checked"] == 3 and len(calls) == 1
    assert store.scalar("SELECT status FROM emails WHERE email = 'billing@smilestudio.com'") == "invalid"


def test_bad_mode(cfg, store):
    manager = make_manager(cfg, store, Counters())
    with pytest.raises(ValueError):
        manager.create_search_run(["dentists"], [LocationSpec("place", "Austin, Texas, USA")],
                                  verification="extreme")


def test_bare_computer_name_is_not_used_as_helo(cfg, store, monkeypatch):
    """A PC name like "Abhishek" gets rejected by mail servers ("need fully-qualified hostname")."""
    monkeypatch.setattr(smtp_check.socket, "getfqdn", lambda: "Abhishek")
    monkeypatch.setattr(smtp_check, "public_helo", lambda: "[203.0.113.7]")
    checker = MailboxChecker(cfg, store, port_check=lambda: True)
    assert checker.helo == ""
    assert checker.available() and checker.helo == "[203.0.113.7]"
