"""Reads and writes manifests through the object store."""

from __future__ import annotations

import datetime as _dt
from enum import Enum
from typing import Any

import yaml
from pydantic import ValidationError

from policy_scraper.catalog.layout import StorageLayout
from policy_scraper.catalog.models import CatalogIndex, CategoryManifest, CategorySummary
from policy_scraper.storage.base import ObjectStore
from policy_scraper.utils.logging import get_logger

logger = get_logger(__name__)


def _to_yaml_safe(value: Any) -> Any:
    """Render pydantic output as plain YAML scalars.

    Dates and enums are emitted as strings so the manifests stay readable
    and portable rather than carrying Python-specific tags.
    """
    if isinstance(value, dict):
        return {k: _to_yaml_safe(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_to_yaml_safe(v) for v in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, _dt.datetime):
        return value.isoformat()
    if isinstance(value, _dt.date):
        return value.isoformat()
    return value


def dump_yaml(model: Any) -> str:
    return yaml.safe_dump(
        _to_yaml_safe(model.model_dump(mode="python")),
        sort_keys=False,
        allow_unicode=True,
        width=100,
    )


class CatalogRepository:
    """Persistence for category manifests and the bucket-wide index.

    Manifests are cached per run: a category is read once, mutated in
    memory as documents are processed, then written once at the end.
    """

    def __init__(self, store: ObjectStore, layout: StorageLayout) -> None:
        self._store = store
        self._layout = layout
        self._cache: dict[tuple[str, str], CategoryManifest] = {}

    # ------------------------------------------------------------------ manifests

    def load_manifest(
        self, source: str, category_key: str, *, display_name: str, category_url: str | None = None
    ) -> CategoryManifest:
        cache_key = (source, category_key)
        if cache_key in self._cache:
            return self._cache[cache_key]

        path = self._layout.manifest_path(source, category_key)
        raw = self._store.read_text(path)

        if raw is None:
            manifest = CategoryManifest(
                source=source,
                category_key=category_key,
                category_display_name=display_name,
                category_url=category_url,
            )
        else:
            try:
                manifest = CategoryManifest.model_validate(yaml.safe_load(raw) or {})
                # Refresh labels in case the publisher renamed the category.
                manifest.category_display_name = display_name
                manifest.category_url = category_url or manifest.category_url
            except (ValidationError, yaml.YAMLError) as exc:
                # A corrupt manifest must not abort the run: rebuild it.
                # Documents already in the bucket will simply be re-converted.
                logger.warning("Manifest %s is unreadable (%s); rebuilding it.", path, exc)
                manifest = CategoryManifest(
                    source=source,
                    category_key=category_key,
                    category_display_name=display_name,
                    category_url=category_url,
                )

        self._cache[cache_key] = manifest
        return manifest

    def save_manifest(self, manifest: CategoryManifest) -> str:
        path = self._layout.manifest_path(manifest.source, manifest.category_key)
        self._store.write_text(path, dump_yaml(manifest.finalise()), content_type="application/yaml")
        logger.debug("Wrote manifest %s (%d documents)", path, manifest.document_count)
        return path

    # ------------------------------------------------------------------ index

    def load_index(self) -> CatalogIndex:
        raw = self._store.read_text(self._layout.index_path())
        if raw is None:
            return CatalogIndex()
        try:
            return CatalogIndex.model_validate(yaml.safe_load(raw) or {})
        except (ValidationError, yaml.YAMLError) as exc:
            logger.warning("Catalog index is unreadable (%s); rebuilding it.", exc)
            return CatalogIndex()

    def save_index(self, index: CatalogIndex) -> str:
        path = self._layout.index_path()
        self._store.write_text(path, dump_yaml(index.finalise()), content_type="application/yaml")
        return path

    def refresh_index(self, manifests: list[CategoryManifest]) -> str:
        """Merge this run's manifests into the existing index and save it."""
        index = self.load_index()
        for manifest in manifests:
            index.upsert(
                CategorySummary(
                    source=manifest.source,
                    category_key=manifest.category_key,
                    category_display_name=manifest.category_display_name,
                    manifest_path=self._layout.manifest_path(manifest.source, manifest.category_key),
                    document_count=manifest.document_count,
                )
            )
        return self.save_index(index)
