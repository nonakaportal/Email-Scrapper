"""Lead score (0-100 with default weights). Weights live in settings.yaml."""

from __future__ import annotations

import json
from datetime import date
from typing import Mapping


def _loads(value) -> dict:
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value or "{}")
    except ValueError:
        return {}


ACTIVITY_LABELS = ("active", "likely active", "unclear", "likely closed")
# The website's domain no longer exists (expired), as opposed to a temporary DNS hiccup.
_DOMAIN_GONE = ("nxdomain",)      # confirmed by a DNS lookup (see RunManager._confirm_dead_domain)
# Errors that still prove a website is there (it just didn't let us in).
# ("robots.txt unavailable (HTTP 521)" is the opposite: the server is down.)
_SITE_THERE = ("http 401", "http 403", "http 429", "robots.txt denies", "blocked by robots")


def business_activity(b: Mapping, emails: list[Mapping] = (), this_year: int | None = None,
                      today: date | None = None, active_years: int = 2,
                      listing_months: int = 18) -> tuple[str, str]:
    """Is the business still operating? -> (label, reason). Labels: active, likely active,
    unclear, likely closed. Signals, strongest first: a closure / parked / suspended notice on
    the homepage, a website domain that no longer exists, a live website, recent dates on the
    site, a recently updated map listing, a mailbox confirmed by the mail server."""
    today = today or date.today()
    this_year = this_year or today.year
    keys = b.keys() if hasattr(b, "keys") else b
    get = lambda k: b[k] if k in keys else None  # noqa: E731
    if get("closed_notice"):
        return "likely closed", get("closed_notice")
    error = (get("error") or "").lower()
    has_site = get("website_status") == "ok"
    if has_site and any(s in error for s in _DOMAIN_GONE):
        return "likely closed", "the website's domain no longer exists"
    if get("redirected_to"):
        return "unclear", (f"the website now goes to another company's site ({get('redirected_to')}): "
                           "it may have been sold, merged or closed")
    site_up = has_site and (not error or any(s in error for s in _SITE_THERE))
    signs = []
    year = max(get("copyright_year") or 0, get("latest_date_year") or 0)
    if year and year >= this_year - active_years:
        signs.append(f"website has {year} content")
    listed = get("listing_updated")
    if listed:
        try:
            listed_on = date.fromisoformat(str(listed)[:10])
            if (today - listed_on).days <= listing_months * 30.5:
                signs.append(f"map listing updated {listed_on:%b %Y}"
                             + (f" ({get('listing_provider')})" if get("listing_provider") else ""))
        except ValueError:
            pass
    if any((e["mailbox_check"] if "mailbox_check" in e.keys() else None) == "valid" for e in emails):
        signs.append("mailbox confirmed by the mail server")
    if site_up and signs:
        return "active", "website online; " + "; ".join(signs)
    if site_up:
        return "likely active", "website online (no dated content found)"
    if signs:
        down = f"website not reachable ({get('error')[:60]}); " if has_site and error else ""
        return "likely active", down + "; ".join(signs)
    if has_site and error:
        return "unclear", f"website not reachable ({get('error')[:60]})"
    return "unclear", "no website and no recent listing date"


def score_lead(business: Mapping, email: Mapping | None, weights: Mapping,
               this_year: int | None = None) -> tuple[int, dict]:
    """Score one (business, email) pair. Returns (score, breakdown)."""
    this_year = this_year or date.today().year
    w = lambda key: int(weights.get(key, 0))  # noqa: E731
    detail: dict[str, int] = {}

    if email is not None:
        status = email["status"]
        detail["email_status"] = w("email_valid") if status == "valid" else (
            w("email_risky") if status == "risky" else 0)
        etype = email["email_type"]
        detail["email_type"] = {"personal": w("email_personal"), "generic": w("email_generic"),
                                "role": w("email_role")}.get(etype, 0)
    else:
        detail["email_status"] = 0
        detail["email_type"] = 0

    detail["independent"] = 0 if business["is_chain"] else w("independent")
    single = int(business["location_count"] or 1) <= 1 and not business["multi_location_site"]
    detail["single_location"] = w("single_location") if single else 0
    detail["has_phone"] = w("has_phone") if business["phone"] else 0
    year = max(business["copyright_year"] or 0,
               (business["latest_date_year"] if "latest_date_year" in business.keys() else None) or 0) or None
    active_years = int(weights.get("website_active_years", 2))
    detail["website_active"] = w("website_active") if year and year >= this_year - active_years else 0
    detail["has_social"] = w("has_social") if _loads(business["socials"]) else 0
    team = business["team_size_hint"]
    max_people = int(weights.get("small_team_max_people", 50))
    small = (team is not None and team <= max_people) or (team is None and not business["has_careers"])
    detail["small_team"] = w("small_team") if small else 0

    total = max(0, min(100, sum(detail.values())))
    return total, detail
