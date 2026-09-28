"""The 50 industries (config/industries.yaml) and how businesses are matched to them.

Businesses come from Overture Maps, where each place has a category path such as
``services_and_business > home_service > plumbing``. An industry lists the Overture
categories it covers; a business belongs to the industry of the deepest category
in its path that some industry lists.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

_GROUP_RE = re.compile(r"^\s*(tier|top)\s*[-_ ]?\s*(\d+)\s*$", re.I)


class UnknownIndustry(ValueError):
    def __init__(self, word: str, suggestions: list[str]):
        self.word = word
        self.suggestions = suggestions
        hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
        super().__init__(
            f"Unknown industry '{word}'.{hint} Run 'leadscout industries' to see all of them."
        )


class NotOnMap(ValueError):
    """The industry exists but has no business categories to search."""

    def __init__(self, rule: "IndustryRule"):
        self.rule = rule
        note = f" {rule.note}" if rule.note else ""
        super().__init__(
            f"'{rule.label}' can't be searched.{note} "
            "Collect their websites another way and use 'Enrich my list' / 'leadscout enrich'."
        )


@dataclass
class IndustryRule:
    key: str
    label: str
    overture: list[str] = field(default_factory=list)   # Overture categories covered
    aliases: list[str] = field(default_factory=list)
    rank: int | None = None
    group: str | None = None
    searchable: bool = True
    note: str | None = None
    tier: int | None = None

    @property
    def on_map(self) -> bool:
        return self.searchable and bool(self.overture)


def load_groups(raw: dict) -> dict[str, str]:
    """Section key -> label, from the '_groups' part of industries.yaml."""
    return dict((raw or {}).get("_groups") or {})


def load_exclude(raw: dict) -> set[str]:
    """Overture categories that are never leads (banks, hospitals, government ...)."""
    return set((raw or {}).get("_exclude") or [])


def load_rules(raw: dict) -> dict[str, IndustryRule]:
    rules = {}
    for key, spec in (raw or {}).items():
        if str(key).startswith("_"):
            continue
        spec = spec or {}
        rules[key] = IndustryRule(
            key=key,
            label=spec.get("label") or key.replace("_", " ").title(),
            overture=list(spec.get("overture") or []),
            aliases=list(spec.get("aliases") or []),
            rank=spec.get("rank"),
            group=spec.get("group"),
            searchable=bool(spec.get("searchable", True)),
            note=spec.get("note"),
            tier=spec.get("tier"),
        )
    return rules


def category_map(rules: dict[str, IndustryRule]) -> dict[str, str]:
    """Overture category -> industry key."""
    out: dict[str, str] = {}
    for rule in ranked(rules):
        for node in rule.overture:
            out.setdefault(node, rule.key)
    return out


def classify(path: list[str] | None, cats: dict[str, str], exclude: set[str] = frozenset(),
             alternates: list[str] | None = None) -> str | None:
    """Industry key for an Overture category path (deepest listed category wins), or None."""
    path = list(path or [])
    if not path or any(node in exclude for node in path):
        return None
    for node in reversed(path):
        if node in cats:
            return cats[node]
    for node in alternates or []:
        if node in cats and node not in exclude:
            return cats[node]
    return None


def _norm(word: str) -> str:
    return re.sub(r"[\s_\-]+", " ", word.strip().lower())


def ranked(rules: dict[str, IndustryRule]) -> list[IndustryRule]:
    """Rules in rank order; unranked ones last, alphabetically."""
    return sorted(rules.values(), key=lambda r: (r.rank is None, r.rank or 0, r.label.lower()))


def group_members(word: str, rules: dict[str, IndustryRule],
                  groups: dict[str, str] | None = None) -> list[IndustryRule] | None:
    """Rules for a group word, or None if the word is not a group.

    'group:healthcare' or a section label ('Healthcare & Wellness') -> that section;
    'top10' -> ranks 1-10. A bare word that is also an industry key means the industry.
    """
    wanted = _norm(word)
    explicit = wanted.startswith("group:")
    if explicit:
        wanted = wanted[6:].strip()
    group_keys = {r.group for r in rules.values() if r.group}
    for key in group_keys:
        label = (groups or {}).get(key, "")
        key_match = wanted == _norm(key) and (explicit or key not in rules)
        if key_match or (label and wanted == _norm(label)):
            return [r for r in ranked(rules) if r.group == key and r.on_map]
    m = _GROUP_RE.match(word)
    if not m:
        return None
    kind, n = m.group(1).lower(), int(m.group(2))
    if kind == "tier":
        members = [r for r in ranked(rules) if r.tier == n]
    else:
        members = [r for r in ranked(rules) if r.rank is not None and r.rank <= n]
    return [r for r in members if r.on_map]


def group_options(rules: dict[str, IndustryRule],
                  groups: dict[str, str] | None = None) -> list[tuple[str, str]]:
    """(section key, description) for each section, in rank order."""
    out, seen = [], []
    for rule in ranked(rules):
        if rule.group and rule.group not in seen:
            seen.append(rule.group)
    for key in seen:
        members = [r for r in rules.values() if r.group == key and r.on_map]
        out.append((key, f"{(groups or {}).get(key, key)} ({len(members)} industries)"))
    return out


def _singulars(text: str) -> list[str]:
    """Possible singular forms of a phrase's last word: agencies -> agency, dentists -> dentist."""
    out = []
    for suffix in ("ies", "es", "s"):
        if text.endswith(suffix) and len(text) > len(suffix) + 2:
            stem = text[: -len(suffix)]
            out.append(stem + "y" if suffix == "ies" else stem)
    return out


def resolve_industry(word: str, rules: dict[str, IndustryRule]) -> IndustryRule:
    """Resolve what the user typed to an industry: its key, label, a search word
    (alias), simple plurals, or part of a name that points to one industry."""
    wanted = _norm(word)
    names: dict[str, str] = {}
    for key, rule in rules.items():
        for name in (key, rule.label, *rule.aliases, *rule.overture):
            names.setdefault(_norm(name), key)
    for name, key in list(names.items()):     # "seo agencies" also answers to "seo agency"
        for single in _singulars(name):
            names.setdefault(single, key)
    for cand in [wanted, *_singulars(wanted)]:
        if cand in names:
            return rules[names[cand]]
    if len(wanted) >= 4:
        pattern = re.compile(r"\b" + re.escape(wanted))
        hits = {key for key, rule in rules.items()
                for name in (rule.label, *rule.aliases) if pattern.search(_norm(name))}
        if len(hits) == 1:
            return rules[hits.pop()]
    close = difflib.get_close_matches(wanted, list(names), n=3, cutoff=0.7)
    raise UnknownIndustry(word, sorted({names[c] for c in close}))


def resolve_many(words: list[str], rules: dict[str, IndustryRule],
                 groups: dict[str, str] | None = None) -> list[IndustryRule]:
    """Resolve industry words and groups (group:healthcare, top10) for a search."""
    out: list[IndustryRule] = []
    seen: set[str] = set()
    for word in words:
        members = group_members(word, rules, groups)
        if members is None:
            rule = resolve_industry(word, rules)
            if not rule.on_map:
                raise NotOnMap(rule)
            members = [rule]
        for rule in members:
            if rule.key not in seen:
                seen.add(rule.key)
                out.append(rule)
    return out
