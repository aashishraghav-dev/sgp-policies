"""Tests for post-OCR currency repair.

The literals here are not invented. Every ``original`` string is a real
observed output from ``tools/ocr_benchmark.py`` running against the two
NPCI circulars in ``tools/samples/``, so a regression in this module shows
up as a test failure rather than as a wrong number in a stored policy.
"""

from __future__ import annotations

import pytest

from policy_scraper.convert.ocr_repair import Confidence, repair_currency


class TestUnambiguousRepairs:
    """Glyphs with no valid reading before a number are simply wrong."""

    @pytest.mark.parametrize(
        "text, expected",
        [
            ("enhanced from %5,000 to", "enhanced from ₹5,000 to"),  # tesseract
            ("capped at °5000 per", "capped at ₹5000 per"),
            ("limit of ~10,000 applies", "limit of ₹10,000 applies"),
        ],
    )
    def test_impossible_prefixes_become_rupees(self, text: str, expected: str) -> None:
        assert repair_currency(text).markdown == expected

    def test_a_repair_is_reported_as_repaired(self) -> None:
        report = repair_currency("enhanced from %5,000 to")
        assert report.repaired == 1
        assert report.substituted == 0
        assert report.findings[0].confidence is Confidence.REPAIRED
        assert report.findings[0].original == "%5,000"


class TestAuditedSubstitutions:
    """Real currency signs are rewritten, but never silently."""

    @pytest.mark.parametrize(
        "original",
        ["¥5000", "$5000", "<5,000", "£5000"],  # all observed, except £
    )
    def test_plausible_prefixes_are_substituted(self, original: str) -> None:
        report = repair_currency(f"limit is capped at {original}.")
        assert "₹" in report.markdown
        assert report.substituted == 1
        assert report.findings[0].confidence is Confidence.SUBSTITUTED

    def test_the_original_is_preserved_so_the_rewrite_is_reversible(self) -> None:
        report = repair_currency("ceiling of $5000.")
        finding = report.findings[0]
        assert finding.original == "$5000"
        assert finding.replacement == "₹5000"
        assert "ceiling of" in finding.context

    def test_substitution_is_distinguished_from_repair_in_the_summary(self) -> None:
        report = repair_currency("from %5,000 to $10,000")
        summary = report.summary()
        assert summary["currency_repaired"] == 1
        assert summary["currency_substituted"] == 1


class TestUnverifiableAmounts:
    """The dangerous cases: nothing in the text reveals the error."""

    def test_a_dropped_symbol_near_money_words_is_flagged(self) -> None:
        # rapidocr's real output: it discards the glyph entirely.
        report = repair_currency("per transaction limit is capped at 5000, up to")
        assert report.unverifiable == 1
        assert report.findings[0].confidence is Confidence.UNVERIFIABLE

    def test_a_symbol_absorbed_into_the_digits_is_flagged_not_corrected(self) -> None:
        # tesseract read a ground-truth "₹5,000" as "25,000". The amount
        # cannot be recovered, so the only honest move is to say so.
        report = repair_currency("hereby enhanced from 25,000 to 10,000, effective")
        assert report.unverifiable == 2
        assert "25,000" in report.markdown  # emphatically NOT rewritten
        assert all(f.replacement is None for f in report.findings)

    def test_nothing_is_flagged_when_the_rupee_sign_survived(self) -> None:
        assert repair_currency("limit is capped at ₹5,000 per").findings == []

    @pytest.mark.parametrize("marker", ["Rs. 5000", "Rs 5000", "INR 5000"])
    def test_spelled_out_currency_counts_as_a_marker(self, marker: str) -> None:
        # Letters survive OCR far better than symbols, so these are trusted.
        assert repair_currency(f"the limit is {marker} per day").findings == []


