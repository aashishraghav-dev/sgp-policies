"""Connector parsing.

These pin the site-shape assumptions that would otherwise only fail against
the live site: RBI's category-vs-date headers sharing a CSS class, and
NPCI's habit of listing circulars with no file attached.
"""

from __future__ import annotations

import datetime as _dt

import pytest

from policy_scraper.config.models import SourceConfig
from policy_scraper.core.errors import PermanentError
from policy_scraper.core.models import Category
from policy_scraper.sources.npci.circulars import NpciCircularsConnector
from policy_scraper.sources.rbi.master_directions import RbiMasterDirectionsConnector

from .conftest import RBI_INDEX_HTML, StubFetcher

BASE = "https://www.rbi.org.in/Scripts/BS_ViewMasterDirections.aspx"


@pytest.fixture
def rbi(rbi_config: SourceConfig, stub_fetcher: StubFetcher) -> RbiMasterDirectionsConnector:
    return RbiMasterDirectionsConnector(rbi_config, stub_fetcher)


class TestRbiIndexParsing:
    def test_categories_are_discovered_not_hardcoded(self, rbi):
        categories, _ = rbi._parse_index(RBI_INDEX_HTML, BASE)
        assert [c.key for c in categories] == [
            "commercial-banks",
            "consumer-education-and-protection",
        ]

    def test_a_repeated_category_heading_merges_into_one(self, rbi):
        """RBI repeats headings down the page; the same category must not
        become two storage folders."""
        categories, documents = rbi._parse_index(RBI_INDEX_HTML, BASE)
        assert len(categories) == 2
        assert len(documents["commercial-banks"]) == 2

    def test_date_headers_are_not_mistaken_for_categories(self, rbi):
        """Both use ``class="tableheader"``; only a parse of the text tells
        them apart."""
        categories, documents = rbi._parse_index(RBI_INDEX_HTML, BASE)
        assert "sep-12-2025" not in {c.key for c in categories}
        assert documents["commercial-banks"][0].published_at == _dt.date(2025, 9, 12)

    def test_document_fields_are_extracted(self, rbi):
        _, documents = rbi._parse_index(RBI_INDEX_HTML, BASE)
        doc = documents["commercial-banks"][0]

        assert doc.document_id == "rbi-md-13141"
        assert doc.extra["rbi_id"] == "13141"
        assert doc.extra["pdf_url"].endswith("61MD0825F72.PDF")
        assert doc.extra["pdf_size_label"] == "256 kb"
        assert doc.source_url.endswith("BS_ViewMasDirections.aspx?id=13141")

    def test_updated_as_on_is_parsed_from_the_title(self, rbi):
        _, documents = rbi._parse_index(RBI_INDEX_HTML, BASE)
        assert documents["commercial-banks"][0].source_updated_at == _dt.date(2018, 11, 22)

    def test_rows_without_a_document_link_are_skipped(self, rbi):
        _, documents = rbi._parse_index(RBI_INDEX_HTML, BASE)
        assert len(documents["consumer-education-and-protection"]) == 1

    def test_republishing_the_pdf_changes_the_revision_key(self, rbi):
        """The PDF filename embeds a content hash, which is the whole basis
        of diffing without downloading."""
        _, before = rbi._parse_index(RBI_INDEX_HTML, BASE)
        _, after = rbi._parse_index(RBI_INDEX_HTML.replace("61MD0825F72", "99MDZZZZZZZ"), BASE)

        assert (
            before["commercial-banks"][0].revision_key
            != after["commercial-banks"][0].revision_key
        )

    def test_an_unchanged_page_yields_identical_keys(self, rbi):
        _, first = rbi._parse_index(RBI_INDEX_HTML, BASE)
        _, second = rbi._parse_index(RBI_INDEX_HTML, BASE)
        assert [d.revision_key for d in first["commercial-banks"]] == [
            d.revision_key for d in second["commercial-banks"]
        ]


NPCI_CATEGORY = Category(
    key="upi", display_name="UPI", source_url="https://www.npci.org.in/circulars/upi"
)


def npci_connector() -> NpciCircularsConnector:
    """Build the connector with its browser-fetcher guard bypassed.

    The guard is tested separately; these tests exercise pure parsing and
    must not need a real browser.
    """
    config = SourceConfig(name="npci", type="npci.circulars", fetcher="browser")
    connector = NpciCircularsConnector.__new__(NpciCircularsConnector)
    connector.config = config
    connector.fetcher = None
    connector.options = NpciCircularsConnector.options_model()
    connector._bootstrapped = set()
    return connector


