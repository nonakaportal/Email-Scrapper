"""Export leads to CSV / Excel with filters, suppression and "only new"."""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from leadscout.db.store import Store, loads
from leadscout.export.templates import TEMPLATES
from leadscout.normalize import domain_matches
from leadscout.verify.lists import contact_priority

VALID_STATUSES = ("valid", "risky", "invalid", "unknown")
# Two leading consonants that start real first names ("chris", "brad");
# anything else like "jsmith" is probably initial + surname.
_NAME_ONSETS = {"bl", "br", "ch", "cl", "cr", "dr", "fl", "fr", "gl", "gr", "kl", "kr", "ph",
                "pl", "pr", "sc", "sh", "sk", "sl", "sm", "sn", "sp", "st", "sv", "sw", "th",
                "tr", "tw", "wh", "wr", "zh"}
_VOWELS = set("aeiouy")


@dataclass
class ExportFilters:
    statuses: list[str] = field(default_factory=lambda: ["valid"])
    min_score: int = 0
    industries: list[str] = field(default_factory=list)
    cities: list[str] = field(default_factory=list)
    email_types: list[str] = field(default_factory=list)
    run_id: int | None = None
    only_new: bool = False
    include_chains: bool = False
    mode: str = "company"        # company = one row per company | email = one row per email
    max_contacts: int | None = 3  # contacts per company (company: 1 main + others); None = no limit
    # Business activity to include (active, likely active, unclear, likely closed); empty = all.
    # Businesses not scored for activity yet count as "unclear".
    activity: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.mode = normalize_mode(self.mode)


def normalize_mode(mode: str | None) -> str:
    """'company' (also old 'best') or 'email' (also old 'all')."""
    return "email" if (mode or "").lower() in ("email", "all", "per-email") else "company"


def found_in(r) -> str:
    """Where the address came from, in words: a website page or a map listing (provider, date)."""
    if r["found_on_url"]:
        return r["found_on_url"]
    if r["email_source"] == "csv":
        return "your CSV"
    if r["email_source"] == "guessed":
        return "guessed, confirmed by the mail server"
    if r["email_source"] == "overture":
        detail = ", ".join(x for x in (r["listing_provider"], r["seen_at"]) if x)
        return "map listing" + (f" ({detail})" if detail else "")
    return r["email_source"] or ""


def company_key(business_id: int, business_domain: str | None, email: str,
                freemail: set[str] | None = None) -> str:
    """Emails that share a company key are one lead: same business, same
    website domain, or the same company email domain (not gmail & co)."""
    if business_domain:
        return business_domain.lower()
    email_domain = email.rsplit("@", 1)[-1].lower()
    if freemail is not None and not domain_matches(email_domain, freemail):
        return email_domain
    return f"business:{business_id}"


def guess_first_name(email: str, email_type: str | None) -> str:
    """Best-effort first name from a personal address (anna@, anna.smith@)."""
    if email_type != "personal":
        return ""
    local = email.split("@", 1)[0].lower()
    parts = [p for p in re.split(r"[._\-]+", local) if p]
    if len(parts) >= 2:
        first = parts[0]
        if first.isalpha() and len(first) >= 2:
            return first.capitalize()
        return ""
    if not local.isalpha() or not 3 <= len(local) <= 8:
        return ""
    if local[0] not in _VOWELS and local[1] not in _VOWELS and local[:2] not in _NAME_ONSETS:
        return ""
    return local.capitalize()


def main_designation(row) -> str:
    """An email's designation (main and email_2, email_3 ...). When it has none and belongs to no one in particular
    (bobsplumbing@gmail.com, cga@x.com ...), the business owner's name goes here instead, so the
    row still says who to address. A person's own email, or "Sales", "General inbox" ..., stays."""
    if not row["contact_name"] and not row["designation"] and row["owner_name"]:
        return row["owner_name"] + (f" ({row['owner_title']})" if row["owner_title"] else "")
    return row["designation"] or ""


