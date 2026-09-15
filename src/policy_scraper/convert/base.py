"""Document conversion abstraction.

A converter turns a :class:`ContentPayload` into markdown. The pipeline
only ever talks to this interface, which is what lets docling run either
in-process or as a remote service without any caller changing.
"""

from __future__ import annotations

import abc

from policy_scraper.core.models import ContentPayload, ConversionResult, MediaType
from policy_scraper.utils.registry import Registry


class DocumentConverter(abc.ABC):
    """Converts a payload of one or more media types into markdown."""

    name: str = "converter"
    supported_media_types: frozenset[MediaType] = frozenset()

    def supports(self, media_type: MediaType) -> bool:
        return media_type in self.supported_media_types

    @abc.abstractmethod
    def convert(self, payload: ContentPayload, *, title: str | None = None) -> ConversionResult:
        """Produce markdown from ``payload``. Raises ``ConversionError``."""

    def health_check(self) -> bool:
        """Report whether the converter is usable (models loaded, service up)."""
        return True

    def close(self) -> None:
        """Release any held resources. Idempotent."""


converter_registry: Registry[DocumentConverter] = Registry("converter")
