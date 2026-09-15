"""End-to-end pipeline runs against a fake source and local storage.

This is the test that actually proves the incremental promise: run twice
over an unchanged site and nothing should be fetched, converted or written
the second time. A fake connector makes that observable by counting its own
fetches.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel

from policy_scraper.catalog.models import CategoryManifest
from policy_scraper.config.models import AppConfig
from policy_scraper.core.errors import FetchError
from policy_scraper.core.models import (
    Category,
    ChangeType,
    ContentPayload,
    DocumentRef,
    MediaType,
)
from policy_scraper.pipeline.orchestrator import ScrapePipeline
from policy_scraper.sources.base import SourceConnector, source_registry
from policy_scraper.utils.hashing import revision_key

PAGE = "<html><body><h1>{title}</h1><p>Body revision {revision}.</p></body></html>"


class FakeOptions(BaseModel):
    pass


class FakeConnector(SourceConnector[FakeOptions]):
    """A source whose content the test controls directly.

    ``site`` maps category key -> list of ``(document_id, title, revision)``,
    optionally ``(document_id, title, revision, body)``. Keeping the
    listing-page signal and the document body independently settable is
    what lets a test say "the listing changed but the text did not".
    """

    options_model = FakeOptions

    site: dict[str, list[tuple[str, ...]]] = {}
    fetch_count: int = 0
    fail_ids: set[str] = set()

    @classmethod
    def reset(cls, site: dict[str, list[tuple[str, ...]]]) -> None:
        cls.site = site
        cls.fetch_count = 0
        cls.fail_ids = set()

    @classmethod
    def _entry(cls, category_key: str, document_id: str) -> tuple[str, ...]:
        return next(e for e in cls.site[category_key] if e[0] == document_id)

    def discover_categories(self) -> list[Category]:
        return [
            Category(key=key, display_name=key.upper(), source_url=f"https://fake.test/{key}")
            for key in self.site
        ]

    def discover_documents(self, category: Category):
        for entry in self.site[category.key]:
            document_id, title, revision = entry[0], entry[1], entry[2]
            yield DocumentRef(
                document_id=document_id,
                title=title,
                category=category,
                source_url=f"https://fake.test/{category.key}/{document_id}",
                revision_key=revision_key(title, revision),
            )

    def fetch_payload(self, ref: DocumentRef) -> ContentPayload:
        type(self).fetch_count += 1
        if ref.document_id in self.fail_ids:
            raise FetchError(f"simulated failure for {ref.document_id}")

        entry = self._entry(ref.category.key, ref.document_id)
        body = entry[3] if len(entry) > 3 else entry[2]
        return ContentPayload(
            data=PAGE.format(title=ref.title, revision=body).encode("utf-8"),
            media_type=MediaType.HTML,
            origin_url=ref.source_url,
        )


@pytest.fixture(autouse=True, scope="module")
def _register_fake():
    source_registry.register("fake.source")(FakeConnector)


@pytest.fixture
def make_config(tmp_path):
    def _make(**source_overrides) -> AppConfig:
        source = {
            "name": "fake",
            "type": "fake.source",
            "fetcher": "http",
            "options": {},
        }
        source.update(source_overrides)
        return AppConfig.model_validate(
            {
                "version": 1,
                "run": {
                    "max_workers": 1,
                    "log_level": "WARNING",
                    # Into tmp_path, not the working tree: the default path
                    # is relative, so leaving it would have every test run
                    # litter the real metrics/ directory.
                    "metrics": {"path": str(tmp_path / "metrics" / "run.jsonl")},
                },
                "storage": {
                    "backend": "local",
                    "root_prefix": "policies",
                    "local": {"base_path": str(tmp_path / "store")},
                },
                "sources": [source],
            }
        )

    return _make


def read(tmp_path, key: str) -> str:
    return (tmp_path / "store" / key).read_text(encoding="utf-8")


def counts(report) -> dict[str, int]:
    tally: dict[str, int] = {}
    for source in report.source_reports:
        for category in source.category_reports:
            for outcome in category.outcomes:
                tally[outcome.change.value] = tally.get(outcome.change.value, 0) + 1
    return tally


class TestFirstRun:
    def test_documents_are_created_and_written(self, make_config, tmp_path):
        FakeConnector.reset({"upi": [("d1", "First Doc", "r1"), ("d2", "Second Doc", "r1")]})
        report = ScrapePipeline(make_config()).run()

        assert counts(report) == {"created": 2}
        body = read(tmp_path, "policies/fake/upi/documents/first-doc-d1.md")
        assert "Body revision r1." in body

    def test_front_matter_is_embedded(self, make_config, tmp_path):
        FakeConnector.reset({"upi": [("d1", "First Doc", "r1")]})
        ScrapePipeline(make_config()).run()

        body = read(tmp_path, "policies/fake/upi/documents/first-doc-d1.md")
        front = yaml.safe_load(body.split("---")[1])
        assert front["document_id"] == "d1"
        assert front["source"] == "fake"
        assert front["category_key"] == "upi"
        assert front["version"] == 1
        assert front["content_sha256"].startswith("sha256:")

    def test_manifest_and_index_are_written(self, make_config, tmp_path):
        FakeConnector.reset({"upi": [("d1", "First Doc", "r1")]})
        ScrapePipeline(make_config()).run()

        manifest = yaml.safe_load(read(tmp_path, "policies/fake/upi/_manifest.yaml"))
        assert manifest["document_count"] == 1
        assert manifest["documents"][0]["document_id"] == "d1"

        index = yaml.safe_load(read(tmp_path, "policies/_index.yaml"))
        assert index["categories"][0]["manifest_path"] == "policies/fake/upi/_manifest.yaml"


class TestIncrementalReRun:
    def test_an_unchanged_site_fetches_nothing_the_second_time(self, make_config):
        """The whole point of the revision_key diff: cost on a no-op run is
        one listing page, not N downloads."""
        FakeConnector.reset({"upi": [("d1", "First Doc", "r1"), ("d2", "Second Doc", "r1")]})
        ScrapePipeline(make_config()).run()
        assert FakeConnector.fetch_count == 2

        report = ScrapePipeline(make_config()).run()
        assert counts(report) == {"unchanged": 2}
        assert FakeConnector.fetch_count == 2, "no document should have been re-fetched"

    def test_only_the_changed_document_is_re_fetched(self, make_config, tmp_path):
        FakeConnector.reset({"upi": [("d1", "First Doc", "r1"), ("d2", "Second Doc", "r1")]})
        ScrapePipeline(make_config()).run()

        FakeConnector.site = {"upi": [("d1", "First Doc", "r2"), ("d2", "Second Doc", "r1")]}
        report = ScrapePipeline(make_config()).run()

        assert counts(report) == {"updated": 1, "unchanged": 1}
        assert FakeConnector.fetch_count == 3
        assert "Body revision r2." in read(
            tmp_path, "policies/fake/upi/documents/first-doc-d1.md"
        )

    def test_a_new_document_is_created_alongside_the_existing_ones(self, make_config):
        FakeConnector.reset({"upi": [("d1", "First Doc", "r1")]})
        ScrapePipeline(make_config()).run()

        FakeConnector.site = {"upi": [("d1", "First Doc", "r1"), ("d3", "Third Doc", "r1")]}
        report = ScrapePipeline(make_config()).run()
        assert counts(report) == {"created": 1, "unchanged": 1}

    def test_force_reconverts_everything(self, make_config):
        FakeConnector.reset({"upi": [("d1", "First Doc", "r1"), ("d2", "Second Doc", "r1")]})
        ScrapePipeline(make_config()).run()

        config = make_config()
        config.run.force_refetch = True
        report = ScrapePipeline(config).run()

        assert FakeConnector.fetch_count == 4, "force must bypass the diff"
        # Re-converted to byte-identical markdown, so the pipeline correctly
        # declines to churn the bucket.
        assert counts(report) == {"unchanged": 2}

    def test_a_cosmetic_revision_change_does_not_bump_the_version(self, make_config, tmp_path):
        """The second safety net: the revision_key moved, so the document is
        re-fetched, but the converted markdown is byte-identical. A
        publisher re-uploading the same text must not churn the bucket."""
        FakeConnector.reset({"upi": [("d1", "First Doc", "r1", "unchanging body")]})
        ScrapePipeline(make_config()).run()
        assert FakeConnector.fetch_count == 1

        # New listing-page signal, same document text.
        FakeConnector.site = {"upi": [("d1", "First Doc", "r2", "unchanging body")]}
        report = ScrapePipeline(make_config()).run()

        assert FakeConnector.fetch_count == 2, "the moved key should trigger a re-fetch"
        assert counts(report) == {"unchanged": 1}, "identical text is not an update"

        entry = yaml.safe_load(read(tmp_path, "policies/fake/upi/_manifest.yaml"))["documents"][0]
        assert entry["version"] == 1, "version must not advance on a no-op republish"
        assert entry["revision_key"] == revision_key("First Doc", "r2"), (
            "the new signal must still be recorded, or every future run re-fetches"
        )


class TestUpdateStrategies:
    def test_replace_in_place_overwrites_one_key(self, make_config, tmp_path):
        FakeConnector.reset({"upi": [("d1", "First Doc", "r1")]})
        ScrapePipeline(make_config(update_strategy="replace_in_place")).run()
        FakeConnector.site = {"upi": [("d1", "First Doc", "r2")]}
        ScrapePipeline(make_config(update_strategy="replace_in_place")).run()

        documents = sorted(p.name for p in (tmp_path / "store/policies/fake/upi/documents").iterdir())
        assert documents == ["first-doc-d1.md"]
        assert "Body revision r2." in read(tmp_path, "policies/fake/upi/documents/first-doc-d1.md")

    def test_new_version_keeps_the_previous_revision(self, make_config, tmp_path):
        FakeConnector.reset({"upi": [("d1", "First Doc", "r1")]})
        ScrapePipeline(make_config(update_strategy="new_version")).run()
        FakeConnector.site = {"upi": [("d1", "First Doc", "r2")]}
        ScrapePipeline(make_config(update_strategy="new_version")).run()

        documents = sorted(p.name for p in (tmp_path / "store/policies/fake/upi/documents").iterdir())
        assert documents == ["first-doc-d1.md", "first-doc-d1.v2.md"]
        assert "Body revision r1." in read(tmp_path, "policies/fake/upi/documents/first-doc-d1.md")
        assert "Body revision r2." in read(
            tmp_path, "policies/fake/upi/documents/first-doc-d1.v2.md"
        )


class TestLimitsAndFiltering:
    def test_category_cap_is_applied(self, make_config):
        FakeConnector.reset({key: [(f"{key}-1", f"Doc {key}", "r1")] for key in "abcde"})
        config = make_config(limits={"max_categories": 2})
        report = ScrapePipeline(config).run()

        assert report.source_reports[0].categories_discovered == 5
        assert report.source_reports[0].categories_scraped == 2

    def test_include_ordering_is_honoured(self, make_config):
        """Selection must be deterministic, not dependent on page order."""
        FakeConnector.reset({key: [(f"{key}-1", f"Doc {key}", "r1")] for key in "abcde"})
        config = make_config(
            limits={"max_categories": 2}, categories={"include": ["d", "b"]}
        )
        report = ScrapePipeline(config).run()
        scraped = [c.category_key for c in report.source_reports[0].category_reports]
        assert scraped == ["d", "b"]

    def test_per_category_document_cap_is_applied(self, make_config):
        FakeConnector.reset({"upi": [(f"d{i}", f"Doc {i}", "r1") for i in range(10)]})
        report = ScrapePipeline(make_config(limits={"max_documents_per_category": 3})).run()
        assert counts(report) == {"created": 3}


class TestDryRun:
    def test_nothing_is_written(self, make_config, tmp_path):
        FakeConnector.reset({"upi": [("d1", "First Doc", "r1")]})
        config = make_config()
        config.run.dry_run = True
        report = ScrapePipeline(config).run()

        assert counts(report) == {"created": 1}
        assert not (tmp_path / "store/policies/fake").exists()

    def test_a_dry_run_does_not_poison_the_next_real_run(self, make_config, tmp_path):
        """A dry run must not record anything that makes the real run think
        the work is already done."""
        FakeConnector.reset({"upi": [("d1", "First Doc", "r1")]})
        config = make_config()
        config.run.dry_run = True
        ScrapePipeline(config).run()

        report = ScrapePipeline(make_config()).run()
        assert counts(report) == {"created": 1}
        assert (tmp_path / "store/policies/fake/upi/documents/first-doc-d1.md").exists()


class TestFailureHandling:
    def test_one_bad_document_does_not_stop_the_others(self, make_config):
        FakeConnector.reset({"upi": [("d1", "First Doc", "r1"), ("d2", "Second Doc", "r1")]})
        FakeConnector.fail_ids = {"d1"}
        report = ScrapePipeline(make_config()).run()

        assert counts(report) == {"failed": 1, "created": 1}
        assert report.ok is False, "a failure must be visible in the exit status"

    def test_a_failed_document_is_retried_on_the_next_run(self, make_config):
        """It must not be recorded as done, or a transient error would
        silently drop a document from the archive for good."""
        FakeConnector.reset({"upi": [("d1", "First Doc", "r1")]})
        FakeConnector.fail_ids = {"d1"}
        ScrapePipeline(make_config()).run()

        FakeConnector.fail_ids = set()
        report = ScrapePipeline(make_config()).run()
        assert counts(report) == {"created": 1}

    def test_an_unknown_source_name_is_rejected(self, make_config):
        FakeConnector.reset({"upi": []})
        with pytest.raises(Exception, match="Unknown source"):
            ScrapePipeline(make_config()).run(["nope"])


class TestManifestRoundTrip:
    def test_a_written_manifest_reloads_as_the_same_model(self, make_config, tmp_path):
        FakeConnector.reset({"upi": [("d1", "First Doc", "r1")]})
        ScrapePipeline(make_config()).run()

        raw = yaml.safe_load(read(tmp_path, "policies/fake/upi/_manifest.yaml"))
        manifest = CategoryManifest.model_validate(raw)

        entry = manifest.by_id()["d1"]
        assert entry.content_sha256.startswith("sha256:")
        assert entry.version == 1
        assert entry.storage_path == "policies/fake/upi/documents/first-doc-d1.md"
        assert entry.source_media_type is MediaType.HTML


class TestMetrics:
    """Runs record a metrics file; the suite must not write into the repo."""

    def events(self, tmp_path) -> list[dict]:
        path = tmp_path / "metrics" / "run.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_a_run_records_every_stage(self, make_config, tmp_path):
        FakeConnector.reset({"upi": [("d1", "First Doc", "r1")]})
        ScrapePipeline(make_config()).run()

        kinds = {e["event"] for e in self.events(tmp_path)}
        assert {"run_start", "document", "category", "run_end"} <= kinds

    def test_stage_timings_are_attached_to_the_document(self, make_config, tmp_path):
        FakeConnector.reset({"upi": [("d1", "First Doc", "r1")]})
        ScrapePipeline(make_config()).run()

        document = next(e for e in self.events(tmp_path) if e["event"] == "document")
        assert {"convert", "store"} <= set(document["stages"])

    def test_the_suite_never_writes_into_the_working_tree(self, make_config, tmp_path):
        """The default metrics path is relative, so a test that forgets to
        redirect it litters the real metrics/ directory -- which is exactly
        how 200+ stray files got there once."""
        before = sorted(Path("metrics").glob("*.jsonl")) if Path("metrics").is_dir() else []
        FakeConnector.reset({"upi": [("d1", "First Doc", "r1")]})
        ScrapePipeline(make_config()).run()
        after = sorted(Path("metrics").glob("*.jsonl")) if Path("metrics").is_dir() else []
        assert after == before

    def test_disabling_metrics_writes_nothing(self, make_config, tmp_path):
        config = make_config()
        config.run.metrics.enabled = False
        FakeConnector.reset({"upi": [("d1", "First Doc", "r1")]})
        ScrapePipeline(config).run()
        assert not (tmp_path / "metrics").exists()
