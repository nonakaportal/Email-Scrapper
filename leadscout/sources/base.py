"""Source adapter interface: every source yields BusinessCandidate objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class BusinessCandidate:
    name: str
    source: str                      # osm | google | csv
    source_ref: str                  # stable ID within the source
    industry: str | None = None
    address: str | None = None
    city: str | None = None
    region: str | None = None
    postcode: str | None = None
    country: str | None = None       # ISO alpha-2
    lat: float | None = None
    lng: float | None = None
    phone: str | None = None         # raw value, normalized by the store
    website: str | None = None       # raw value, normalized by the store
    emails: list[str] = field(default_factory=list)
    socials: list[str] = field(default_factory=list)   # profile URLs (Facebook, Instagram ...)
    listing_updated: str | None = None   # YYYY-MM-DD the data provider last updated this listing
    listing_provider: str | None = None  # e.g. Foursquare, meta, microsoft
    tags: dict = field(default_factory=dict)

    @property
    def brand(self) -> str | None:
        return self.tags.get("brand") or self.tags.get("brand:wikidata")


class Source(Protocol):
    name: str

    def search(self, location, industry) -> list[BusinessCandidate]: ...
