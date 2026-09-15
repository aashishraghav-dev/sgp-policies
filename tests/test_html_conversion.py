"""HTML -> markdown, and the layout-table problem in particular.

RBI nests whole documents inside positioning tables. Getting this wrong is
silent: the text is all still there, but every paragraph boundary is gone
and the document arrives as one giant table cell. These tests pin the
distinction between a layout table and a real one.
"""

from __future__ import annotations

from bs4 import BeautifulSoup

from policy_scraper.config.models import HtmlConversionConfig
from policy_scraper.convert.html import (
    HtmlToMarkdownConverter,
    _is_layout_table,
    extract_first_match,
    normalise_markdown,
    unwrap_layout_tables,
)
from policy_scraper.core.models import ContentPayload, MediaType


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def _table(html: str):
    return _soup(html).find("table")


class TestLayoutTableDetection:
    def test_table_of_paragraphs_is_layout(self):
        html = """
        <table><tr><td><p>First paragraph.</p><p>Second paragraph.</p></td></tr>
        <tr><td><p>Third.</p></td></tr></table>
        """
        assert _is_layout_table(_table(html)) is True

    def test_table_of_inline_cells_is_data(self):
        html = """
        <table>
          <tr><th>Instrument</th><th>Limit</th></tr>
          <tr><td>UPI</td><td>5000</td></tr>
        </table>
        """
        assert _is_layout_table(_table(html)) is False

    def test_single_cell_table_is_layout(self):
        assert _is_layout_table(_table("<table><tr><td>alone</td></tr></table>")) is True

    def test_empty_table_is_layout(self):
        assert _is_layout_table(_table("<table></table>")) is True


class TestUnwrapping:
    def test_layout_wrapper_becomes_divs(self):
        soup = _soup("<table><tr><td><p>A</p></td></tr><tr><td><p>B</p></td></tr></table>")
        assert unwrap_layout_tables(soup) == 1
        assert soup.find("table") is None
        assert soup.find("tr") is None

    def test_nested_data_table_survives_its_layout_wrapper(self):
        """The regression that mattered: flattening the wrapper must not
        take a genuine index table down with it."""
        html = """
        <table>
          <tr><td>
            <p>Preamble text.</p>
            <table>
              <tr><th>Chapter</th><th>Page</th></tr>
              <tr><td>Scope</td><td>1</td></tr>
              <tr><td>Definitions</td><td>2</td></tr>
            </table>
          </td></tr>
        </table>
        """
        soup = _soup(html)
        unwrap_layout_tables(soup)

        remaining = soup.find_all("table")
        assert len(remaining) == 1, "the inner data table should still be a table"
        assert "Chapter" in remaining[0].get_text()

    def test_orphan_rows_are_normalised(self):
        """RBI's content selector is a ``tr``, so the extracted fragment
        re-parses as rows with no enclosing table. Left alone, markdownify
        renders them as a spurious one-cell table."""
        soup = _soup("<tr class='tablecontent2'><td><p>Body text.</p></td></tr>")
        unwrap_layout_tables(soup)
        assert soup.find("tr") is None
        assert soup.find("td") is None


class TestConverter:
    def _convert(self, html: str) -> str:
        converter = HtmlToMarkdownConverter(HtmlConversionConfig())
        payload = ContentPayload(
            data=html.encode("utf-8"),
            media_type=MediaType.HTML,
            origin_url="https://example.test/doc",
        )
        return converter.convert(payload).markdown

    def test_layout_table_yields_paragraphs_not_a_table_row(self):
        html = """
        <table><tr><td>
          <p>Paragraph one.</p>
          <p>Paragraph two.</p>
        </td></tr></table>
        """
        markdown = self._convert(html)
        assert "|" not in markdown
        assert "Paragraph one." in markdown
        assert "Paragraph two." in markdown

    def test_real_table_is_rendered_as_gfm(self):
        html = """
        <table>
          <tr><th>Chapter</th><th>Page</th></tr>
          <tr><td>Scope</td><td>1</td></tr>
        </table>
        """
        assert "| Chapter | Page |" in self._convert(html)

    def test_stripped_tags_do_not_reach_the_output(self):
        html = "<div><script>var x = 1;</script><p>Kept.</p><style>p{}</style></div>"
        markdown = self._convert(html)
        assert "Kept." in markdown
        assert "var x" not in markdown


class TestNormalisation:
    def test_entities_are_decoded(self):
        """docling escapes ``&`` and ``<`` on export; an agent should read
        prose, not entities."""
        assert normalise_markdown("Tap &amp; Pay under &lt;5000") == "Tap & Pay under <5000\n"

    def test_invisible_characters_are_translated(self):
        assert normalise_markdown("a\xa0b") == "a b\n"

    def test_blank_line_runs_collapse(self):
        assert normalise_markdown("a\n\n\n\n\nb") == "a\n\nb\n"

    def test_trailing_whitespace_is_stripped(self):
        assert normalise_markdown("line   \nnext\t\n") == "line\nnext\n"

    def test_normalisation_is_idempotent(self):
        """It feeds content_sha256, so a second pass must not shift the hash."""
        once = normalise_markdown("Tap &amp; Pay\xa0here\n\n\n\ndone   ")
        assert normalise_markdown(once) == once


class TestSelectors:
    def test_first_matching_selector_wins(self):
        html = "<div><tr class='tablecontent2'>row</tr><div id='annual'>annual</div></div>"
        found = extract_first_match(html, ["tr.tablecontent2", "#annual"])
        assert found is not None and "row" in found.get_text()

    def test_falls_through_to_later_selectors(self):
        found = extract_first_match("<div id='annual'>annual</div>", ["tr.missing", "#annual"])
        assert found is not None and found.get("id") == "annual"

    def test_returns_none_when_nothing_matches(self):
        assert extract_first_match("<p>x</p>", ["#nope"]) is None
