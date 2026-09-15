"""GCS backend, against a fake client.

The GCS path is the one that runs in production but the one hardest to
exercise, so these tests stand in for a real bucket. The point is parity:
the pipeline must not be able to tell the two backends apart, because a
local run is supposed to be a faithful rehearsal of a bucket run.
"""

from __future__ import annotations

import pytest
from google.api_core.exceptions import Forbidden, NotFound
from google.cloud import storage

from policy_scraper.config.models import GcsStoreConfig, LocalStoreConfig
from policy_scraper.core.errors import ConfigurationError, StorageError
from policy_scraper.storage.gcs import GcsObjectStore
from policy_scraper.storage.local import LocalObjectStore


class FakeBlob:
    def __init__(self, bucket: "FakeBucket", name: str) -> None:
        self._bucket = bucket
        self.name = name

    def upload_from_string(self, data, content_type=None, timeout=None):
        if self._bucket.readonly:
            raise Forbidden("insufficient permissions")
        self._bucket.objects[self.name] = data
        self._bucket.content_types[self.name] = content_type

    def download_as_text(self, timeout=None):
        if self.name not in self._bucket.objects:
            raise NotFound(self.name)
        return self._bucket.objects[self.name].decode("utf-8")

    def exists(self, timeout=None):
        return self.name in self._bucket.objects

    def delete(self, timeout=None):
        if self.name not in self._bucket.objects:
            raise NotFound(self.name)
        del self._bucket.objects[self.name]


class FakeBucket:
    def __init__(self, name: str) -> None:
        self.name = name
        self.objects: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}
        self.readonly = False

    def blob(self, name: str) -> FakeBlob:
        return FakeBlob(self, name)

    def list_blobs(self, prefix="", timeout=None):
        return [FakeBlob(self, n) for n in self.objects if n.startswith(prefix)]


class FakeClient:
    def __init__(self, *args, **kwargs) -> None:
        self.buckets: dict[str, FakeBucket] = {}

    def bucket(self, name: str) -> FakeBucket:
        return self.buckets.setdefault(name, FakeBucket(name))


@pytest.fixture
def gcs(monkeypatch) -> GcsObjectStore:
    client = FakeClient()
    monkeypatch.setattr(storage, "Client", lambda *a, **k: client)
    return GcsObjectStore(GcsStoreConfig(bucket="test-bucket", project="test-project"))


class TestConfiguration:
    def test_an_unset_bucket_fails_fast(self):
        """The shipped config leaves the bucket as ``${GCS_BUCKET:}``, so
        this is the error a misconfigured deployment actually hits."""
        with pytest.raises(ConfigurationError, match="bucket must be set"):
            GcsObjectStore(GcsStoreConfig(bucket=""))

    def test_the_client_is_not_built_until_first_use(self, monkeypatch):
        """Constructing the store must not require credentials; nothing
        should touch the network until a run actually stores something."""
        monkeypatch.setattr(
            storage, "Client", lambda *a, **k: pytest.fail("client built too early")
        )
        GcsObjectStore(GcsStoreConfig(bucket="b"))


class TestOperations:
    def test_round_trip(self, gcs):
        stored = gcs.write_text("policies/rbi/a/doc.md", "# Hello")
        assert gcs.read_text("policies/rbi/a/doc.md") == "# Hello"
        assert stored.uri == "gs://test-bucket/policies/rbi/a/doc.md"
        assert stored.byte_size == len(b"# Hello")

    def test_reading_a_missing_key_returns_none(self, gcs):
        """Same contract as local: a first run has no manifest yet and must
        not crash."""
        assert gcs.read_text("policies/_index.yaml") is None

    def test_charset_is_declared_on_upload(self, gcs):
        gcs.write_text("a.yaml", "k: v", content_type="application/yaml")
        bucket = gcs._ensure_bucket()
        assert bucket.content_types["a.yaml"] == "application/yaml; charset=utf-8"

    def test_listing_is_prefix_scoped_and_sorted(self, gcs):
        gcs.write_text("policies/rbi/b.md", "x")
        gcs.write_text("policies/rbi/a.md", "x")
        gcs.write_text("policies/npci/c.md", "x")
        assert gcs.list_paths("policies/rbi") == ["policies/rbi/a.md", "policies/rbi/b.md"]

    def test_delete_reports_whether_anything_went(self, gcs):
        gcs.write_text("a.md", "x")
        assert gcs.delete("a.md") is True
        assert gcs.delete("a.md") is False

    def test_leading_slashes_are_normalised_away(self, gcs):
        """Otherwise the same logical key could land at two blob names."""
        gcs.write_text("/policies/a.md", "x")
        assert gcs.exists("policies/a.md")

    def test_an_api_error_becomes_a_storage_error(self, gcs):
        """A permissions failure must surface as the pipeline's own error
        type, not leak a google exception through the run."""
        gcs._ensure_bucket().readonly = True
        with pytest.raises(StorageError, match="Failed to upload"):
            gcs.write_text("a.md", "x")


class TestParityWithLocal:
    """The two backends must be indistinguishable to the pipeline."""

    @pytest.fixture
    def local(self, tmp_path) -> LocalObjectStore:
        return LocalObjectStore(LocalStoreConfig(base_path=str(tmp_path / "store")))

    def test_same_keys_and_listings(self, gcs, local):
        keys = [
            "policies/_index.yaml",
            "policies/rbi/commercial-banks/_manifest.yaml",
            "policies/rbi/commercial-banks/documents/kyc-rbi-md-1.md",
            "policies/npci/upi/documents/oc-186.v2.md",
        ]
        for store in (gcs, local):
            for key in keys:
                store.write_text(key, "body")

        assert gcs.list_paths("policies") == local.list_paths("policies")
        assert gcs.list_paths("policies/rbi") == local.list_paths("policies/rbi")

    def test_same_missing_key_behaviour(self, gcs, local):
        assert gcs.read_text("policies/absent.md") is local.read_text("policies/absent.md") is None
        assert gcs.exists("policies/absent.md") == local.exists("policies/absent.md") is False
        assert gcs.delete("policies/absent.md") == local.delete("policies/absent.md") is False
