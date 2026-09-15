"""RBI Master Directions connector.

Page shape (verified against www.rbi.org.in/Scripts/BS_ViewMasterDirections.aspx):

The whole index is one flat ``table.tablebg``. Structure is carried by row
classes rather than nesting:

    <td class="tableheader"><b>Commercial Banks</b></td>     <- category
    <td class="tableheader"><b>Jul 31, 2026</b></td>         <- issue date
    <td><a class="link2" href=BS_ViewMasDirections.aspx?id=13637>Title</a></td>
    <td><a href='https://rbidocs.rbi.org.in/.../378MD65D4....PDF'>...</a>
        <span>1059 kb</span></td>

Category and date headers share the same class, so they are told apart by
trying to parse the text as a date -- anything that is not a date is a
category heading. Categories are therefore discovered, never hardcoded.

RBI repeats several entity groups (Commercial Banks, Payments Banks, ...)
because the page is sectioned by department; rows are merged under one
category key.

The page is fully server-rendered, so the plain HTTP fetcher is enough --
no browser needed.

Content strategy: each detail page carries the complete text of the
direction as HTML, so that is converted directly (lossless, no OCR). The
PDF is used only when the HTML body is missing or implausibly short.
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Iterable
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag
from pydantic import BaseModel, ConfigDict, Field

from policy_scraper.core.errors import FetchError
from policy_scraper.core.models import Category, ContentPayload, DocumentRef, MediaType
from policy_scraper.sources.base import SourceConnector, source_registry
from policy_scraper.utils.hashing import revision_key
from policy_scraper.utils.logging import get_logger

logger = get_logger(__name__)

_DATE_FORMATS = ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y")
_UPDATED_AS_ON = re.compile(r"updated\s+as\s+on\s+([A-Za-z]+\s+\d{1,2},?\s+\d{4})", re.IGNORECASE)
_DOC_ID = re.compile(r"[?&]id=(\d+)", re.IGNORECASE)


def _parse_date(text: str) -> _dt.date | None:
    cleaned = " ".join(text.split()).replace(",", ", ").replace(",,", ",")
    cleaned = " ".join(cleaned.split())
    for fmt in _DATE_FORMATS:
        try:
            return _dt.datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


class RbiMasterDirectionsOptions(BaseModel):
    """Connector-specific settings, validated out of ``source.options``."""

    model_config = ConfigDict(extra="forbid")

    index_url: str = "https://www.rbi.org.in/Scripts/BS_ViewMasterDirections.aspx"
    prefer: str = Field(
        default="html",
        pattern="^(html|pdf)$",
        description="Preferred representation. 'html' avoids OCR entirely.",
    )
    min_html_chars: int = Field(
        default=1500,
        ge=0,
        description="Below this, the HTML body is treated as a stub and the PDF is used instead.",
    )
    content_selectors: list[str] = Field(
        default_factory=lambda: [
            "tr.tablecontent2",
            "td.tablecontent2",
            "#annual .tablebg",
            "#annual",
        ],
        description="Tried in order to locate the body of a detail page.",
    )
    strip_selectors: list[str] = Field(
        default_factory=lambda: ["table.td table.tablebg tr td[colspan]:has(a[href^='#'])"],
        description="Removed from the extracted body before conversion.",
    )


@source_registry.register("rbi.master_directions")
class RbiMasterDirectionsConnector(SourceConnector[RbiMasterDirectionsOptions]):
    """Discovers and retrieves RBI Master Directions."""

    options_model = RbiMasterDirectionsOptions

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._index_cache: dict[str, list[DocumentRef]] | None = None
        self._category_order: list[Category] = []

    # ------------------------------------------------------------------ discovery

    def discover_categories(self) -> list[Category]:
        self._ensure_index()
        return list(self._category_order)

    def discover_documents(self, category: Category) -> Iterable[DocumentRef]:
        index = self._ensure_index()
        return index.get(category.key, [])

    def _ensure_index(self) -> dict[str, list[DocumentRef]]:
        """Parse the index page once; every category comes from that visit."""
        if self._index_cache is not None:
            return self._index_cache

        url = self.options.index_url
        logger.info("Fetching RBI Master Directions index: %s", url)
        response = self.fetcher.get(url)
        self._category_order, self._index_cache = self._parse_index(response.text, url)

        logger.info(
            "Discovered %d RBI categories, %d documents",
            len(self._category_order),
            sum(len(v) for v in self._index_cache.values()),
        )
        return self._index_cache

    def _parse_index(
        self, html: str, base_url: str
    ) -> tuple[list[Category], dict[str, list[DocumentRef]]]:
        soup = BeautifulSoup(html, "lxml")

        categories: dict[str, Category] = {}
        order: list[Category] = []
        documents: dict[str, list[DocumentRef]] = {}

        current: Category | None = None
        current_date: _dt.date | None = None

        for row in soup.find_all("tr"):
            header = self._header_text(row)
            if header is not None:
                as_date = _parse_date(header)
                if as_date is not None:
                    # A date sub-heading: applies to the rows that follow.
                    current_date = as_date
                else:
                    current = categories.get(_key(header)) or Category.from_display_name(
                        header, source_url=base_url
                    )
                    if current.key not in categories:
                        categories[current.key] = current
                        order.append(current)
                        documents[current.key] = []
                    current_date = None
                continue

            if current is None:
                continue  # Preamble rows above the first category heading.

            ref = self._parse_document_row(row, current, current_date, base_url)
            if ref is not None:
                documents[current.key].append(ref)

        return order, documents

    @staticmethod
    def _header_text(row: Tag) -> str | None:
        """Return the bold text of a ``tableheader`` row, if this is one."""
        cell = row.find("td", class_="tableheader")
        if cell is None:
            return None
        bold = cell.find("b")
        text = (bold or cell).get_text(" ", strip=True)
        return text or None

    def _parse_document_row(
        self, row: Tag, category: Category, published_at: _dt.date | None, base_url: str
    ) -> DocumentRef | None:
        link = row.find("a", class_="link2")
        if link is None or not link.get("href"):
            return None

        detail_url = urljoin(base_url, str(link["href"]).strip())
        match = _DOC_ID.search(detail_url)
        if match is None:
            logger.debug("Skipping RBI row with no document id: %s", detail_url)
            return None
        document_id = match.group(1)

        title = " ".join(link.get_text(" ", strip=True).split())
        if not title:
            return None

        pdf_url = self._find_pdf_url(row, base_url)
        size_label = self._find_size_label(row)

        updated_match = _UPDATED_AS_ON.search(title)
        source_updated_at = _parse_date(updated_match.group(1)) if updated_match else None

        return DocumentRef(
            document_id=f"rbi-md-{document_id}",
            title=title,
            category=category,
            source_url=detail_url,
            # The PDF filename embeds a content hash, so it changes whenever
            # RBI republishes -- the cheapest reliable change signal here.
            revision_key=revision_key(title, pdf_url, size_label, published_at),
            published_at=published_at,
            source_updated_at=source_updated_at,
            extra={
                "rbi_id": document_id,
                "pdf_url": pdf_url,
                "pdf_size_label": size_label,
                "instrument_type": "master_direction",
                "regulator": "RBI",
            },
        )

    @staticmethod
    def _find_pdf_url(row: Tag, base_url: str) -> str | None:
        for anchor in row.find_all("a", href=True):
            href = str(anchor["href"]).strip()
            if ".pdf" in href.lower():
                return urljoin(base_url, href)
        return None

    @staticmethod
    def _find_size_label(row: Tag) -> str | None:
        span = row.find("span", id=re.compile(r"^SPDF_"))
        return span.get_text(strip=True) if span else None

    # ------------------------------------------------------------------ retrieval

    def fetch_payload(self, ref: DocumentRef) -> ContentPayload:
        """HTML-first, PDF fallback.

        The detail page holds the full text of the direction, so converting
        it is lossless and free. The PDF path (and its OCR cost) is only
        taken when the HTML body is absent or too thin to be the real
        document.
        """
        pdf_url = ref.extra.get("pdf_url")

        if self.options.prefer == "html":
            body_html = self._extract_body(ref)
            if body_html is not None:
                return ContentPayload(
                    data=body_html.encode("utf-8"),
                    media_type=MediaType.HTML,
                    origin_url=ref.source_url,
                    extra={"strategy": "html"},
                )
            logger.info(
                "HTML body for %s was missing or under %d chars; falling back to PDF.",
                ref.document_id,
                self.options.min_html_chars,
            )

        if not pdf_url:
            raise FetchError(
                f"{ref.document_id} has no usable HTML body and no PDF link "
                f"({ref.source_url})."
            )

        response = self.fetcher.get(pdf_url)
        return ContentPayload(
            data=response.content,
            media_type=MediaType.PDF,
            origin_url=pdf_url,
            extra={"strategy": "pdf_fallback"},
        )

    def _extract_body(self, ref: DocumentRef) -> str | None:
        """Isolate the direction's text from a detail page, or return None."""
        try:
            response = self.fetcher.get(ref.source_url)
        except FetchError as exc:
            logger.warning("Could not load RBI detail page %s: %s", ref.source_url, exc)
            return None

        soup = BeautifulSoup(response.text, "lxml")
        for selector in self.options.content_selectors:
            element = soup.select_one(selector)
            if element is None:
                continue
            for stripped in self.options.strip_selectors:
                for node in element.select(stripped):
                    node.decompose()
            if len(element.get_text(" ", strip=True)) >= self.options.min_html_chars:
                return str(element)

        return None


def _key(display_name: str) -> str:
    from policy_scraper.utils.slug import slugify

    return slugify(display_name)
