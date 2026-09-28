"""Settings loading: built-in defaults merged with config/*.yaml.

The project root is the folder that contains ``config/settings.yaml``. It is
found from $LEADSCOUT_HOME, then the current directory and its parents, then
the folder above this package. Relative paths in settings resolve against it.
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from leadscout import __version__

DEFAULTS: dict[str, Any] = {
    "user_agent": f"LeadScout/{__version__} (self-hosted B2B research tool)",
    "contact_email": "",
    "paths": {"database": "data/leadscout.db", "exports": "exports", "logs": "logs"},
    "nominatim": {
        "base_url": "https://nominatim.openstreetmap.org",
        "min_interval_seconds": 1.1,
        "cache_days": 90,
        "fallback_radius_km": 10,
    },
    "overture": {
        "release": None,           # None = latest monthly release
        "min_confidence": 0.5,     # skip places Overture is unsure about
        "tile_degrees": 0.5,       # big areas are downloaded in tiles of this size...
        "max_tiles": 64,           # ...but never more than this many
        "cache_days": 30,
    },
    "crawler": {
        "max_pages_per_site": 5,
        "timeout_seconds": 15,
        "retries": 2,
        "per_domain_min_interval_seconds": 2.0,
        "global_concurrency": 16,
        "max_page_bytes": 2_000_000,
        "respect_robots_txt": True,
    },
    "email": {
        "verify_mx": True,
        "dns_timeout_seconds": 5,
        "dns_cache_days": 7,
        "dns_concurrency": 16,
    },
    "non_business_domains": [],
    "size_check": {
        "wikidata": True,
        "min_interval_seconds": 0.3,
        "cache_days": 90,
        "max_employees": 250,
        "exclude_wikipedia_notable": True,
        "intl_site_languages": 6,
        "investor_relations": True,
        "non_commercial_suffixes": [".edu", ".gov", ".mil", ".int", ".gov.uk", ".ac.uk", ".nhs.uk",
                                    ".edu.au", ".gov.au", ".gc.ca", ".gouv.fr", ".gov.in",
                                    ".nic.in", ".ac.in", ".edu.in"],
    },
    "filters": {
        "require_website": True,
        "exclude_chains": True,
        "chain_name_repeat_threshold": 3,
        "max_results": 2000,
        "min_score": 0,
        "max_contacts_per_company": 3,
    },
    "scoring": {
        "email_valid": 25,
        "email_risky": 10,
        "email_personal": 15,
        "email_generic": 5,
        "email_role": 3,
        "independent": 20,
        "single_location": 10,
        "has_phone": 10,
        "website_active": 10,
        "website_active_years": 2,
        "has_social": 5,
        "small_team": 5,
        "small_team_max_people": 50,
    },
    "export": {"default_statuses": ["valid"], "mode": "company", "format": "csv"},
}


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def find_root() -> Path:
    env = os.environ.get("LEADSCOUT_HOME")
    if env:
        return Path(env).resolve()
    cwd = Path.cwd().resolve()
    for candidate in (cwd, *cwd.parents):
        if (candidate / "config" / "settings.yaml").exists():
            return candidate
    return Path(__file__).resolve().parent.parent


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _load_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            lines.append(line.lower())
    return lines


@dataclass
class Config:
    root: Path
    settings: dict
    industries: dict = field(default_factory=dict)
    email_rules: dict = field(default_factory=dict)
    known_chains: list[str] = field(default_factory=list)

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.settings
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def path(self, key: str) -> Path:
        p = Path(self.get(f"paths.{key}"))
        return p if p.is_absolute() else self.root / p

    @property
    def db_path(self) -> Path:
        return self.path("database")

    @property
    def export_dir(self) -> Path:
        return self.path("exports")

    @property
    def log_dir(self) -> Path:
        return self.path("logs")

    @property
    def contact_email(self) -> str:
        return (self.get("contact_email") or "").strip()

    @property
    def user_agent(self) -> str:
        ua = (self.get("user_agent") or DEFAULTS["user_agent"]).strip()
        email = self.contact_email
        if not email:
            return ua
        if ua.endswith(")"):
            return f"{ua[:-1]}; +mailto:{email})"
        return f"{ua} (+mailto:{email})"


LOCAL_SETTINGS = "settings.local.yaml"


def save_local_settings(root: Path, values: dict) -> Path:
    """Merge ``values`` into config/settings.local.yaml (written by the dashboard,
    so the commented settings.yaml is never rewritten)."""
    path = Path(root) / "config" / LOCAL_SETTINGS
    merged = deep_merge(_load_yaml(path), values)
    path.write_text(yaml.safe_dump(merged, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


def load_config(root: Path | None = None, overrides: dict | None = None,
                local: bool = True) -> Config:
    root = Path(root).resolve() if root else find_root()
    conf_dir = root / "config"
    settings = deep_merge(DEFAULTS, _load_yaml(conf_dir / "settings.yaml"))
    if local:
        settings = deep_merge(settings, _load_yaml(conf_dir / LOCAL_SETTINGS))
        if os.environ.get("LEADSCOUT_CONTACT_EMAIL"):
            settings["contact_email"] = os.environ["LEADSCOUT_CONTACT_EMAIL"]
    if overrides:
        settings = deep_merge(settings, overrides)
    return Config(
        root=root,
        settings=settings,
        industries=_load_yaml(conf_dir / "industries.yaml"),
        email_rules=_load_yaml(conf_dir / "email_rules.yaml"),
        known_chains=_load_lines(conf_dir / "known_chains.txt"),
    )