def describe_contact(row) -> str:
    """'Mark Chen (Office Manager) <mark@x.com>' / 'Billing / Accounts <billing@x.com>' / 'info@x.com'."""
    who = row["contact_name"] or ""
    role = row["designation"] or ""
    if who and role:
        label = f"{who} ({role})"
    else:
        label = who or role
    return f"{label} <{row['email']}>" if label else row["email"]


def select_leads(store: Store, filters: ExportFilters,
                 freemail_domains: list[str] | set[str] | None = None) -> list[dict]:
    """Leads matching the filters. In company mode each company is one lead:
    its highest-scoring email, with colleagues' addresses in other_emails."""
    where = ["e.status IN ({})".format(",".join("?" * len(filters.statuses)))]
    params: list = list(filters.statuses)
    if filters.min_score:
        where.append("COALESCE(e.score, 0) >= ?")
        params.append(int(filters.min_score))
    if not filters.include_chains:
        where.append("COALESCE(b.is_chain, 0) = 0")
    if filters.activity:
        where.append("COALESCE(b.activity, 'unclear') IN ({})".format(",".join("?" * len(filters.activity))))
        params.extend(filters.activity)
    if filters.run_id is not None:
        where.append("e.business_id IN (SELECT business_id FROM run_businesses WHERE run_id = ?)")
        params.append(filters.run_id)
    if filters.industries:
        where.append("b.industry IN ({})".format(",".join("?" * len(filters.industries))))
        params.extend(filters.industries)
    if filters.cities:
        where.append("LOWER(b.city) IN ({})".format(",".join("?" * len(filters.cities))))
        params.extend(c.lower() for c in filters.cities)
    if filters.email_types:
        where.append("e.email_type IN ({})".format(",".join("?" * len(filters.email_types))))
        params.extend(filters.email_types)
    if filters.only_new and filters.mode == "email":
        where.append("NOT EXISTS (SELECT 1 FROM export_items x WHERE x.email_id = e.id)")
    sql = f"""
        SELECT e.id AS email_id, e.email, e.status AS email_status, e.email_type,
               e.found_on_url, e.score AS email_score, e.score_detail AS email_score_detail,
               e.contact_name, e.designation, COALESCE(e.decision_maker, 0) AS decision_maker,
               e.mailbox_check, COALESCE(e.is_freemail, 0) AS is_freemail, e.seen_at,
               e.source AS email_source, b.activity, b.activity_reason, b.listing_provider,
               b.id AS business_id, b.name AS business_name, b.phone, b.website, b.final_url,
               b.domain, b.industry, b.city, b.region, b.country, b.socials, b.platform,
               b.is_chain, b.owner_name, b.owner_title, b.people
        FROM emails e JOIN businesses b ON b.id = e.business_id
        WHERE {' AND '.join(where)}
        ORDER BY COALESCE(e.decision_maker, 0) DESC, COALESCE(e.score, 0) DESC, b.name, e.email"""
    # Best email to contact first: each company's lead row gets its best address, and the
    # contact limit keeps the best ones (see contact_priority).
    rows = sorted(store.query(sql, params), key=lambda r: contact_priority(
        {**dict(r), "status": r["email_status"], "score": r["email_score"]}))

    suppressed_emails, suppressed_domains = set(), []
    for s in store.suppression_list():
        (suppressed_emails.add if s["kind"] == "email" else suppressed_domains.append)(s["value"])

    freemail = {d.lower() for d in (freemail_domains or [])}
    exported_companies: set[str] = set()
    if filters.only_new and filters.mode == "company":
        for x in store.query(
            "SELECT e.email, e.business_id, b.domain FROM export_items x"
            " JOIN emails e ON e.id = x.email_id JOIN businesses b ON b.id = e.business_id"
        ):
            exported_companies.add(company_key(x["business_id"], x["domain"], x["email"], freemail))

    leads: list[dict] = []
    by_company: dict[str, dict] = {}
    per_company: dict[str, int] = {}
    limit = filters.max_contacts
    for r in rows:
        email_domain = r["email"].rsplit("@", 1)[-1]
        if r["email"] in suppressed_emails:
            continue
        if domain_matches(email_domain, suppressed_domains) or domain_matches(r["domain"], suppressed_domains):
            continue
        key = company_key(r["business_id"], r["domain"], r["email"], freemail)
        guessed = r["email_source"] == "guessed"
        if not guessed:                   # guesses (confirmed by the mail server) come on top of the limit
            if limit and per_company.get(key, 0) >= limit:
                continue                  # contact limit per company reached (best ones come first)
            per_company[key] = per_company.get(key, 0) + 1
        if filters.mode == "company":
            if key in exported_companies:
                continue
            lead = by_company.get(key)
            if lead is not None:          # another person at a company we already have
                lead["other_emails"].append(r["email"])
                lead["other_contacts"].append(describe_contact(r))
                lead["others"].append({"email": r["email"], "name": r["contact_name"] or "",
                                       "designation": main_designation(r)})
                lead["email_ids"].append(r["email_id"])
                if guessed:
                    lead["guessed"].add(r["email"])
                continue
        socials = loads(r["socials"], {}) or {}
        sources = store.business_sources(r["business_id"])
        lead = {
            "email_id": r["email_id"],
            "email_ids": [r["email_id"]],
            "business_id": r["business_id"],
            "business_name": r["business_name"],
            "email": r["email"],
            "guessed": {r["email"]} if guessed else set(),   # for colour coding, not a column
            "other_emails": [],
            "other_contacts": [],
            "others": [],                                     # the same contacts, one column each on export
            "email_status": r["email_status"],
            "email_type": r["email_type"],
            "first_name": (r["contact_name"] or "").split()[0] if r["contact_name"]
                          else guess_first_name(r["email"], r["email_type"]),
            "contact_name": r["contact_name"] or "",
            "designation": main_designation(r),
            "decision_maker": "yes" if r["decision_maker"] else "",
            "owner_name": r["owner_name"] or "",
            "owner_title": r["owner_title"] or "",
            "key_people": "; ".join(
                f"{p['name']}" + (f" ({p['title']})" if p.get("title") else "") + (f" <{p['email']}>" if p.get("email") else "")
                for p in (loads(r["people"], []) or [])),
            "phone": r["phone"] or "",
            "website": r["final_url"] or r["website"] or "",
            "domain": r["domain"] or "",
            "industry": r["industry"] or "",
            "city": r["city"] or "",
            "region": r["region"] or "",
            "country": r["country"] or "",
            "score": r["email_score"] if r["email_score"] is not None else "",
            "linkedin": socials.get("linkedin", ""),
            "facebook": socials.get("facebook", ""),
            "instagram": socials.get("instagram", ""),
            "x_twitter": socials.get("x", ""),
            "youtube": socials.get("youtube", ""),
            "tiktok": socials.get("tiktok", ""),
            "platform": r["platform"] or "",
            "source": "+".join(sources),
            "found_on_url": r["found_on_url"] or "",
            "email_found_in": found_in(r),
            "email_last_seen": r["seen_at"] or "",
            "business_activity": r["activity"] or "unclear",
            "activity_reason": r["activity_reason"] or "",
        }
        if filters.mode == "company":
            by_company[key] = lead
        leads.append(lead)
    return leads