class TestPrecision:
    """A report swamped by false positives protects nobody."""

    def test_years_are_not_reported_as_lost_amounts(self) -> None:
        # Phrased without a trailing period on purpose: an earlier version
        # of this test passed only because the period blocked the match,
        # which hid the fact that sentence-final amounts were never seen.
        assert repair_currency("the limit was revised in 2023 by circular").unverifiable == 0

    def test_an_amount_ending_a_sentence_is_still_seen(self) -> None:
        # rapidocr's real second miss in oc-186a, which the regex used to
        # skip because the amount was followed by a full stop.
        report = repair_currency("within the aforesaid ceiling of 5000.")
        assert report.unverifiable == 1

    def test_a_decimal_amount_is_matched_whole(self) -> None:
        report = repair_currency("a fee of %1,250.50 applies")
        assert report.markdown == "a fee of ₹1,250.50 applies"

    def test_circular_numbers_are_not_reported(self) -> None:
        text = "reference to circular NPCI/UPI/OC-186/2023-24 which revised the limit"
        assert repair_currency(text).unverifiable == 0

    def test_numbers_outside_a_money_context_are_ignored(self) -> None:
        assert repair_currency("approximately 4500 members participated.").findings == []

    def test_a_percentage_after_a_number_is_left_alone(self) -> None:
        # '%' only means a lost rupee sign when it *precedes* the digits.
        assert repair_currency("a fee of 2,500 basis points").markdown == (
            "a fee of 2,500 basis points"
        )

    def test_two_digit_numbers_are_not_treated_as_amounts(self) -> None:
        assert repair_currency("the limit in clause 12 applies").findings == []


class TestReportShape:
    def test_a_clean_document_produces_no_front_matter_noise(self) -> None:
        assert repair_currency("No amounts appear in this circular.").summary() == {}

    def test_findings_are_capped_so_they_cannot_bury_the_document(self) -> None:
        text = " ".join(f"the fee is %{n},000 and" for n in range(1, 60))
        summary = repair_currency(text).summary()
        assert summary["currency_repaired"] > 25  # all of them are counted
        assert len(summary["findings"]) == 25  # but not all are listed

    def test_markdown_is_returned_unchanged_when_there_is_nothing_to_fix(self) -> None:
        text = "A circular with prose and no figures at all.\n"
        assert repair_currency(text).markdown == text


class TestBothBackendsRepair:
    """A config-only switch to a docling *service* must not silently drop
    the accuracy guarantee, so the repair is wired via the router into
    whichever backend is selected."""

    def _converter(self, backend: str):
        from policy_scraper.config.models import ConversionConfig
        from policy_scraper.convert.router import build_pdf_converter

        config = ConversionConfig()
        config.pdf.backend = backend  # type: ignore[assignment]
        return build_pdf_converter(config)

    @pytest.mark.parametrize("backend", ["docling_local", "docling_remote"])
    def test_repair_is_enabled_on_both_backends_by_default(self, backend: str) -> None:
        assert self._converter(backend)._repair_currency is True

    @pytest.mark.parametrize("backend", ["docling_local", "docling_remote"])
    def test_disabling_it_reaches_both_backends(self, backend: str) -> None:
        from policy_scraper.config.models import ConversionConfig
        from policy_scraper.convert.router import build_pdf_converter

        config = ConversionConfig()
        config.pdf.backend = backend  # type: ignore[assignment]
        config.pdf.repair_currency = False
        assert build_pdf_converter(config)._repair_currency is False


class TestApplyRepair:
    def test_it_is_a_no_op_when_disabled(self) -> None:
        from policy_scraper.convert.ocr_repair import apply_repair

        markdown, findings = apply_repair("capped at %5,000 per", enabled=False)
        assert markdown == "capped at %5,000 per"  # untouched
        assert findings == {}

    def test_it_returns_both_the_text_and_the_front_matter_block(self) -> None:
        from policy_scraper.convert.ocr_repair import apply_repair

        markdown, findings = apply_repair("capped at %5,000 per", enabled=True)
        assert markdown == "capped at ₹5,000 per"
        assert findings["currency_repaired"] == 1
