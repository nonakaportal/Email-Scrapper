"""Extract emails, phones, social links and site signals from HTML pages."""

from __future__ import annotations

import html as htmlmod
import re
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import unquote, urljoin, urlsplit

import phonenumbers
from bs4 import BeautifulSoup

from leadscout.crawler.people import find_people
from leadscout.verify.lists import FILE_EXTENSIONS

EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+-])([A-Za-z0-9][A-Za-z0-9._%+-]{0,63}@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\.[A-Za-z]{2,24})(?![A-Za-z0-9-])"
)

_AT = r"(?:\s*[\[\(\{<]\s*at\s*[\]\)\}>]\s*|\s+at\s+|\s*@\s*)"
_DOT = r"(?:\s*[\[\(\{<]\s*dot\s*[\]\)\}>]\s*|\s+dot\s+|\.)"
OBFUSCATED_RE = re.compile(
    rf"(?<![\w.+-])([A-Za-z0-9][\w.+-]{{0,63}}?)({_AT})([A-Za-z0-9-]+(?:{_DOT}[A-Za-z0-9-]+)+)",
    re.I,
)
_DOT_SPLIT = re.compile(_DOT, re.I)
CFEMAIL_RE = re.compile(r"""data-cfemail=["']([0-9a-fA-F]+)["']""")
CFLINK_RE = re.compile(r"/cdn-cgi/l/email-protection#([0-9a-fA-F]+)")
COPYRIGHT_RE = re.compile(
    r"(?:©|&copy;|\(c\)|copyright)\s*(?:[^0-9<]{0,40}?)((?:19|20)\d{2})(?:\s*[-–—]\s*((?:19|20)\d{2}))?",
    re.I,
)

SOCIAL_HOSTS = {
    "linkedin": ("linkedin.com",),
    "facebook": ("facebook.com", "fb.com"),
    "instagram": ("instagram.com",),
    "x": ("twitter.com", "x.com"),
    "youtube": ("youtube.com",),
    "tiktok": ("tiktok.com",),
}
SOCIAL_SKIP = re.compile(r"sharer|/share|intent/|/dialog/|/plugins/|/tr\?|login|/embed/|hashtag|/watch\?", re.I)

PLATFORM_MARKERS = [
    ("shopify", ("cdn.shopify.com", "shopify.theme", "myshopify.com")),
    ("wix", ("static.wixstatic.com", "wix.com website builder", "_wixcssimports", "wixsite.com")),
    ("squarespace", ("static1.squarespace.com", "squarespace.com", "squarespace-cdn")),
    ("webflow", ("webflow.com", "data-wf-site", "data-wf-page")),
    ("godaddy", ("img1.wsimg.com", "godaddy website builder")),
    ("weebly", ("weebly.com", "editmysite.com")),
    ("duda", ("dudaone", "multiscreensite.com")),
    ("wordpress", ("wp-content/", "wp-includes/", "wordpress")),
    ("joomla", ("joomla",)),
    ("drupal", ("drupal.settings", "/sites/default/files", "drupal")),
    ("hubspot", ("hs-scripts.com", "hubspot")),
]

CONTACT_WORDS = ("contact", "kontakt", "impressum", "imprint", "contacto", "contatti",
                 "get-in-touch", "reach-us", "find-us", "location")
ABOUT_WORDS = ("about", "team", "staff", "people", "who-we-are", "our-story", "meet",
               "doctors", "attorneys", "providers", "ueber-uns", "uber-uns", "equipe",
               "leadership", "management", "our-company", "history", "founder", "owner", "executives")
# Pages most likely to name the owner and leaders, best first.
PEOPLE_PAGE_WORDS = ("leadership", "management", "executives", "our-team", "team", "staff", "meet",
                     "founder", "owner", "people", "who-we-are", "our-story", "about", "history")
TEAM_PAGE_RE = re.compile(r"team|staff|people|meet|doctors|attorneys|providers|our-story|about", re.I)
CAREERS_RE = re.compile(r"career|/jobs?\b|join-our-team|join-us|work-with-us|we-?re-hiring|vacanc|stellen", re.I)
# A plain "Locations" menu item is common on single-office sites, so only an
# explicit locator or links to several location sub-pages count as "many locations".
LOCATOR_RE = re.compile(r"store-?locator|location-?finder|find-an?-(?:location|store|clinic|office|branch)|"
                        r"all-locations|standortsuche", re.I)