def render_export(leads: list[dict], *, fmt: str = "csv", template: str = "default",
                  columns: list[str] | None = None) -> bytes:
    """Leads -> file bytes in the chosen format and column template.

    ``columns`` (LeadScout column names) overrides the template's selection.
    """
    if template not in TEMPLATES:
        raise ValueError(f"Unknown template '{template}'. Choose from: {', '.join(TEMPLATES)}")
    if fmt not in ("csv", "xlsx"):
        raise ValueError("Format must be csv or xlsx")
    columns = [(c, c) for c in columns] if columns else TEMPLATES[template]
    columns = _split_other_contacts(columns, leads)
    header = [dst for _, dst in columns]
    rows = [[_cell(_value(lead, src)) for src, _ in columns] for lead in leads]
    if fmt == "csv":
        buf = io.StringIO(newline="")
        writer = csv.writer(buf)
        writer.writerow(header)
        writer.writerows(rows)
        # utf-8-sig so Excel detects the encoding.
        return buf.getvalue().encode("utf-8-sig")
    from openpyxl import Workbook
    from openpyxl.cell.rich_text import CellRichText, TextBlock
    from openpyxl.cell.text import InlineFont
    from openpyxl.styles import PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "Leads"
    ws.append(header)
    for row in rows:
        ws.append(row)
    ws.freeze_panes = "A2"
    # Colour code: green = written on the website / a listing, yellow = guessed and
    # confirmed by the company's mail server.
    fills = {False: PatternFill("solid", fgColor=EMAIL_GREEN), True: PatternFill("solid", fgColor=EMAIL_YELLOW)}
    fonts = {False: InlineFont(color=EMAIL_GREEN_TEXT), True: InlineFont(color=EMAIL_YELLOW_TEXT)}
    for row_no, lead in enumerate(leads, start=2):
        guessed = lead.get("guessed") or set()
        for col_no, (src, _) in enumerate(columns, start=1):
            cell = ws.cell(row=row_no, column=col_no)
            if src == "email" and lead.get("email"):
                cell.fill = fills[lead["email"] in guessed]
            elif _OTHER.fullmatch(src) and src.startswith("email_") and cell.value:
                cell.fill = fills[cell.value in guessed]
            elif src in ("other_contacts", "other_emails") and lead.get(src):
                parts = []
                for i, item in enumerate(lead[src]):
                    if i:
                        parts.append("; ")
                    parts.append(TextBlock(fonts[any(g in item for g in guessed)], item))
                cell.value = CellRichText(*parts)
    legend = wb.create_sheet("Colour key")
    legend.append(["Colour", "Meaning"])
    legend.append(["green", "email written on the business's website or listing"])
    legend.append(["yellow", "email guessed from the company's format and confirmed by its mail server"])
    legend["A2"].fill, legend["A3"].fill = fills[False], fills[True]
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


