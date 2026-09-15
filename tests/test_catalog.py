"""Change detection, manifests and object keys.

This is where an incremental run gets its correctness from: the decision to
skip must be trustworthy, and the two update strategies must put documents
at the right keys.
"""

from __future__ import annotations

import datetime as _dt

import pytest

from policy_scraper.catalog.diff import decide
from policy_scraper.catalog.layout import StorageLayout
from policy_scraper.catalog.models import CatalogIndex, CategoryManifest, CategorySummary, ManifestEntry
from policy_scraper.core.models import (
    Category,
    ChangeType,
    DocumentRef,
    MediaType,
    UpdateStrategy,
)
from policy_scraper.utils.hashing import revision_key
from policy_scraper.utils.slug import slugify

CATEGORY = Category(
    key="commercial-banks",
    display_name="Commercial Banks",
    source_url="https://example.test/index",
)


def make_ref(document_id: str = "rbi-md-1", revision: str = "rev-a") -> DocumentRef:
    return DocumentRef(
        document_id=document_id,
        title="Know Your Customer Directions",
        category=CATEGORY,
        source_url="https://example.test/doc",
        revision_key=revision,
    )


def make_entry(document_id: str = "rbi-md-1", revision: str = "rev-a", version: int = 1):
    return ManifestEntry(
        document_id=document_id,
        title="Know Your Customer Directions",
        storage_path="policies/rbi/commercial-banks/documents/kyc-rbi-md-1.md",
        source_url="https://example.test/doc",
        origin_url="https://example.test/doc",
        revision_key=revision,
        content_sha256="sha256:abc",
        source_media_type=MediaType.HTML,
        converter="html_markdownify",
        version=version,
    )


def make_manifest(*entries: ManifestEntry) -> CategoryManifest:
    return CategoryManifest(
        source="rbi",
        category_key=CATEGORY.key,
        category_display_name=CATEGORY.display_name,
        documents=list(entries),
    )


class TestDecide:
    def test_unknown_document_is_created(self):
        decision = decide(
            make_ref(), make_manifest(), update_strategy=UpdateStrategy.REPLACE_IN_PLACE
        )
        assert decision.change is ChangeType.CREATED
        assert (decision.version, decision.path_version) == (1, 1)
        assert decision.needs_work

    def test_matching_revision_key_is_unchanged(self):
        decision = decide(
            make_ref(revision="rev-a"),
            make_manifest(make_entry(revision="rev-a")),
            update_strategy=UpdateStrategy.REPLACE_IN_PLACE,
        )
        assert decision.change is ChangeType.UNCHANGED
        assert not decision.needs_work

    def test_differing_revision_key_is_updated(self):
        decision = decide(
            make_ref(revision="rev-b"),
            make_manifest(make_entry(revision="rev-a")),
            update_strategy=UpdateStrategy.REPLACE_IN_PLACE,
        )
        assert decision.change is ChangeType.UPDATED
        assert decision.needs_work

    def test_force_overrides_a_matching_key(self):
        decision = decide(
            make_ref(revision="rev-a"),
            make_manifest(make_entry(revision="rev-a")),
            update_strategy=UpdateStrategy.REPLACE_IN_PLACE,
            force=True,
        )
        assert decision.change is ChangeType.UPDATED


class TestUpdateStrategies:
    """The version a manifest records and the version a *path* uses are
    deliberately different numbers under REPLACE_IN_PLACE."""

    def test_replace_in_place_pins_the_path_but_counts_the_version(self):
        decision = decide(
            make_ref(revision="rev-b"),
            make_manifest(make_entry(revision="rev-a", version=3)),
            update_strategy=UpdateStrategy.REPLACE_IN_PLACE,
        )
        assert decision.version == 4, "history should still be counted"
        assert decision.path_version == 1, "RBI must overwrite at a stable key"

    def test_new_version_advances_the_path(self):
        decision = decide(
            make_ref(revision="rev-b"),
            make_manifest(make_entry(revision="rev-a", version=3)),
            update_strategy=UpdateStrategy.NEW_VERSION,
        )
        assert decision.version == decision.path_version == 4

    def test_unchanged_keeps_the_existing_path_under_new_version(self):
        decision = decide(
            make_ref(revision="rev-a"),
            make_manifest(make_entry(revision="rev-a", version=3)),
            update_strategy=UpdateStrategy.NEW_VERSION,
        )
        assert decision.path_version == 3, "must not point at a key that was never written"


