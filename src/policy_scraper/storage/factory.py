"""Object-store construction from config."""

from __future__ import annotations

from policy_scraper.config.models import StorageConfig
from policy_scraper.core.errors import ConfigurationError
from policy_scraper.storage.base import ObjectStore
from policy_scraper.storage.gcs import GcsObjectStore
from policy_scraper.storage.local import LocalObjectStore


def build_object_store(config: StorageConfig) -> ObjectStore:
    if config.backend == "local":
        return LocalObjectStore(config.local)
    if config.backend == "gcs":
        return GcsObjectStore(config.gcs)
    raise ConfigurationError(f"Unknown storage backend {config.backend!r}.")