EMAIL_GREEN, EMAIL_GREEN_TEXT = "C6EFCE", "006100"      # Excel's "good" colours
EMAIL_YELLOW, EMAIL_YELLOW_TEXT = "FFEB9C", "9C5700"    # Excel's "neutral" colours


_OTHER = re.compile(r"(email|name|designation)_(\d+)")


def _split_other_contacts(columns: list[tuple[str, str]], leads: list[dict]) -> list[tuple[str, str]]:
    """The "other_contacts" column becomes email_2, name_2, designation_2, email_3 ... (as many
    as the lead with the most colleagues needs), so each person's details sit in their own cells."""
    most = max((len(lead.get("others") or []) for lead in leads), default=0)
    out = []
    for src, dst in columns:
        if src != "other_contacts":
            out.append((src, dst))
            continue
        for n in range(2, most + 2):
            out += [(f"{field}_{n}", f"{field}_{n}") for field in ("email", "name", "designation")]
    return out


def _value(lead: dict, src: str):
    m = _OTHER.fullmatch(src)
    if m and src not in lead:
        others = lead.get("others") or []
        n = int(m.group(2)) - 2
        return others[n][m.group(1)] if n < len(others) else ""
    return lead.get(src, "")


def _cell(value):
    return "; ".join(value) if isinstance(value, list) else value


def export_filename(fmt: str, template: str = "default") -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"leads-{stamp}" + (f"-{template}" if template != "default" else "") + f".{fmt}"


def record_export(store: Store, filters: ExportFilters, leads: list[dict], *, fmt: str,
                  template: str, file_path: str) -> int:
    record = asdict(filters) | {"format": fmt, "template": template}
    email_ids = [eid for lead in leads for eid in lead.get("email_ids") or [lead["email_id"]]]
    return store.record_export(json.loads(json.dumps(record)), file_path, email_ids)


def write_export(store: Store, filters: ExportFilters, *, export_dir: Path, fmt: str = "csv",
                 template: str = "default", output: Path | None = None,
                 freemail_domains=None) -> tuple[Path, int]:
    leads = select_leads(store, filters, freemail_domains)
    data = render_export(leads, fmt=fmt, template=template)
    output = Path(output) if output else Path(export_dir) / export_filename(fmt, template)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(data)
    record_export(store, filters, leads, fmt=fmt, template=template, file_path=str(output))
    return output, len(leads)