LOCATION_PAGE_RE = re.compile(r"/(?:locations?|offices|clinics|branches|standorte)/([^/?#]+)", re.I)
MULTI_LOCATION_MIN_PAGES = 3
INVESTOR_RE = re.compile(r"investor[-_ ]?relations", re.I)   # not bare "investors": realtors use that
MEMBER_CLASS_RE = re.compile(r"team[-_]?member|staff[-_]?member|\bmember\b|\bperson\b|\bprofile\b|"
                             r"\bdoctor\b|\battorney\b|\bprovider\b|\bbio\b|team[-_]?card|staff[-_]?card", re.I)
SKIP_EXT_RE = re.compile(r"\.(?:pdf|jpe?g|png|gif|webp|svg|zip|docx?|xlsx?|pptx?|mp[34]|mov|avi|ico|css|js|xml|rss)$", re.I)


@dataclass
class PageData:
    url: str
    emails: dict[str, str] = field(default_factory=dict)   # email -> how found
    phones: list[str] = field(default_factory=list)        # E.164
    socials: dict[str, str] = field(default_factory=dict)
    links: list[tuple[str, str]] = field(default_factory=list)  # (abs url, text)
    platform: str | None = None
    copyright_year: int | None = None
    latest_date_year: int | None = None   # newest year in a real date on the page
    closed_notice: str | None = None      # "closed permanently", parked domain, suspended hosting
    team_count: int | None = None
    has_careers: bool = False
    multi_location: bool = False
    intl_languages: int = 0          # distinct hreflang versions of this page
    investor_relations: bool = False
    people: list = field(default_factory=list)   # Person(name, title, email) named on the page


def decode_cfemail(hexstr: str) -> str | None:
    try:
        data = bytes.fromhex(hexstr)
    except ValueError:
        return None
    if len(data) < 2:
        return None
    key = data[0]
    return "".join(chr(b ^ key) for b in data[1:])


def clean_email(raw: str) -> str | None:
    """Normalize a candidate address; None if it cannot be an email."""
    email = unquote(raw).strip().strip(".,;:'\"()[]<>").lower()
    if email.startswith("mailto:"):
        email = email[7:]
    email = email.split("?")[0]
    # Leftovers of JSON-escaped text, e.g. ">info@x.com".
    email = re.sub(r"^(?:u00[0-9a-f]{2})+", "", email)
    m = EMAIL_RE.fullmatch(email)
    if not m:
        return None
    local, domain = email.rsplit("@", 1)
    if local.startswith(".") or local.endswith(".") or ".." in local or ".." in domain:
        return None
    if domain.rsplit(".", 1)[-1] in FILE_EXTENSIONS:
        return None   # "logo@2x.png"
    return email


def _deobfuscate(text: str) -> dict[str, str]:
    found = {}
    for m in OBFUSCATED_RE.finditer(text):
        local, at, domain_part = m.group(1), m.group(2), m.group(3)
        parts = _DOT_SPLIT.split(domain_part)
        seps = _DOT_SPLIT.findall(domain_part)
        plain_at = at.strip().lower() == "at" and not re.search(r"[\[\(\{<]", at)
        fancy_dot = any(s != "." for s in seps)
        if at == "@" and not fancy_dot:
            continue  # a normal address; the plain regex handles it
        if plain_at and not fancy_dot:
            continue  # "meet us at example.com" is too ambiguous
        email = clean_email(f"{local}@{'.'.join(p.strip() for p in parts)}")
        if email:
            found.setdefault(email, "obfuscated")
    return found


def find_emails(raw_html: str, text: str) -> dict[str, str]:
    """All email candidates on a page -> how each was found."""
    found: dict[str, str] = {}
    for hexstr in CFEMAIL_RE.findall(raw_html) + CFLINK_RE.findall(raw_html):
        email = clean_email(decode_cfemail(hexstr) or "")
        if email:
            found.setdefault(email, "cloudflare")
    for m in re.finditer(r"mailto:([^\"'>\s]+)", raw_html, re.I):
        email = clean_email(htmlmod.unescape(m.group(1)))
        if email:
            found.setdefault(email, "mailto")
    source = htmlmod.unescape(raw_html)
    source = source.replace("\\u0040", "@").replace("\\u003c", " ").replace("\\u003e", " ")
    source = source.replace("%40", "@")
    for m in EMAIL_RE.finditer(source):
        email = clean_email(m.group(1))
        if email:
            found.setdefault(email, "text")
    for email, how in _deobfuscate(text).items():
        found.setdefault(email, how)
    return found