class TestStorageLayout:
    layout = StorageLayout(root_prefix="policies")

    def test_index_and_manifest_keys(self):
        assert self.layout.index_path() == "policies/_index.yaml"
        assert self.layout.manifest_path("rbi", "commercial-banks") == (
            "policies/rbi/commercial-banks/_manifest.yaml"
        )

    def test_version_one_has_no_suffix(self):
        assert self.layout.document_path("rbi", "commercial-banks", "kyc-rbi-md-1", version=1) == (
            "policies/rbi/commercial-banks/documents/kyc-rbi-md-1.md"
        )

    def test_later_versions_are_suffixed(self):
        assert self.layout.document_path("npci", "upi", "oc-186", version=3).endswith(
            "/documents/oc-186.v3.md"
        )

    def test_root_prefix_is_honoured(self):
        assert StorageLayout(root_prefix="archive").index_path() == "archive/_index.yaml"


class TestManifest:
    def test_upsert_replaces_by_id_and_keeps_first_seen(self):
        first_seen = _dt.datetime(2024, 1, 1, tzinfo=_dt.timezone.utc)
        original = make_entry(revision="rev-a")
        original.first_seen_at = first_seen
        manifest = make_manifest(original)

        manifest.upsert(make_entry(revision="rev-b", version=2))

        assert len(manifest.documents) == 1
        assert manifest.documents[0].revision_key == "rev-b"
        assert manifest.documents[0].first_seen_at == first_seen

    def test_upsert_appends_a_new_id(self):
        manifest = make_manifest(make_entry("rbi-md-1"))
        manifest.upsert(make_entry("rbi-md-2"))
        assert sorted(manifest.by_id()) == ["rbi-md-1", "rbi-md-2"]

    def test_finalise_refreshes_the_count_and_sorts_newest_first(self):
        older, newer = make_entry("a"), make_entry("b")
        older.published_at = _dt.date(2020, 1, 1)
        newer.published_at = _dt.date(2025, 1, 1)
        manifest = make_manifest(older, newer).finalise()

        assert manifest.document_count == 2
        assert manifest.documents[0].document_id == "b"

    def test_entries_with_no_published_date_do_not_break_sorting(self):
        dated, undated = make_entry("a"), make_entry("b")
        dated.published_at = _dt.date(2025, 1, 1)
        assert make_manifest(undated, dated).finalise().documents[0].document_id == "a"


class TestCatalogIndex:
    def _summary(self, source: str, key: str, count: int = 1) -> CategorySummary:
        return CategorySummary(
            source=source,
            category_key=key,
            category_display_name=key.upper(),
            manifest_path=f"policies/{source}/{key}/_manifest.yaml",
            document_count=count,
        )

    def test_upsert_is_keyed_on_source_and_category(self):
        index = CatalogIndex()
        index.upsert(self._summary("rbi", "upi", count=1))
        index.upsert(self._summary("npci", "upi", count=5))
        index.upsert(self._summary("rbi", "upi", count=9))

        assert len(index.categories) == 2
        counts = {(c.source, c.category_key): c.document_count for c in index.categories}
        assert counts == {("rbi", "upi"): 9, ("npci", "upi"): 5}

    def test_finalise_sorts_by_source_then_category(self):
        index = CatalogIndex()
        index.upsert(self._summary("rbi", "commercial-banks"))
        index.upsert(self._summary("npci", "upi"))
        index.upsert(self._summary("npci", "imps"))
        index.finalise()

        assert [(c.source, c.category_key) for c in index.categories] == [
            ("npci", "imps"),
            ("npci", "upi"),
            ("rbi", "commercial-banks"),
        ]


class TestFingerprints:
    def test_revision_key_is_stable(self):
        assert revision_key("t", "u", None) == revision_key("t", "u", None)

    def test_any_changed_part_changes_the_key(self):
        assert revision_key("title", "url") != revision_key("title", "url2")

    def test_none_is_distinct_from_empty_string(self):
        """A signal disappearing must register as a change, not be dropped."""
        assert revision_key("t", None) != revision_key("t", "")

    def test_parts_cannot_be_confused_by_concatenation(self):
        assert revision_key("ab", "c") != revision_key("a", "bc")


class TestSlugify:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("Commercial Banks", "commercial-banks"),
            ("Banker & Debt Manager", "banker-debt-manager"),
            ("  Spaced  Out  ", "spaced-out"),
            ("UPI | OC No. 186A", "upi-oc-no-186a"),
        ],
    )
    def test_common_titles(self, value: str, expected: str):
        assert slugify(value) == expected

    def test_never_returns_an_empty_slug(self):
        assert slugify("!!!") == "untitled"

    def test_truncates_on_a_word_boundary(self):
        slug = slugify("alpha beta gamma delta epsilon", max_length=14)
        assert len(slug) <= 14
        assert not slug.endswith("-")
        assert slug == "alpha-beta"
