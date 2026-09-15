"""Routes a payload to the converter that handles its media type.

This is what makes "HTML-first, PDF fallback" a property of the *data*
rather than a branch in every connector: a connector returns whichever
payload it considers best, and the router picks the matching converter.
"""

from __future__ import annotations

from policy_scraper.config.models import ConversionConfig
from policy_scraper.convert.base import DocumentConverter
from policy_scraper.convert.docling_local import LocalDoclingConverter
from policy_scraper.convert.docling_remote import RemoteDoclingConverter
from policy_scraper.convert.html import HtmlToMarkdownConverter
from policy_scraper.core.errors import ConfigurationError, ConversionError
from policy_scraper.core.models import ContentPayload, ConversionResult, MediaType
from policy_scraper.utils.logging import get_logger

logger = get_logger(__name__)


def build_pdf_converter(config: ConversionConfig) -> DocumentConverter:
    """Instantiate the configured PDF backend (local library or service)."""
    backend = config.pdf.backend
    if backend == "docling_local":
        return LocalDoclingConverter(config.pdf.docling_local)
    if backend == "docling_remote":
        return RemoteDoclingConverter(config.pdf.docling_remote)
    raise ConfigurationError(f"Unknown PDF conversion backend {backend!r}.")


class ConverterRouter(DocumentConverter):
    """Dispatches by media type; itself a converter, so it composes."""

    name = "router"

    def __init__(self, config: ConversionConfig) -> None:
        self._converters: dict[MediaType, DocumentConverter] = {
            MediaType.HTML: HtmlToMarkdownConverter(config.html),
            MediaType.PDF: build_pdf_converter(config),
        }
        self.supported_media_types = frozenset(self._converters)

    def converter_for(self, media_type: MediaType) -> DocumentConverter:
        try:
            return self._converters[media_type]
        except KeyError:
            raise ConversionError(
                f"No converter registered for media type {media_type.value!r}. "
                f"Supported: {', '.join(m.value for m in self._converters)}."
            ) from None

    def convert(self, payload: ContentPayload, *, title: str | None = None) -> ConversionResult:
        converter = self.converter_for(payload.media_type)
        logger.debug("Converting %s via %s", payload.origin_url, converter.name)
        return converter.convert(payload, title=title)

    def health_check(self) -> bool:
        return all(c.health_check() for c in self._converters.values())

    def close(self) -> None:
        for converter in self._converters.values():
            converter.close()
