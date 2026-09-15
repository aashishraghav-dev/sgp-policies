"""Manifest schema.

A manifest is the YAML sidecar that records what is in the bucket. It is
what makes incremental runs possible: the next run compares freshly
discovered documents against these entries and only does work where
something actually changed.

It is also the index an agent reads to find the right policy document
without listing the bucket.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from policy_scraper.core.models import MediaType

SCHEMA_VERSION = 1


def _utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)


class ManifestEntry(BaseModel):
    """One stored markdown document."""

    model_config = ConfigDict(extra="allow")

    document_id: str
    title: str
    storage_path: str

    source_url: str = Field(description="Human-readable landing page at the publisher.")
    origin_url: str = Field(description="URL the bytes were actually fetched from.")

    # Change detection.
    revision_key: str = Field(description="Fingerprint of listing-page signals; drives diffing.")
    content_sha256: str = Field(description="Hash of the markdown, to catch silent edits.")

    # Provenance.
    source_media_type: MediaType
    converter: str
    page_count: int | None = None
    byte_size: int = 0

    # Publisher dates, where the source exposes them.
    published_at: _dt.date | None = None
    source_updated_at: _dt.date | None = None

    # Our bookkeeping.
    version: int = 1
    first_seen_at: _dt.datetime = Field(default_factory=_utc_now)
    last_updated_at: _dt.datetime = Field(default_factory=_utc_now)

    extra: dict[str, Any] = Field(default_factory=dict)


class CategoryManifest(BaseModel):
    """All documents stored under one (source, category)."""

    model_config = ConfigDict(extra="allow")

    schema_version: int = SCHEMA_VERSION
    source: str
    category_key: str
    category_display_name: str
    category_url: str | None = None
    generated_at: _dt.datetime = Field(default_factory=_utc_now)
    document_count: int = 0
    documents: list[ManifestEntry] = Field(default_factory=list)

    def by_id(self) -> dict[str, ManifestEntry]:
        return {entry.document_id: entry for entry in self.documents}

    def upsert(self, entry: ManifestEntry) -> None:
        """Replace the entry with the same id, or append it."""
        for index, existing in enumerate(self.documents):
            if existing.document_id == entry.document_id:
                # Preserve when we first saw the document across updates.
                entry.first_seen_at = existing.first_seen_at
                self.documents[index] = entry
                return
        self.documents.append(entry)

    def finalise(self) -> "CategoryManifest":
        """Sort deterministically and refresh derived fields before writing."""
        self.documents.sort(key=lambda e: (e.published_at or _dt.date.min, e.title), reverse=True)
        self.document_count = len(self.documents)
        self.generated_at = _utc_now()
        return self


class CategorySummary(BaseModel):
    """One line in the top-level index."""

    source: str
    category_key: str
    category_display_name: str
    manifest_path: str
    document_count: int
    last_run_at: _dt.datetime = Field(default_factory=_utc_now)


class CatalogIndex(BaseModel):
    """Bucket-wide roll-up: every category we hold, and how big it is."""

    model_config = ConfigDict(extra="allow")

    schema_version: int = SCHEMA_VERSION
    generated_at: _dt.datetime = Field(default_factory=_utc_now)
    categories: list[CategorySummary] = Field(default_factory=list)

    def upsert(self, summary: CategorySummary) -> None:
        key = (summary.source, summary.category_key)
        for index, existing in enumerate(self.categories):
            if (existing.source, existing.category_key) == key:
                self.categories[index] = summary
                return
        self.categories.append(summary)

    def finalise(self) -> "CatalogIndex":
        self.categories.sort(key=lambda c: (c.source, c.category_key))
        self.generated_at = _utc_now()
        return self
