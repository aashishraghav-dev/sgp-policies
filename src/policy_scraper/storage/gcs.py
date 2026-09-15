"""Google Cloud Storage object store.

The client is imported lazily so a local-only run needs neither the
``google-cloud-storage`` package at import time nor any credentials.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from policy_scraper.config.models import GcsStoreConfig
from policy_scraper.core.errors import ConfigurationError, StorageError
from policy_scraper.core.models import StoredObject
from policy_scraper.storage.base import ObjectStore
from policy_scraper.utils.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from google.cloud.storage import Bucket, Client

logger = get_logger(__name__)


class GcsObjectStore(ObjectStore):
    """Reads and writes objects in a GCS bucket."""

    name = "gcs"

    def __init__(self, config: GcsStoreConfig) -> None:
        if not config.bucket:
            raise ConfigurationError("storage.gcs.bucket must be set.")
        self._config = config
        self._client: "Client | None" = None
        self._bucket: "Bucket | None" = None

    def _ensure_bucket(self) -> "Bucket":
        if self._bucket is not None:
            return self._bucket
        try:
            from google.api_core.exceptions import GoogleAPIError  # noqa: F401
            from google.cloud import storage
        except ImportError as exc:  # pragma: no cover
            raise ConfigurationError(
                "google-cloud-storage is required for the 'gcs' storage backend. "
                "Install it with: pip install google-cloud-storage"
            ) from exc

        if self._config.credentials_path:
            self._client = storage.Client.from_service_account_json(
                self._config.credentials_path, project=self._config.project
            )
        else:
            # Application Default Credentials: what Cloud Run will use.
            self._client = storage.Client(project=self._config.project)

        self._bucket = self._client.bucket(self._config.bucket)
        logger.info("GCS object store bound to gs://%s", self._config.bucket)
        return self._bucket

    def _blob(self, path: str) -> Any:
        return self._ensure_bucket().blob(path.strip("/"))

    def write_text(self, path: str, content: str, *, content_type: str = "text/plain") -> StoredObject:
        from google.api_core.exceptions import GoogleAPIError

        data = content.encode("utf-8")
        try:
            self._blob(path).upload_from_string(
                data,
                content_type=f"{content_type}; charset=utf-8",
                timeout=self._config.timeout_seconds,
            )
        except GoogleAPIError as exc:
            raise StorageError(f"Failed to upload gs://{self._config.bucket}/{path}: {exc}") from exc
        return StoredObject(path=path, uri=self.uri(path), byte_size=len(data))

    def read_text(self, path: str) -> str | None:
        from google.api_core.exceptions import GoogleAPIError, NotFound

        try:
            return self._blob(path).download_as_text(timeout=self._config.timeout_seconds)
        except NotFound:
            return None
        except GoogleAPIError as exc:
            raise StorageError(f"Failed to read gs://{self._config.bucket}/{path}: {exc}") from exc

    def exists(self, path: str) -> bool:
        from google.api_core.exceptions import GoogleAPIError

        try:
            return bool(self._blob(path).exists(timeout=self._config.timeout_seconds))
        except GoogleAPIError as exc:
            raise StorageError(f"Failed to stat gs://{self._config.bucket}/{path}: {exc}") from exc

    def list_paths(self, prefix: str) -> list[str]:
        from google.api_core.exceptions import GoogleAPIError

        try:
            blobs = self._ensure_bucket().list_blobs(
                prefix=prefix.strip("/"), timeout=self._config.timeout_seconds
            )
            return sorted(blob.name for blob in blobs)
        except GoogleAPIError as exc:
            raise StorageError(f"Failed to list gs://{self._config.bucket}/{prefix}: {exc}") from exc

    def delete(self, path: str) -> bool:
        from google.api_core.exceptions import GoogleAPIError, NotFound

        try:
            self._blob(path).delete(timeout=self._config.timeout_seconds)
            return True
        except NotFound:
            return False
        except GoogleAPIError as exc:
            raise StorageError(f"Failed to delete gs://{self._config.bucket}/{path}: {exc}") from exc

    def uri(self, path: str) -> str:
        return f"gs://{self._config.bucket}/{path.strip('/')}"

    def close(self) -> None:
        self._client = self._bucket = None