def find_phones(soup: BeautifulSoup, text: str, region: str | None, limit: int = 5) -> list[str]:
    phones: list[str] = []

    def add(num) -> None:
        e164 = phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)
        if e164 not in phones:
            phones.append(e164)

    for a in soup.select('a[href^="tel:"], a[href^="TEL:"], a[href^="Tel:"]'):
        raw = unquote(a.get("href", "")[4:])
        try:
            num = phonenumbers.parse(raw, region)
        except phonenumbers.NumberParseException:
            continue
        if phonenumbers.is_valid_number(num):
            add(num)
    if len(phones) < limit:
        try:
            matcher = phonenumbers.PhoneNumberMatcher(text[:200_000], region,
                                                      leniency=phonenumbers.Leniency.VALID)
            for match in matcher:
                add(match.number)
                if len(phones) >= limit:
                    break
        except Exception:  # noqa: BLE001 - matcher can choke on odd input
            pass
    return phones[:limit]


def find_socials(links: list[tuple[str, str]]) -> dict[str, str]:
    socials: dict[str, str] = {}
    for url, _ in links:
        host = (urlsplit(url).hostname or "").lower()
        if SOCIAL_SKIP.search(url):
            continue
        path = urlsplit(url).path.strip("/")
        if not path:
            continue
        for network, hosts in SOCIAL_HOSTS.items():
            if network in socials:
                continue
            if any(host == h or host.endswith("." + h) for h in hosts):
                socials[network] = url.split("?")[0]
    return socials


def detect_platform(raw_lower: str, soup: BeautifulSoup) -> str | None:
    gen = soup.find("meta", attrs={"name": re.compile("^generator$", re.I)})
    generator = (gen.get("content") or "").lower() if gen else ""
    for platform, markers in PLATFORM_MARKERS:
        if platform in generator:
            return platform
    for platform, markers in PLATFORM_MARKERS:
        if any(m in raw_lower for m in markers):
            return platform
    return None


_MONTHS = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
           r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
# Real dates only ("March 12, 2026", "12 Mar 2026", "03/12/2026", "2026-03-12", "Updated 2026"),
# so street numbers and phone parts ("2024 Main St") don't count.
DATE_YEAR_RE = re.compile(
    rf"\b{_MONTHS}\.?\s+(?:\d{{1,2}}(?:st|nd|rd|th)?,?\s+)?((?:19|20)\d{{2}})\b"
    rf"|\b\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTHS}\.?,?\s+((?:19|20)\d{{2}})\b"
    r"|\b\d{1,2}[/.]\d{1,2}[/.]((?:19|20)\d{2})\b"
    r"|\b((?:19|20)\d{2})-\d{2}-\d{2}\b"
    r"|\b(?:updated|posted|published|last modified|season|hours for|holiday hours)\s*:?\s*((?:19|20)\d{2})\b",
    re.I,
)
# The business says it has closed, or the site is a parked / for-sale / suspended page.
CLOSED_RES = [
    (re.compile(r"\b(?:we\s+(?:are|have)\s+(?:now\s+)?|business\s+(?:is|has)\s+)?(?:permanently|officially)\s+closed\b"
                r"|\bclosed\s+(?:for\s+good|permanently)\b|\bout\s+of\s+business\b|\bceased\s+(?:operations|trading)\b"
                r"|\bno\s+longer\s+in\s+business\b|\bclosed\s+(?:our|its)\s+doors\b", re.I),
     "the website says the business has closed"),
    (re.compile(r"\b(?:this|the)\s+domain(?:\s+name)?\s+(?:is|may\s+be)\s+for\s+sale\b|\bbuy\s+this\s+domain\b"
                r"|\bdomain\s+(?:is\s+)?parked\b|\bthis\s+web\s*page\s+is\s+parked\b|\bparked\s+free\b"
                r"|\bthis\s+website\s+is\s+for\s+sale\b", re.I),
     "the website is a parked / for-sale domain"),
    (re.compile(r"\b(?:this\s+)?account\s+has\s+been\s+suspended\b|\bhosting\s+(?:account\s+)?(?:has\s+)?expired\b"
                r"|\bwebsite\s+(?:has\s+)?expired\b|\bsite\s+(?:is\s+)?(?:suspended|expired)\b", re.I),
     "the website's hosting is suspended or expired"),
]


