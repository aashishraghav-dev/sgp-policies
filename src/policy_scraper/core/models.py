"""Source-agnostic domain model.

Everything that crosses a component boundary (connector -> converter ->
store -> catalog) is one of these types.  Connectors translate whatever
shape a website happens to expose into these; no downstream component
ever learns which website a document came from.
"""

from __future__ import annotations

import datetime as _dt
import enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from policy_scraper.utils.hashing import sha256_of_text
from policy_scraper.utils.slug import slugify


class MediaType(str, enum.Enum):
    """Payload formats the pipeline knows how to route to a converter."""

    HTML = "text/html"
    PDF = "application/pdf"
    MARKDOWN = "text/markdown"
    TEXT = "text/plain"

    @classmethod
    def from_header(cls, value: str | None, default: "MediaType | None" = None) -> "MediaType":
        """Map a raw Content-Type header onto a known media type.

        ``default`` is resolved here rather than in the signature: inside
        the class body ``TEXT`` is still the bare string ``"text/plain"``,
        so a default written there would hand back a ``str`` instead of a
        member every time the lookup fell through.
        """
        default = cls.TEXT if default is None else default
        if not value:
            return default
        base = value.split(";", 1)[0].strip().lower()
        for member in cls:
            if member.value == base:
                return member
        if base in {"application/xhtml+xml"}:
            return cls.HTML
        if base in {"application/x-pdf", "application/acrobat"}:
            return cls.PDF
        return default


class UpdateStrategy(str, enum.Enum):
    """How a source's documents evolve over time.

    REPLACE_IN_PLACE
        The publisher amends a document under a stable identity (RBI
        Master Directions -- "Updated as on <date>").  A change overwrites
        the existing markdown object at the same path.

    NEW_VERSION
        The publisher issues a fresh document each time and never edits an
        old one (NPCI circulars).  A change is written to a new,
        version-suffixed path so history is preserved.
    """

    REPLACE_IN_PLACE = "replace_in_place"
    NEW_VERSION = "new_version"


class ChangeType(str, enum.Enum):
    """Outcome of comparing a discovered document against the catalog."""

    CREATED = "created"
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    FAILED = "failed"
    SKIPPED = "skipped"


class Category(BaseModel):
    """A grouping of documents within a source (RBI subject, NPCI product)."""

    model_config = ConfigDict(frozen=True)

    key: str = Field(description="Stable, path-safe identifier, e.g. 'commercial-banks'.")
    display_name: str = Field(description="Human label as published by the source.")
    source_url: str | None = Field(default=None, description="Page the category was discovered on.")
    extra: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_display_name(cls, display_name: str, **kwargs: Any) -> "Category":
        return cls(key=slugify(display_name), display_name=display_name.strip(), **kwargs)


class DocumentRef(BaseModel):
    """A document located during discovery, before its content is fetched.

    ``revision_key`` is the heart of incremental scraping: it is built from
    signals that are visible on the *listing* page, so the pipeline can
    decide whether a document changed without downloading it.  Connectors
    choose the strongest cheap signal available (RBI embeds a content hash
    in its PDF filename; NPCI's upload URLs carry a Strapi content hash).
    """

    model_config = ConfigDict(frozen=True)

    document_id: str = Field(description="Stable id, unique within (source, category).")
    title: str
    category: Category
    source_url: str = Field(description="Canonical landing page for a human reader.")
    revision_key: str = Field(description="Fingerprint of listing-visible change signals.")

    published_at: _dt.date | None = None
    source_updated_at: _dt.date | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    @property
    def slug(self) -> str:
        """Path-safe filename stem combining title and id."""
        return f"{slugify(self.title, max_length=90)}-{slugify(self.document_id)}"


class ContentPayload(BaseModel):
    """Raw bytes for one document, tagged with how to interpret them."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    data: bytes
    media_type: MediaType
    origin_url: str = Field(description="URL the bytes were actually retrieved from.")
    encoding: str = "utf-8"
    extra: dict[str, Any] = Field(default_factory=dict)

    def as_text(self) -> str:
        return self.data.decode(self.encoding, errors="replace")


class ConversionResult(BaseModel):
    """Markdown produced from a payload, plus provenance about how."""

    model_config = ConfigDict(frozen=True)

    markdown: str
    converter: str = Field(description="Converter name, recorded in the catalog for provenance.")
    source_media_type: MediaType
    page_count: int | None = None
    duration_seconds: float | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    @property
    def content_sha256(self) -> str:
        return sha256_of_text(self.markdown)


class StoredObject(BaseModel):
    """Where a markdown document landed in object storage."""

    model_config = ConfigDict(frozen=True)

    path: str
    uri: str
    byte_size: int


class DocumentOutcome(BaseModel):
    """Per-document result, aggregated into the run report."""

    document_id: str
    title: str
    source: str
    category_key: str
    change: ChangeType
    storage_path: str | None = None
    converter: str | None = None
    error: str | None = None
    duration_seconds: float | None = None
