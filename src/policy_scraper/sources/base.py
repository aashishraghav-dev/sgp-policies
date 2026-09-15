"""Source connector interface.

A connector is the *only* place a website's peculiarities are allowed to
live. It answers three questions, and nothing else:

  1. :meth:`discover_categories` -- what groupings does this source have?
  2. :meth:`discover_documents`  -- what documents are in a category, and
     what cheap signal tells us whether each one changed?
  3. :meth:`fetch_payload`       -- give me the bytes for one document,
     choosing the best available representation.

Adding a website means adding one subclass and one YAML block. No other
file changes.
"""

from __future__ import annotations

import abc
from typing import Any, ClassVar, Generic, Iterable, TypeVar

from pydantic import BaseModel, ValidationError

from policy_scraper.config.models import SourceConfig
from policy_scraper.core.errors import ConfigurationError
from policy_scraper.core.models import Category, ContentPayload, DocumentRef
from policy_scraper.fetch.base import Fetcher
from policy_scraper.utils.registry import Registry

OptionsT = TypeVar("OptionsT", bound=BaseModel)


class SourceConnector(abc.ABC, Generic[OptionsT]):
    """Translates one website into the pipeline's domain model.

    Subclasses declare an ``options_model``; the free-form
    ``SourceConfig.options`` mapping is validated into it at construction,
    so connector settings get the same typo-checking as core config
    without core config having to know about them.
    """

    options_model: ClassVar[type[BaseModel]]

    def __init__(self, config: SourceConfig, fetcher: Fetcher) -> None:
        self.config = config
        self.fetcher = fetcher
        try:
            self.options: OptionsT = self.options_model.model_validate(config.options)  # type: ignore[assignment]
        except ValidationError as exc:
            raise ConfigurationError(
                f"Invalid options for source {config.name!r} (type {config.type!r}):\n{exc}"
            ) from exc

    @property
    def name(self) -> str:
        return self.config.name

    @abc.abstractmethod
    def discover_categories(self) -> list[Category]:
        """List every category the source publishes, in the source's order.

        Return all of them. Filtering and limiting is the pipeline's job,
        driven by config -- that keeps "only 5 categories for milestone 1"
        a deployment decision rather than a code change.
        """

    @abc.abstractmethod
    def discover_documents(self, category: Category) -> Iterable[DocumentRef]:
        """Yield the documents in a category, newest first where knowable."""

    @abc.abstractmethod
    def fetch_payload(self, ref: DocumentRef) -> ContentPayload:
        """Retrieve the best available representation of one document."""

    def document_metadata(self, ref: DocumentRef) -> dict[str, Any]:
        """Extra key/values to record in the manifest. Override as needed."""
        return dict(ref.extra)

    def close(self) -> None:
        """Release connector-held resources. The fetcher is owned elsewhere."""


source_registry: Registry[SourceConnector] = Registry("source connector")


def build_connector(config: SourceConfig, fetcher: Fetcher) -> SourceConnector:
    """Instantiate the connector named by ``config.type``."""
    return source_registry.get(config.type)(config, fetcher)
