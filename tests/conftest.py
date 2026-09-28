from pathlib import Path

import pytest

from leadscout.config import load_config
from leadscout.db.store import Store

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def cfg(tmp_path):
    return load_config(ROOT, local=False, overrides={
        "contact_email": "test@example.org",
        "paths": {
            "database": str(tmp_path / "test.db"),
            "exports": str(tmp_path / "exports"),
            "logs": str(tmp_path / "logs"),
        },
        "nominatim": {"min_interval_seconds": 0, "base_url": "https://nominatim.test"},
        "crawler": {"per_domain_min_interval_seconds": 0, "retries": 0},
        "size_check": {"wikidata": False, "min_interval_seconds": 0},
        # Most tests count every email found; test_contact_limit covers the limit.
        "filters": {"max_contacts_per_company": None},
    })


@pytest.fixture
def store(cfg):
    s = Store(cfg.db_path)
    yield s
    s.close()


def fixture_html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")
