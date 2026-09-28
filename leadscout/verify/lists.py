"""Email classification: junk filter, type labels, freemail/disposable lists,
and whether an address plausibly belongs to the business."""

from __future__ import annotations

import re

from leadscout.normalize import domain_label, domain_matches, name_tokens, registered_domain

FILE_EXTENSIONS = {
    "png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "ico", "tif", "tiff", "avif",
    "css", "js", "mjs", "json", "xml", "pdf", "zip", "mp4", "mp3", "woff", "woff2",
    "ttf", "eot", "php", "html", "htm", "asp", "aspx", "jsp", "txt", "map",
}
_HASHY_LOCAL = re.compile(r"^[0-9a-f]{16,}$")

# Inboxes for running social accounts: rarely read for business, so contacted last.
SOCIAL_INBOXES = {"facebook", "fb", "instagram", "insta", "ig", "twitter", "tiktok", "youtube", "linkedin",
                  "pinterest", "social", "socials", "socialmedia", "social.media"}
_STATUS_RANK = {"valid": 0, None: 1, "unknown": 1, "risky": 2, "invalid": 3}
_TYPE_RANK = {"personal": 1, "generic": 2, "role": 3}


def contact_priority(c: dict) -> tuple:
    """Sort key for "which email is best to contact at this company" (lower is better):

    0. written somewhere (website, listing) before guessed-and-confirmed addresses
    1. the most senior person: owner / CEO, then director / VP, then manager
    2. deliverable: valid, then not yet known, then risky (catch-all), then invalid
    3. who reads it: a named person, other personal address, the main inbox (info@,
       office@), department inboxes (sales@, billing@), social-media inboxes last
    4. seen on the business's own website (map-only listings can be old)
    5. seen most recently (today's crawl beats a 2021 directory listing)
    6. confirmed by the mail server
    7. on the company's own domain rather than Gmail & co.
    8. higher lead score
    """
    seen = re.sub(r"\D", "", str(c.get("seen_at") or ""))[:8]
    # How senior the person behind the address is: owner / CEO, then director / VP, then manager.
    from leadscout.crawler.people import seniority      # here: the crawler imports this module
    level = seniority(c.get("designation")) if c.get("contact_name") else None
    rank = {1: 0, 2: 1, 3: 2}.get(level, 3)
    if c.get("decision_maker") and rank > 1:
        rank = 0 if level is None else 1           # owner found another way (business named after them)
    local = (c.get("email") or "").split("@", 1)[0].lower().split("+")[0]
    kind = _TYPE_RANK.get(c.get("email_type"), 3)
    if kind == 1 and c.get("contact_name"):
        kind = 0
    if local in SOCIAL_INBOXES or c.get("designation") == "Social media":
        kind = 4
    return (
        1 if (c.get("source") or c.get("email_source")) == "guessed" else 0,   # written ones first
        rank,
        _STATUS_RANK.get(c.get("status"), 1),
        kind,
        0 if c.get("found_on_url") else 1,
        -int(seen or 0),
        0 if c.get("mailbox_check") == "valid" else 1,
        1 if c.get("is_freemail") else 0,
        -(c.get("score") or 0),
    )


class EmailRules:
    def __init__(self, raw: dict):
        low = lambda key: {str(v).lower() for v in (raw.get(key) or [])}  # noqa: E731
        self.generic = low("generic_local_parts")
        self.role = low("role_local_parts")
        self.freemail = low("freemail_domains")
        self.disposable = low("disposable_domains")
        self.junk_domains = low("junk_domains")
        self.junk_local = low("junk_local_parts")

    @staticmethod
    def split(email: str) -> tuple[str, str]:
        local, _, domain = email.rpartition("@")
        return local.lower(), domain.lower()

    def is_junk(self, email: str) -> str | None:
        """Reason the address is not a real business contact, or None."""
        local, domain = self.split(email)
        tld = domain.rsplit(".", 1)[-1]
        if tld in FILE_EXTENSIONS:
            return "file name, not an email"
        if domain_matches(domain, self.junk_domains):
            return "placeholder or platform domain"
        if local in self.junk_local:
            return "placeholder or no-reply address"
        if _HASHY_LOCAL.match(local) or len(local) > 40:
            return "machine-generated address"
        return None

    def email_type(self, email: str, business_name: str | None = None,
                   site_urls: list[str] | tuple = ()) -> str:
        """personal | generic | role. With business context, an address named
        after the business (smilestudio@gmail.com) counts as generic."""
        local, _ = self.split(email)
        base = re.split(r"[+]", local)[0]
        if base in self.role:
            return "role"
        if base in self.generic:
            return "generic"
        letters = re.sub(r"[^a-z]", "", base)
        if letters:
            for url in site_urls:
                label = (domain_label(url) or "").replace("-", "")
                if len(label) >= 4 and (label in letters or (len(letters) >= 5 and letters in label)):
                    return "generic"
            for token in name_tokens(business_name):
                if len(token) >= 5 and token in letters:
                    return "generic"
        return "personal"

    TYPE_RANK = {"personal": 0, "generic": 1, "role": 2}

    def contact_rank(self, email_type: str | None, email: str) -> tuple[int, int]:
        """Sort key for choosing which contacts to keep (lower is better)."""
        return self.TYPE_RANK.get(email_type or "", 3), int(self.is_freemail(email))

    def is_freemail(self, email: str) -> bool:
        return domain_matches(self.split(email)[1], self.freemail)

    def is_disposable(self, email: str) -> bool:
        return domain_matches(self.split(email)[1], self.disposable)

    def is_related(self, email: str, site_urls: list[str], business_name: str | None) -> bool:
        """Does the email plausibly belong to this business?

        True for addresses on the site's own domain (or its redirect target),
        common mailbox providers, and domains that share a clear name token
        with the site or business. Web agency / platform addresses are not.
        """
        _, domain = self.split(email)
        if self.is_freemail(email):
            return True
        email_reg = registered_domain(domain)
        site_regs = {registered_domain(u) for u in site_urls if u}
        if email_reg in site_regs:
            return True
        email_label = (domain_label(domain) or "").replace("-", "")
        if len(email_label) >= 4:
            for url in site_urls:
                site_label = (domain_label(url) or "").replace("-", "")
                if len(site_label) >= 4 and (email_label in site_label or site_label in email_label):
                    return True
            for token in name_tokens(business_name):
                if len(token) >= 5 and token in email_label:
                    return True
        return False
