"""Guessed emails for decision makers: kept only when the mail server confirms them,
always after written emails, shown in yellow."""

import io

import pytest
from openpyxl import load_workbook

from leadscout.export import ExportFilters, render_export, select_leads
from leadscout.sources.base import BusinessCandidate
from leadscout.verify.guess import candidates, infer_pattern, name_parts
from leadscout.verify.smtp_check import MailboxChecker

from test_pipeline import Counters, make_manager


def test_name_parts():
    assert name_parts("Dr. Chris A. Martinez-Lopez") == ("chris", "martinezlopez")
    assert name_parts("José Núñez") == ("jose", "nunez")
    assert name_parts("Tom") is None


@pytest.mark.parametrize("known,pattern", [
    ([("rebecca@acme.com", None), ("info@acme.com", None)], "first"),
    ([("anna.lopez@acme.com", "Anna Lopez")], "first.last"),
    ([("alopez@acme.com", "Anna Lopez")], "flast"),
    ([("info@acme.com", None), ("sales@acme.com", None)], None),
])
def test_infer_pattern(known, pattern):
    found = infer_pattern(known)
    assert (found[0] if found else None) == pattern


def test_candidates_learned_format_first():
    cands = candidates("Chris Martinez", "acme.com", "first.last")
    assert cands[0] == ("chris.martinez@acme.com", "first.last")
    assert ("chris@acme.com", "first") in cands


def setup(cfg, store, server_answers):
    """Central Electric: rebecca@ is written on the site, the President has no published email."""
    manager = make_manager(cfg, store, Counters())
    run_id = manager.create_search_run(["dentists"], [__import__("leadscout.location").location.LocationSpec(
        "place", "Austin, Texas, USA")], verification="full")
    bid, _ = store.upsert_business(BusinessCandidate("Central Electric", "overture", "ce1",
                                                     website="https://centralelectricsa.com"))
    store.link_business(run_id, bid, "construction", "San Antonio")
    store.add_email(bid, "rebecca@centralelectricsa.com", email_type="personal", source="website",
                    found_on_url="https://centralelectricsa.com/contact", is_freemail=False)
    store.conn.execute("UPDATE emails SET status = 'valid', status_reason = 'mail server found (MX)' "
                       "WHERE email = 'rebecca@centralelectricsa.com'")
    store._update("businesses", bid, {"people": [
        {"name": "Chris Martinez", "title": "President", "email": "", "level": 1},
        {"name": "Joe Morin", "title": "Senior Superintendent", "email": "", "level": 3}]})
    store.conn.commit()
    asked = []

    def probe(host, emails, helo, sender, timeout):
        asked.extend(emails)
        return {e: server_answers.get(e, ("invalid", "mailbox does not exist (550)")) for e in emails}

    manager._mailbox_checker = MailboxChecker(cfg, store, probe=probe, mx_lookup=lambda d: "mx.test",
                                              port_check=lambda: True, sleep=lambda s: None)
    return manager, run_id, bid, asked


