"""Normalization of domains, URLs, phones and names, plus dedupe helpers."""

from __future__ import annotations

import math
import re
import unicodedata
from urllib.parse import urlsplit, urlunsplit

import phonenumbers
import tldextract

# Offline extractor: uses the public-suffix snapshot bundled with tldextract,
# never downloads anything.
_TLD = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)
# Same, but treating hosting platforms from the public-suffix list's private
# section (wixsite.com, myshopify.com, github.io ...) as suffixes.
_TLD_PRIVATE = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None,
                                     include_psl_private_domains=True)

# Site builders where many unrelated businesses share one domain, missing from
# the public-suffix list. Each subdomain is its own company.
SHARED_HOSTING = (
    "business.site", "squarespace.com", "voolt.com", "godaddysites.com", "weebly.com",
    "wordpress.com", "blogspot.com", "webflow.io", "carrd.co", "jimdosite.com", "site123.me",
    "mystrikingly.com", "strikingly.com", "yolasite.com", "ueniweb.com", "company.site",
    "wixsite.com", "editorx.io", "hubspotpagebuilder.com", "webnode.com", "simplesite.com",
    "tilda.ws", "sites.google.com", "square.site", "myshopify.com",
)

LEGAL_SUFFIXES = {
    "llc", "l l c", "inc", "incorporated", "ltd", "limited", "co", "corp",
    "corporation", "company", "pllc", "pc", "p c", "plc", "llp", "lp", "gmbh",
    "ag", "sa", "sarl", "srl", "bv", "nv", "pty", "pvt", "ab", "oy", "as",
    "kg", "ug", "dds", "dmd", "md", "cpa", "esq",
}


def normalize_url(raw: str | None) -> str | None:
    """Turn an OSM/CSV website value into a fetchable absolute URL."""
    if not raw:
        return None
    value = str(raw).strip()
    # OSM sometimes holds several values separated by ';'.
    value = re.split(r"[;\s]+", value)[0].strip()
    if not value or value.lower().startswith(("mailto:", "tel:", "javascript:")):
        return None
    if not re.match(r"^[a-z][a-z0-9+.-]*://", value, re.I):
        value = "https://" + value.lstrip("/")
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.lower().rstrip(".")
    if "." not in host:
        return None
    netloc = host if parts.port is None else f"{host}:{parts.port}"
    path = parts.path or "/"
    return urlunsplit((parts.scheme.lower(), netloc, path, parts.query, ""))


def host_of(url: str | None) -> str | None:
    if not url:
        return None
    try:
        host = urlsplit(url if "://" in url else "http://" + url).hostname
    except ValueError:
        return None
    return host.lower().rstrip(".") if host else None


def normalize_domain(url_or_domain: str | None) -> str | None:
    """``https://www.SmileStudio.com/contact`` -> ``smilestudio.com``."""
    host = host_of(url_or_domain)
    if not host:
        return None
    if host.startswith("www."):
        host = host[4:]
    return host if "." in host else None


def registered_domain(url_or_domain: str | None) -> str | None:
    """Domain under the public suffix: ``shop.smile.co.uk`` -> ``smile.co.uk``."""
    host = host_of(url_or_domain)
    if not host:
        return None
    ext = _TLD(host)
    if ext.domain and ext.suffix:
        return f"{ext.domain}.{ext.suffix}"
    return host


def company_domain(url_or_domain: str | None, shared_hosting=SHARED_HOSTING) -> str | None:
    """The domain that identifies a company: the registered domain
    (``local.jacksonhewitt.com`` -> ``jacksonhewitt.com``), except on shared site
    builders where each subdomain is a different business (``joes.wixsite.com``)."""
    host = normalize_domain(url_or_domain)
    if not host:
        return None
    for shared in shared_hosting:
        shared = shared.lower().lstrip(".")
        if host.endswith("." + shared):
            return host
    ext = _TLD_PRIVATE(host)
    if ext.domain and ext.suffix:
        return f"{ext.domain}.{ext.suffix}"
    return host


def domain_label(url_or_domain: str | None) -> str | None:
    """The brandable part of a domain: ``smile-studio.co.uk`` -> ``smile-studio``."""
    host = host_of(url_or_domain)
    if not host:
        return None
    return _TLD(host).domain or None


