"""Guessed emails for decision makers whose address isn't published.

The business's website names its president but only shows rebecca@company.com. Company
addresses follow one format, so the president's is probably chris@company.com. We:

1. learn the format from the company's own addresses: one we can tie to a named person
   (anna.lopez@ for Anna Lopez -> first.last), or a lone first name (rebecca@ -> first);
2. build the candidates for the decision maker, the learned format first;
3. ask the company's mail server which one exists (the mailbox check; nothing is sent).

A guess is kept only with a basis: confirmed by the mail server, or (unconfirmed) the format
the company's own addresses use. Guesses always rank after addresses written on the website.
"""

from __future__ import annotations

import re
import unicodedata

# Formats small companies use, most common first.
PATTERNS = ["first", "first.last", "flast", "firstlast", "first_last", "firstl", "f.last", "last", "lastf"]
_INBOX_WORDS = {"info", "contact", "office", "admin", "sales", "billing", "support", "hello", "mail", "team",
                "service", "services", "accounts", "accounting", "jobs", "careers", "hr", "frontdesk", "reception",
                "marketing", "orders", "help", "estimates", "quotes", "dispatch", "shop", "webmaster"}


def _ascii(word: str) -> str:
    word = unicodedata.normalize("NFKD", word).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z]", "", word.lower())


def name_parts(name: str) -> tuple[str, str] | None:
    """'Dr. Chris A. Martinez-Lopez' -> ('chris', 'martinezlopez'); None without first and last."""
    words = [w for w in re.split(r"\s+", name.strip()) if w and not w.endswith(".") or len(w) > 2]
    words = [_ascii(w) for w in words if _ascii(w) not in ("dr", "mr", "mrs", "ms", "jr", "sr", "ii", "iii")]
    words = [w for w in words if w]
    if len(words) < 2:
        return None
    return words[0], words[-1]


def render(pattern: str, first: str, last: str) -> str:
    return {
        "first": first, "first.last": f"{first}.{last}", "flast": f"{first[0]}{last}",
        "firstlast": f"{first}{last}", "first_last": f"{first}_{last}", "firstl": f"{first}{last[0]}",
        "f.last": f"{first[0]}.{last}", "last": last, "lastf": f"{last}{first[0]}",
    }[pattern]


def infer_pattern(known: list[tuple[str, str | None]]) -> tuple[str, str] | None:
    """The company's address format from (email, person name or None) pairs -> (pattern, example).

    An address tied to a named person shows the format exactly (anna.lopez@ -> first.last).
    Without names, a lone first-name-like address (rebecca@) suggests "first"."""
    for email, name in known:
        parts = name_parts(name) if name else None
        local = email.split("@", 1)[0].lower()
        if parts:
            for pattern in PATTERNS:
                if render(pattern, *parts) == local:
                    return pattern, email
    for email, name in known:
        local = email.split("@", 1)[0].lower()
        if name is None and local.isalpha() and 3 <= len(local) <= 10 and local not in _INBOX_WORDS:
            return "first", email
    return None


def candidates(name: str, domain: str, pattern: str | None = None, limit: int = 6) -> list[tuple[str, str]]:
    """(email, pattern) guesses for a person, the learned pattern first."""
    parts = name_parts(name)
    if not parts:
        return []
    order = ([pattern] if pattern else []) + [p for p in PATTERNS if p != pattern]
    out, seen = [], set()
    for p in order:
        email = f"{render(p, *parts)}@{domain}"
        if email not in seen:
            seen.add(email)
            out.append((email, p))
    return out[:limit]
