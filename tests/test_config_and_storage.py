"""Config loading and the local object store."""

from __future__ import annotations

from pathlib import Path

import pytest

from policy_scraper.config.loader import load_config
from policy_scraper.config.models import LocalStoreConfig
from policy_scraper.core.errors import ConfigurationError, StorageError
from policy_scraper.core.models import UpdateStrategy
from policy_scraper.storage.local import LocalObjectStore

MINIMAL = """
version: 1
sources:
  - name: rbi
    type: rbi.master_directions
"""


def write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "sources.yaml"
    path.write_text(body, encoding="utf-8")
    return path


class TestEnvInterpolation:
    def test_a_set_variable_is_substituted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GCS_BUCKET", "my-bucket")
        config = load_config(
            write_config(tmp_path, MINIMAL + "\nstorage:\n  gcs:\n    bucket: ${GCS_BUCKET}\n")
        )
        assert config.storage.gcs.bucket == "my-bucket"

    def test_the_default_is_used_when_unset(self, tmp_path, monkeypatch):
        monkeypatch.delenv("STORAGE_BACKEND", raising=False)
        config = load_config(
            write_config(tmp_path, MINIMAL + "\nstorage:\n  backend: ${STORAGE_BACKEND:local}\n")
        )
        assert config.storage.backend == "local"

    def test_an_env_value_beats_the_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STORAGE_BACKEND", "gcs")
        config = load_config(
            write_config(
                tmp_path,
                MINIMAL
                + "\nstorage:\n  backend: ${STORAGE_BACKEND:local}\n  gcs:\n    bucket: b\n",
            )
        )
        assert config.storage.backend == "gcs"

    def test_an_empty_default_is_respected(self, tmp_path, monkeypatch):
        """``${GCS_BUCKET:}`` is how the shipped config leaves the bucket
        unset; it must not be read as "no default given"."""
        monkeypatch.delenv("GCS_BUCKET", raising=False)
        config = load_config(
            write_config(tmp_path, MINIMAL + "\nstorage:\n  gcs:\n    bucket: ${GCS_BUCKET:}\n")
        )
        assert config.storage.gcs.bucket == ""

    def test_a_missing_variable_with_no_default_is_an_error(self, tmp_path, monkeypatch):
        monkeypatch.delenv("NOT_SET_ANYWHERE", raising=False)
        path = write_config(
            tmp_path, MINIMAL + "\nstorage:\n  gcs:\n    bucket: ${NOT_SET_ANYWHERE}\n"
        )
        with pytest.raises(ConfigurationError, match="NOT_SET_ANYWHERE"):
            load_config(path)

    def test_interpolation_reaches_nested_source_options(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RBI_URL", "https://example.test/index")
        config = load_config(
            write_config(
                tmp_path,
                "version: 1\nsources:\n  - name: rbi\n    type: rbi.master_directions\n"
                "    options:\n      index_url: ${RBI_URL}\n",
            )
        )
        assert config.sources[0].options["index_url"] == "https://example.test/index"


class TestConfigValidation:
    def test_a_missing_file_is_reported_clearly(self, tmp_path):
        with pytest.raises(ConfigurationError, match="not found"):
            load_config(tmp_path / "nope.yaml")

    def test_invalid_yaml_is_reported_clearly(self, tmp_path):
        with pytest.raises(ConfigurationError, match="not valid YAML"):
            load_config(write_config(tmp_path, "key: [unclosed\n"))

    def test_a_non_mapping_document_is_rejected(self, tmp_path):
        with pytest.raises(ConfigurationError, match="mapping"):
            load_config(write_config(tmp_path, "- just\n- a list\n"))

    def test_duplicate_source_names_are_rejected(self, tmp_path):
        """Source names are storage folders, so a duplicate would have two
        sources writing over each other."""
        body = (
            "version: 1\nsources:\n"
            "  - name: rbi\n    type: rbi.master_directions\n"
            "  - name: rbi\n    type: npci.circulars\n"
        )
        with pytest.raises(ConfigurationError):
            load_config(write_config(tmp_path, body))

    def test_an_unknown_key_is_rejected_rather_than_silently_ignored(self, tmp_path):
        with pytest.raises(ConfigurationError):
            load_config(write_config(tmp_path, MINIMAL + "\nstorge:\n  backend: local\n"))

    def test_defaults_apply_when_only_sources_are_given(self, tmp_path):
        config = load_config(write_config(tmp_path, MINIMAL))
        assert config.storage.backend == "local"
        assert config.sources[0].update_strategy is UpdateStrategy.REPLACE_IN_PLACE
        assert config.sources[0].enabled is True


class TestShippedConfig:
    def test_the_real_config_file_loads(self, monkeypatch):
        """Catches a typo in config/sources.yaml before a run does."""
        monkeypatch.delenv("STORAGE_BACKEND", raising=False)
        config = load_config("config/sources.yaml")
        names = {s.name for s in config.sources}
        assert {"rbi", "npci"} <= names

    def test_npci_uses_the_browser_fetcher(self, monkeypatch):
        """It 403s anything else, so this is load-bearing, not cosmetic."""
        monkeypatch.delenv("STORAGE_BACKEND", raising=False)
        config = load_config("config/sources.yaml")
        npci = next(s for s in config.sources if s.name == "npci")
        assert npci.fetcher == "browser"
        assert npci.update_strategy is UpdateStrategy.NEW_VERSION

    def test_rbi_replaces_in_place(self, monkeypatch):
        monkeypatch.delenv("STORAGE_BACKEND", raising=False)
        config = load_config("config/sources.yaml")
        rbi = next(s for s in config.sources if s.name == "rbi")
        assert rbi.update_strategy is UpdateStrategy.REPLACE_IN_PLACE

    def test_ocr_stays_enabled(self, monkeypatch):
        """NPCI circulars are scanned images; with OCR off they silently
        convert to nothing."""
        monkeypatch.delenv("STORAGE_BACKEND", raising=False)
        assert load_config("config/sources.yaml").conversion.pdf.docling_local.ocr_enabled

    def test_the_measured_ocr_settings_are_pinned(self, monkeypatch):
        """These three were chosen by measurement, not preference, and each
        degrades accuracy if changed casually. See docs/ocr-accuracy.md.

        pypdfium especially: docling's default backend returns a *blank
        page* for some NPCI scans, with a SUCCESS status and no error.
        """
        monkeypatch.delenv("STORAGE_BACKEND", raising=False)
        docling = load_config("config/sources.yaml").conversion.pdf.docling_local
        assert docling.pdf_backend == "pypdfium"
        assert docling.ocr_engine == "tesseract"
        assert docling.ocr_mode == "default"

    def test_currency_repair_stays_on(self, monkeypatch):
        """No OCR engine reads the rupee sign. With this off, wrong amounts
        reach the corpus unflagged.

        It lives on the PDF config rather than under a backend on purpose:
        switching to a docling service must not drop it.
        """
        monkeypatch.delenv("STORAGE_BACKEND", raising=False)
        assert load_config("config/sources.yaml").conversion.pdf.repair_currency


class TestLocalObjectStore:
    def store(self, tmp_path: Path) -> LocalObjectStore:
        return LocalObjectStore(LocalStoreConfig(base_path=str(tmp_path / "store")))

    def test_round_trip(self, tmp_path):
        store = self.store(tmp_path)
        stored = store.write_text("policies/rbi/a/doc.md", "# Hello")
        assert store.read_text("policies/rbi/a/doc.md") == "# Hello"
        assert stored.byte_size == len(b"# Hello")

    def test_reading_a_missing_key_returns_none(self, tmp_path):
        """The pipeline relies on this for a first run with no manifest."""
        assert self.store(tmp_path).read_text("policies/nothing.yaml") is None

    def test_nested_keys_create_their_directories(self, tmp_path):
        store = self.store(tmp_path)
        store.write_text("policies/npci/upi/documents/deep.md", "x")
        assert store.exists("policies/npci/upi/documents/deep.md")

    def test_writes_overwrite(self, tmp_path):
        store = self.store(tmp_path)
        store.write_text("a.md", "first")
        store.write_text("a.md", "second")
        assert store.read_text("a.md") == "second"

    def test_listing_is_prefix_scoped(self, tmp_path):
        store = self.store(tmp_path)
        store.write_text("policies/rbi/a/doc.md", "x")
        store.write_text("policies/npci/upi/doc.md", "y")
        assert store.list_paths("policies/rbi") == ["policies/rbi/a/doc.md"]

    def test_delete_reports_whether_anything_went(self, tmp_path):
        store = self.store(tmp_path)
        store.write_text("a.md", "x")
        assert store.delete("a.md") is True
        assert store.delete("a.md") is False

    def test_a_traversing_key_is_refused(self, tmp_path):
        """Keys are built from scraped titles, so escaping the store root
        must not be possible."""
        with pytest.raises(StorageError, match="outside the store root"):
            self.store(tmp_path).write_text("../../escaped.md", "x")