def domain_matches(domain: str | None, candidates) -> bool:
    """True if ``domain`` equals or is a subdomain of any candidate."""
    if not domain:
        return False
    domain = domain.lower()
    for cand in candidates:
        cand = cand.lower().lstrip(".")
        if domain == cand or domain.endswith("." + cand):
            return True
    return False


def normalize_phone(raw: str | None, region: str | None = None) -> str | None:
    """Parse a phone number into E.164, or None if it is not valid."""
    if not raw:
        return None
    value = str(raw).split(";")[0].strip()
    if value.lower().startswith("tel:"):
        value = value[4:]
    try:
        num = phonenumbers.parse(value, (region or "").upper() or None)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(num):
        return None
    return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)


def _strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )


def normalize_name(name: str | None) -> str:
    """Lowercase, drop punctuation, accents and legal suffixes (LLC, Inc ...)."""
    if not name:
        return ""
    text = _strip_accents(name).lower().replace("&", " and ")
    text = re.sub(r"[^\w\s]", " ", text)
    text = text.replace("_", " ")
    words = text.split()
    while words and words[-1] in LEGAL_SUFFIXES:
        words.pop()
    # Two-word suffixes like "l l c" after punctuation removal.
    joined = " ".join(words)
    for suffix in ("l l c", "p c"):
        if joined.endswith(" " + suffix):
            joined = joined[: -len(suffix) - 1]
    return re.sub(r"\s+", " ", joined).strip()


def name_tokens(name: str | None) -> set[str]:
    return {t for t in normalize_name(name).split() if len(t) >= 3}


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# Where expired domains end up: marketplaces and parking services.
DOMAIN_MARKETS = ("hugedomains.com", "expireddomains.com", "afternic.com", "sedo.com", "dan.com", "bodis.com",
                  "parkingcrew.net", "above.com", "buydomains.com", "domainmarket.com", "undeveloped.com",
                  "sav.com", "perfectdomain.com", "brandbucket.com", "squadhelp.com", "atom.com", "epik.com",
                  "uniregistry.com", "domainnamesales.com", "godaddy.com", "namecheap.com", "domainagents.com")


def redirect_kind(website: str | None, final_url: str | None, business_name: str | None = None) -> str | None:
    """Where the business's website now lands.

    None      - no redirect to another domain
    "related" - another domain with the same name (statenlaw.com -> statenlaw.net)
    "parked"  - a domain marketplace / parking page: the domain expired
    "other"   - an unrelated company's site (sold, merged, closed or taken over)
    """
    old, new = company_domain(website), company_domain(final_url)
    if not old or not new or old == new:
        return None
    if domain_matches(new, DOMAIN_MARKETS):
        return "parked"
    # the brand part: joes.wixsite.com -> joes, smile-studio.co.uk -> smilestudio
    a, b = ((d.split(".")[0] if _TLD(d).subdomain else domain_label(d)) or "" for d in (old, new))
    a, b = a.replace("-", ""), b.replace("-", "")
    if len(a) >= 4 and len(b) >= 4 and (a in b or b in a or a[:6] == b[:6]):
        return "related"
    if any(len(t) >= 4 and t not in _COMMON_WORDS and t in b for t in name_tokens(business_name)):
        return "related"
    # initials: Kelly Francis Illumination -> kfillum.com, A&W Irrigation -> aw-landscapeservice.com
    words = re.findall(r"[a-z0-9]+", re.sub(r"&", " ", (business_name or "").lower()))
    initials = "".join(w[0] for w in words[:2])
    if len(initials) == 2 and b.startswith(initials) and len(b) <= 25:
        return "related"
    return "other"


# Too common to show two domains belong to the same company.
_COMMON_WORDS = set("""
hotel hotels inn street avenue group services service company home homes realty construction landscaping
landscape electric electrical plumbing roofing cleaning design designs studio center centre care auto repair
dental family clinic medical health garden gardens nursery farm farms lawn tree trees pest supply supplies
rental rentals storage moving movers law legal firm office associates partners solutions systems global
city county state north south east west new york texas best quality pro professional premier custom
""".split())
