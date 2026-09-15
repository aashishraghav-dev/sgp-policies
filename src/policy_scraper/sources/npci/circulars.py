"""NPCI circulars connector.

npci.org.in is a React SPA behind Akamai Bot Manager. Scraping the
rendered DOM would be slow and brittle, but the SPA is backed by a clean
JSON API that the browser fetcher can reach:

    GET /api/circulars/{product}?pageNum=1&year=2026&sort=desc&size=100&locale=en
    -> {"status": 200,
        "data": {"pageNum": 1, "size": 100, "totalCount": 19,
                 "files": [{"id": 3963,
                            "fileName": "UPI | OC No. 186A | ...",
                            "mediaType": "pdf",
                            "yearLabel": "FY 26-27",
                            "media": {"url": "/uploads/UPI_..._e695625b85.pdf"}}]}}

The edge rejects the same request made out-of-band -- including from an
``APIRequestContext`` carrying the browser's own cookies -- so all calls go
through the browser fetcher's in-page ``fetch()``. A single navigation to
the product page bootstraps the clearance cookies for the whole run.

Products (UPI, IMPS, AePS, NACH, ...) are discovered from the page
metadata endpoint rather than hardcoded; config selects which ones to
scrape.

Circular PDFs are scanned images with no text layer, so they must go
through OCR -- there is no HTML alternative to prefer here.
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Any, Iterable
from urllib.parse import urljoin

from pydantic import BaseModel, ConfigDict, Field

from policy_scraper.core.errors import FetchError, PermanentError
from policy_scraper.core.models import Category, ContentPayload, DocumentRef, MediaType
from policy_scraper.fetch.browser import BrowserFetcher
from policy_scraper.sources.base import SourceConnector, source_registry
from policy_scraper.utils.hashing import revision_key
from policy_scraper.utils.logging import get_logger

logger = get_logger(__name__)

# "FY 26-27" / "FY 2026-27" -> the starting financial year.
_FY_LABEL = re.compile(r"FY\s*(\d{2,4})\s*[-/]\s*(\d{2,4})", re.IGNORECASE)


class NpciCircularsOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = "https://www.npci.org.in"
    page_path: str = Field(
        default="/circulars/{product}", description="SPA route used to bootstrap the origin."
    )
    api_path: str = Field(default="/api/circulars/{product}")
    products_api_path: str = Field(
        default="/api/circulars-and-notifications-page/{product}",
        description="Returns the product switcher, used to discover available products.",
    )
    discovery_product: str = Field(
        default="upi", description="Product page consulted to enumerate the others."
    )
    page_size: int = Field(default=100, ge=1, le=500)
    max_pages: int = Field(default=20, ge=1, description="Hard stop against runaway pagination.")
    years: list[int] = Field(
        default_factory=list, description="Explicit calendar years; empty means use lookback_years."
    )
    lookback_years: int = Field(
        default=1, ge=1, description="How many years back from today to pull when years is empty."
    )
    locale: str = "en"
    sort: str = "desc"


@source_registry.register("npci.circulars")
class NpciCircularsConnector(SourceConnector[NpciCircularsOptions]):
    """Discovers and retrieves NPCI product circulars."""

    options_model = NpciCircularsOptions

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        if not isinstance(self.fetcher, BrowserFetcher):
            raise PermanentError(
                f"Source {self.config.name!r} must use the browser fetcher: npci.org.in "
                f"returns 403 to every non-browser client. Set 'fetcher: browser'."
            )
        self._bootstrapped: set[str] = set()

    # ------------------------------------------------------------------ discovery

    def discover_categories(self) -> list[Category]:
        """Enumerate NPCI products from the page-metadata endpoint.

        Falls back to whatever the config explicitly asked for if the
        endpoint's shape changes, so a site tweak degrades rather than
        breaks the run.
        """
        product = self.options.discovery_product
        self._bootstrap(product)
        url = self._url(self.options.products_api_path, product)

        try:
            payload = self.fetcher.get_json(url)
        except (FetchError, ValueError) as exc:
            logger.warning("NPCI product discovery failed (%s); using configured products.", exc)
            return self._configured_categories()

        products = self._extract_products(payload)
        # The discovery product itself is the current page, so it is not in
        # its own switcher list.
        products.setdefault(product, product.upper())

        categories = [
            Category(key=key, display_name=name, source_url=self._page_url(key))
            for key, name in sorted(products.items())
        ]
        logger.info("Discovered %d NPCI products: %s", len(categories), ", ".join(products))
        return categories or self._configured_categories()

    def _extract_products(self, payload: Any) -> dict[str, str]:
        """Pull ``{product_key: display_name}`` out of the page metadata.

        Walks the payload rather than indexing a fixed path, because the
        CMS nests the switcher differently per product page.
        """
        products: dict[str, str] = {}

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                link = node.get("link")
                if isinstance(link, str) and "/circulars/" in link:
                    key = link.rstrip("/").rsplit("/", 1)[-1].strip().lower()
                    if key:
                        logo = node.get("logo") or {}
                        name = (logo.get("productName") if isinstance(logo, dict) else None) or key
                        products[key] = str(name).strip() or key.upper()
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(payload)
        return products

    def _configured_categories(self) -> list[Category]:
        return [
            Category(key=key, display_name=key.upper(), source_url=self._page_url(key))
            for key in self.config.categories.include
        ]

    def discover_documents(self, category: Category) -> Iterable[DocumentRef]:
        self._bootstrap(category.key)
        seen: set[str] = set()
        skipped = 0

        for year in self._target_years():
            for item in self._iter_year(category.key, year):
                ref = self._to_ref(item, category, year)
                if ref is None:
                    skipped += 1
                    continue
                if ref.document_id not in seen:
                    seen.add(ref.document_id)
                    yield ref

        if skipped:
            # NPCI's CMS regularly lists circulars with no file attached.
            # Report the count so a genuine regression is distinguishable
            # from the publisher's own housekeeping.
            logger.warning(
                "%s: skipped %d listed circular(s) with no downloadable file.",
                category.key,
                skipped,
            )

    def _target_years(self) -> list[int]:
        if self.options.years:
            return sorted(set(self.options.years), reverse=True)
        current = _dt.date.today().year
        return [current - offset for offset in range(self.options.lookback_years)]

    def _iter_year(self, product: str, year: int) -> Iterable[dict[str, Any]]:
        """Page through one product-year, stopping at totalCount."""
        retrieved = 0
        for page in range(1, self.options.max_pages + 1):
            url = (
                f"{self._url(self.options.api_path, product)}"
                f"?pageNum={page}&year={year}&sort={self.options.sort}"
                f"&size={self.options.page_size}&locale={self.options.locale}"
            )
            try:
                payload = self.fetcher.get_json(url)
            except (FetchError, ValueError) as exc:
                logger.warning("NPCI %s/%s page %d failed: %s", product, year, page, exc)
                return

            data = payload.get("data") or {}
            files = data.get("files") or []
            total = int(data.get("totalCount") or 0)

            if not files:
                return

            yield from files
            retrieved += len(files)
            logger.debug("NPCI %s/%s page %d: %d/%d", product, year, page, retrieved, total)
            if retrieved >= total:
                return

    def _to_ref(self, item: dict[str, Any], category: Category, year: int) -> DocumentRef | None:
        media = item.get("media") or {}
        media_path = media.get("url") if isinstance(media, dict) else None
        item_id = item.get("id")
        title = " ".join(str(item.get("fileName") or "").split())

        if not media_path or item_id is None or not title:
            logger.debug("Skipping malformed NPCI item: %s", item)
            return None

        media_type = str(item.get("mediaType") or "").lower()
        if media_type and media_type != "pdf":
            logger.debug("Skipping non-PDF NPCI item %s (%s)", item_id, media_type)
            return None

        pdf_url = urljoin(self.options.base_url, str(media_path))
        year_label = item.get("yearLabel")

        return DocumentRef(
            document_id=f"npci-{category.key}-{item_id}",
            title=title,
            category=category,
            # The SPA has no per-circular route, so the product listing is
            # the closest human-readable landing page.
            source_url=self._page_url(category.key),
            # The upload path carries a CMS content hash, so it changes on
            # any re-upload -- a reliable, download-free change signal.
            revision_key=revision_key(title, media_path, item.get("updatedAt")),
            published_at=self._published_date(item, year_label, year),
            extra={
                "npci_id": item_id,
                "product": category.key,
                "pdf_url": pdf_url,
                "financial_year": year_label,
                "calendar_year": year,
                "instrument_type": "circular",
                "regulator": "NPCI",
            },
        )

    @staticmethod
    def _published_date(item: dict[str, Any], year_label: Any, year: int) -> _dt.date | None:
        """Best-effort publication date.

        The API exposes no per-circular date, only a financial-year label.
        The FY start year is recorded so documents still sort sensibly;
        the precise date lives inside the PDF.
        """
        for key in ("publishedAt", "createdAt", "updatedAt"):
            raw = item.get(key)
            if isinstance(raw, str) and raw:
                try:
                    return _dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
                except ValueError:
                    pass

        match = _FY_LABEL.search(str(year_label or ""))
        if match:
            start = match.group(1)
            start_year = int(start) if len(start) == 4 else 2000 + int(start)
            return _dt.date(start_year, 4, 1)  # Indian FY begins 1 April.

        return _dt.date(year, 1, 1)

    # ------------------------------------------------------------------ retrieval

    def fetch_payload(self, ref: DocumentRef) -> ContentPayload:
        pdf_url = ref.extra["pdf_url"]
        self._bootstrap(ref.category.key)
        response = self.fetcher.get(pdf_url)

        if not response.content.startswith(b"%PDF"):
            raise FetchError(
                f"{pdf_url} did not return a PDF (got {len(response.content)} bytes, "
                f"content-type {response.headers.get('content-type')!r}); "
                f"the bot wall may have served a challenge page."
            )

        return ContentPayload(
            data=response.content,
            media_type=MediaType.PDF,
            origin_url=pdf_url,
            # Recorded so the OCR requirement is visible in the manifest.
            extra={"strategy": "pdf", "requires_ocr": True},
        )

    # ------------------------------------------------------------------ helpers

    def _bootstrap(self, product: str) -> None:
        """Visit a product page once so the edge plants clearance cookies."""
        if product in self._bootstrapped:
            return
        assert isinstance(self.fetcher, BrowserFetcher)
        self.fetcher.bootstrap(self._page_url(product))
        self._bootstrapped.add(product)

    def _url(self, template: str, product: str) -> str:
        return urljoin(self.options.base_url, template.format(product=product))

    def _page_url(self, product: str) -> str:
        return self._url(self.options.page_path, product)