def test_confirmed_guess_is_kept_after_the_written_email(cfg, store):
    manager, run_id, bid, asked = setup(cfg, store, {
        "chris@centralelectricsa.com": ("valid", "mailbox exists (mail server confirmed)")})
    assert manager._guess_emails(run_id) == 1
    assert asked[0] == "chris@centralelectricsa.com"              # the company's format (rebecca@) first
    assert not any("joe" in e for e in asked)                     # managers aren't guessed, only leaders
    row = store.one("SELECT * FROM emails WHERE source = 'guessed'")
    assert (row["email"], row["contact_name"], row["designation"], row["status"]) == (
        "chris@centralelectricsa.com", "Chris Martinez", "President", "valid")
    assert "like rebecca@centralelectricsa.com" in row["status_reason"]

    (lead,) = select_leads(store, ExportFilters(run_id=run_id), cfg.email_rules["freemail_domains"])
    assert lead["email"] == "rebecca@centralelectricsa.com"       # written email stays the main one
    assert lead["other_contacts"] == ["Chris Martinez (President) <chris@centralelectricsa.com>"]
    assert lead["guessed"] == {"chris@centralelectricsa.com"}

    wb = load_workbook(io.BytesIO(render_export([lead], fmt="xlsx")), rich_text=True)
    ws = wb["Leads"]
    header = [c.value for c in ws[1]]
    email_cell = ws.cell(row=2, column=header.index("email") + 1)
    assert email_cell.fill.fgColor.rgb.endswith("C6EFCE")         # green: written
    col = header.index("email_2") + 1                               # colleagues get their own columns
    assert header[col - 1:col + 2] == ["email_2", "name_2", "designation_2"]
    assert [ws.cell(row=2, column=col + i).value for i in range(3)] == [
        "chris@centralelectricsa.com", "Chris Martinez", "President"]
    assert ws.cell(row=2, column=col).fill.fgColor.rgb.endswith("FFEB9C")      # yellow: guessed
    assert "other_contacts" not in header
    assert "guessed_emails" not in header                         # no separate column
    assert "Colour key" in wb.sheetnames


def test_nothing_kept_without_confirmation(cfg, store):
    manager, run_id, bid, asked = setup(cfg, store, {})               # server says no to every guess
    assert manager._guess_emails(run_id) == 0
    assert store.one("SELECT 1 FROM emails WHERE source = 'guessed'") is None


def test_catch_all_confirms_nothing(cfg, store):
    catch_all = ("risky", "catch-all: the mail server accepts any address")
    manager, run_id, bid, asked = setup(cfg, store, {e: catch_all for e, _ in candidates(
        "Chris Martinez", "centralelectricsa.com", "first")})
    assert manager._guess_emails(run_id) == 0


def test_blocked_port_guesses_nothing(cfg, store):
    manager, run_id, bid, asked = setup(cfg, store, {})
    manager._mailbox_checker.port_check = lambda: False
    manager._mailbox_checker._port_ok = None
    assert manager._guess_emails(run_id) == 0 and asked == []


def test_guess_is_main_only_without_written_email(cfg, store):
    manager, run_id, bid, asked = setup(cfg, store, {
        "chris@centralelectricsa.com": ("valid", "mailbox exists (mail server confirmed)")})
    manager._guess_emails(run_id)
    store.conn.execute("DELETE FROM emails WHERE email = 'rebecca@centralelectricsa.com'")
    store.conn.commit()
    (lead,) = select_leads(store, ExportFilters(run_id=run_id), cfg.email_rules["freemail_domains"])
    assert lead["email"] == "chris@centralelectricsa.com" and lead["email_found_in"].startswith("guessed")
    ws = load_workbook(io.BytesIO(render_export([lead], fmt="xlsx")))["Leads"]
    header = [c.value for c in ws[1]]
    assert ws.cell(row=2, column=header.index("email") + 1).fill.fgColor.rgb.endswith("FFEB9C")   # yellow


def test_other_contacts_columns_csv():
    """Each colleague gets email_N, name_N, designation_N; leads with fewer colleagues leave them empty."""
    import csv
    leads = [
        {"business_name": "A", "email": "a@a.com", "others": [
            {"email": "b@a.com", "name": "Bea Diaz", "designation": "Owner"},
            {"email": "info@a.com", "name": "", "designation": "General inbox"}]},
        {"business_name": "B", "email": "x@b.com", "others": []},
    ]
    rows = list(csv.reader(io.StringIO(render_export(
        leads, columns=["business_name", "email", "other_contacts"]).decode("utf-8-sig"))))
    assert rows[0] == ["business_name", "email", "email_2", "name_2", "designation_2",
                       "email_3", "name_3", "designation_3"]
    assert rows[1] == ["A", "a@a.com", "b@a.com", "Bea Diaz", "Owner", "info@a.com", "", "General inbox"]
    assert rows[2] == ["B", "x@b.com", "", "", "", "", "", ""]