class TestNpciGuard:
    def test_a_non_browser_fetcher_is_rejected_up_front(self, stub_fetcher):
        """npci.org.in 403s every non-browser client, so this should fail at
        construction rather than as a wall of fetch errors mid-run."""
        config = SourceConfig(name="npci", type="npci.circulars", fetcher="http")
        with pytest.raises(PermanentError, match="browser fetcher"):
            NpciCircularsConnector(config, stub_fetcher)


class TestNpciRefBuilding:
    def _item(self, **overrides):
        item = {
            "id": 3963,
            "fileName": "UPI | OC No. 186A | Enhancement",
            "mediaType": "pdf",
            "yearLabel": "FY 26-27",
            "media": {"url": "/uploads/UPI_OC_186A_e695625b85.pdf"},
        }
        item.update(overrides)
        return item

    def test_a_well_formed_item_becomes_a_ref(self):
        ref = npci_connector()._to_ref(self._item(), NPCI_CATEGORY, 2026)
        assert ref is not None
        assert ref.document_id == "npci-upi-3963"
        assert ref.extra["pdf_url"] == (
            "https://www.npci.org.in/uploads/UPI_OC_186A_e695625b85.pdf"
        )
        assert ref.extra["instrument_type"] == "circular"

    @pytest.mark.parametrize(
        "overrides",
        [
            {"media": {}},
            {"media": None},
            {"id": None},
            {"fileName": ""},
            {"mediaType": "video"},
        ],
        ids=["no-url", "null-media", "no-id", "no-title", "not-a-pdf"],
    )
    def test_unusable_items_are_skipped(self, overrides):
        """NPCI's CMS routinely lists circulars with no file attached; these
        are the observed shapes."""
        assert npci_connector()._to_ref(self._item(**overrides), NPCI_CATEGORY, 2026) is None

    def test_reupload_changes_the_revision_key(self):
        """The upload path carries a CMS content hash, so a re-upload is
        detectable without downloading the PDF."""
        connector = npci_connector()
        original = connector._to_ref(self._item(), NPCI_CATEGORY, 2026)
        reuploaded = connector._to_ref(
            self._item(media={"url": "/uploads/UPI_OC_186A_ffffffffff.pdf"}),
            NPCI_CATEGORY,
            2026,
        )
        assert original.revision_key != reuploaded.revision_key


class TestNpciDates:
    def test_an_explicit_timestamp_wins(self):
        ref = npci_connector()._to_ref(
            {
                "id": 1,
                "fileName": "Circular",
                "mediaType": "pdf",
                "media": {"url": "/uploads/a.pdf"},
                "publishedAt": "2026-09-10T00:00:00.000Z",
            },
            NPCI_CATEGORY,
            2026,
        )
        assert ref.published_at == _dt.date(2026, 9, 10)

    def test_a_financial_year_label_falls_back_to_1_april(self):
        """The API exposes no per-circular date, only "FY 26-27"; the Indian
        financial year starts in April, so documents still sort sensibly."""
        assert NpciCircularsConnector._published_date({}, "FY 26-27", 2026) == _dt.date(2026, 4, 1)

    def test_a_four_digit_financial_year_is_handled(self):
        assert NpciCircularsConnector._published_date({}, "FY 2025-26", 2025) == _dt.date(
            2025, 4, 1
        )

    def test_an_unparseable_label_falls_back_to_the_calendar_year(self):
        assert NpciCircularsConnector._published_date({}, "unknown", 2024) == _dt.date(2024, 1, 1)


class TestNpciProductDiscovery:
    def test_products_are_walked_out_of_nested_page_metadata(self):
        """The CMS nests the product switcher differently per page, so the
        payload is walked rather than indexed at a fixed path."""
        payload = {
            "data": {
                "blocks": [
                    {
                        "items": [
                            {"link": "/circulars/imps", "logo": {"productName": "IMPS"}},
                            {"link": "/circulars/aeps", "logo": {"productName": "AePS"}},
                        ]
                    },
                    {"link": "/about-us", "logo": {"productName": "Nope"}},
                ]
            }
        }
        products = npci_connector()._extract_products(payload)
        assert products == {"imps": "IMPS", "aeps": "AePS"}

    def test_a_product_without_a_name_falls_back_to_its_key(self):
        products = npci_connector()._extract_products({"link": "/circulars/nach"})
        assert products == {"nach": "nach"}

    def test_unrecognised_metadata_yields_nothing_rather_than_raising(self):
        assert npci_connector()._extract_products({"unexpected": ["shape"]}) == {}