def find_latest_date_year(text: str, this_year: int | None = None) -> int | None:
    """The newest year in a real date on the page (not in the future)."""
    this_year = this_year or date.today().year
    years = [int(g) for m in DATE_YEAR_RE.finditer(text) for g in m.groups() if g]
    years = [y for y in years if 1995 <= y <= this_year]
    return max(years) if years else None


def find_closed_notice(text: str) -> str | None:
    for regex, reason in CLOSED_RES:
        m = regex.search(text)
        if m:
            return f"{reason} (\"{m.group(0).strip()}\")"
    return None


def find_copyright_year(text: str, this_year: int | None = None) -> int | None:
    this_year = this_year or date.today().year
    years = []
    for m in COPYRIGHT_RE.finditer(text):
        for g in m.groups():
            if g and 1990 <= int(g) <= this_year + 1:
                years.append(int(g))
    return max(years) if years else None


def count_team_members(soup: BeautifulSoup) -> int | None:
    """Rough count of person cards on a team/staff page."""
    matches = [el for el in soup.find_all(["div", "li", "article", "section", "figure"])
               if MEMBER_CLASS_RE.search(" ".join(el.get("class") or []))]
    if not matches:
        return None
    match_ids = {id(el) for el in matches}
    leaves = []
    for el in matches:
        # Keep the innermost card only (skip wrappers that contain other cards).
        if any(id(child) in match_ids for child in el.find_all(True)):
            continue
        if el.find(["img", "h2", "h3", "h4", "h5", "strong"]):
            leaves.append(el)
    return len(leaves) or None


def extract_page(raw_html: str, url: str, region: str | None = None) -> PageData:
    soup = BeautifulSoup(raw_html, "lxml")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.extract()
    text = soup.get_text(" ", strip=True)
    data = PageData(url=url)
    data.emails = find_emails(raw_html, text)
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
            continue
        try:
            abs_url = urljoin(url, href)
        except ValueError:
            continue
        if abs_url.startswith(("http://", "https://")):
            links.append((abs_url.split("#")[0], a.get_text(" ", strip=True)[:80]))
    data.links = links
    data.phones = find_phones(soup, text, region)
    data.socials = find_socials(links)
    data.platform = detect_platform(raw_html.lower(), soup)
    data.copyright_year = find_copyright_year(text)
    data.latest_date_year = find_latest_date_year(text)
    data.closed_notice = find_closed_notice(text[:20000])
    link_blob = " ".join(f"{u} {t}" for u, t in links)
    data.has_careers = bool(CAREERS_RE.search(link_blob))
    data.multi_location = detect_multi_location(links)
    data.investor_relations = any(INVESTOR_RE.search(f"{urlsplit(u).path} {t}") for u, t in links)
    langs = {(tag.get("hreflang") or "").strip().lower() for tag in soup.find_all("link", hreflang=True)}
    data.intl_languages = len(langs - {"", "x-default"})
    if TEAM_PAGE_RE.search(urlsplit(url).path):
        data.team_count = count_team_members(soup)
    data.people = find_people(raw_html, soup, url)     # last: it annotates mailto links in soup
    return data


def detect_multi_location(links: list[tuple[str, str]]) -> bool:
    slugs = set()
    for url, text in links:
        if LOCATOR_RE.search(url) or LOCATOR_RE.search(text.lower().replace(" ", "-")):
            return True
        m = LOCATION_PAGE_RE.search(urlsplit(url).path)
        if m:
            slugs.add(m.group(1).lower())
    return len(slugs) >= MULTI_LOCATION_MIN_PAGES


def people_page_rank(url: str, text: str) -> int | None:
    """How likely a page names the company's people (0 best), or None."""
    blob = f"{urlsplit(url).path.lower()} {text.lower()}"
    for rank, word in enumerate(PEOPLE_PAGE_WORDS):
        if word in blob:
            return rank
    return None


def link_priority(url: str, text: str) -> int | None:
    """0 = contact-like page, 1 = about/team page, None = not interesting."""
    path = urlsplit(url).path.lower()
    if SKIP_EXT_RE.search(path):
        return None
    blob = f"{path} {text.lower()}"
    if any(w in blob for w in CONTACT_WORDS):
        return 0
    if any(w in blob for w in ABOUT_WORDS):
        return 1
    return None
