"""Crawl one business website: homepage, then contact/about/team pages."""

from __future__ import annotations

import html as htmlmod
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

from leadscout.crawler.extract import extract_page, link_priority, people_page_rank
from leadscout.crawler.fetcher import Fetcher, FetchResult
from leadscout.normalize import registered_domain

FALLBACK_PATHS = ("/contact", "/contact-us", "/about")


@dataclass
class SiteResult:
    start_url: str
    final_url: str | None = None
    pages: list[FetchResult] = field(default_factory=list)
    emails: dict[str, str] = field(default_factory=dict)   # email -> page URL
    phones: list[str] = field(default_factory=list)
    socials: dict[str, str] = field(default_factory=dict)
    platform: str | None = None
    copyright_year: int | None = None
    latest_date_year: int | None = None            # newest dated content on any page
    closed_notice: str | None = None               # closure / parked / suspended notice on the homepage
    team_size_hint: int | None = None
    has_careers: bool = False
    multi_location: bool = False
    intl_languages: int = 0
    investor_relations: bool = False
    people: list = field(default_factory=list)     # Person(name, title, email) across pages
    error: str | None = None


def canonical(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def _alternate_scheme(url: str) -> str | None:
    parts = urlsplit(url)
    if parts.scheme == "https":
        return urlunsplit(("http", *parts[1:]))
    if parts.scheme == "http":
        return urlunsplit(("https", *parts[1:]))
    return None


async def crawl_site(fetcher: Fetcher, url: str, *, max_pages: int = 5,
                     region: str | None = None) -> SiteResult:
    result = SiteResult(start_url=url)
    home = await fetcher.fetch(url)
    result.pages.append(home)
    if not home.ok and home.status is None and not (home.error or "").startswith(("blocked", "robots")):
        alt = _alternate_scheme(url)
        if alt:
            retry = await fetcher.fetch(alt)
            result.pages.append(retry)
            if retry.ok:
                home = retry
    if not home.ok:
        result.error = home.error or "homepage could not be fetched"
        return result

    result.final_url = home.final_url or home.url
    site_domain = registered_domain(result.final_url)
    base = urlsplit(result.final_url)
    visited = {canonical(url), canonical(home.url), canonical(result.final_url)}

    own_agent = getattr(fetcher, "user_agent", "") or ""

    def absorb(page_url: str, html: str) -> list[tuple[str, str]]:
        # Some sites echo the visitor's User-Agent into the page (hidden form fields, analytics);
        # it holds our own contact email, which is not the business's.
        if own_agent:
            for form in {own_agent, htmlmod.escape(own_agent), htmlmod.escape(own_agent, quote=False)}:
                html = html.replace(form, " ")
        data = extract_page(html, page_url, region)
        for email in data.emails:
            result.emails.setdefault(email, page_url)
        for phone in data.phones:
            if phone not in result.phones:
                result.phones.append(phone)
        for net, link in data.socials.items():
            result.socials.setdefault(net, link)
        result.platform = result.platform or data.platform
        if data.copyright_year and (result.copyright_year or 0) < data.copyright_year:
            result.copyright_year = data.copyright_year
        if data.latest_date_year and (result.latest_date_year or 0) < data.latest_date_year:
            result.latest_date_year = data.latest_date_year
        if data.closed_notice and canonical(page_url) == canonical(result.final_url):
            result.closed_notice = data.closed_notice
        if data.team_count and (result.team_size_hint or 0) < data.team_count:
            result.team_size_hint = data.team_count
        result.has_careers = result.has_careers or data.has_careers
        result.multi_location = result.multi_location or data.multi_location
        result.intl_languages = max(result.intl_languages, data.intl_languages)
        result.investor_relations = result.investor_relations or data.investor_relations
        known = {p.name.lower(): p for p in result.people}
        for person in data.people:
            same = known.get(person.name.lower())
            if same is None:
                result.people.append(person)
                known[person.name.lower()] = person
            else:
                # keep the most senior title any page gives them ("DDS" on home, "Owner" on about)
                if not same.title or (person.title and person.level < same.level):
                    same.title = person.title
                same.email = same.email or person.email
        return data.links

    links = absorb(result.final_url, home.html or "")

    queue: list[tuple[int, int, str]] = []
    seen_candidates: set[str] = set()
    for order, (link, text) in enumerate(links):
        if registered_domain(link) != site_domain:
            continue
        prio = link_priority(link, text)
        key = canonical(link)
        if prio is None or key in visited or key in seen_candidates:
            continue
        seen_candidates.add(key)
        queue.append((prio, order, link))
    queue.sort()
    # Visit the best people page (leadership / team / about) second, right after the best
    # contact page, so owners and directors are found even on sites with many contact pages.
    ranked = [(people_page_rank(link, ""), i) for i, (_, _, link) in enumerate(queue)]
    ranked = [(r, i) for r, i in ranked if r is not None]
    if ranked:
        best = queue.pop(min(ranked)[1])
        queue.insert(1 if queue and queue[0][0] == 0 else 0, best)

    budget = max_pages - 1
    for _, _, link in queue:
        if budget <= 0:
            break
        page = await fetcher.fetch(link)
        result.pages.append(page)
        visited.add(canonical(link))
        budget -= 1
        if page.ok:
            if page.final_url:
                visited.add(canonical(page.final_url))
            absorb(page.final_url or link, page.html or "")

    if not result.emails:
        for path in FALLBACK_PATHS:
            if budget <= 0:
                break
            link = urlunsplit((base.scheme, base.netloc, path, "", ""))
            if canonical(link) in visited:
                continue
            page = await fetcher.fetch(link)
            result.pages.append(page)
            visited.add(canonical(link))
            budget -= 1
            if page.ok:
                absorb(page.final_url or link, page.html or "")
                if result.emails:
                    break
    return result
