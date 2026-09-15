"""HTML -> markdown conversion.

Preferred over the PDF path whenever a source publishes the full document
as markup: it is lossless, needs no OCR, and is orders of magnitude
cheaper. RBI Master Direction detail pages carry the complete text, so
this handles the bulk of RBI documents.
"""

from __future__ import annotations

import html as _html
import re
import time

from bs4 import BeautifulSoup, Tag
from markdownify import MarkdownConverter

from policy_scraper.config.models import HtmlConversionConfig
from policy_scraper.convert.base import DocumentConverter, converter_registry
from policy_scraper.core.errors import ConversionError
from policy_scraper.core.models import ContentPayload, ConversionResult, MediaType
from policy_scraper.utils.logging import get_logger

logger = get_logger(__name__)

_BLANK_LINES = re.compile(r"\n{3,}")
_TRAILING_WS = re.compile(r"[ \t]+$", re.MULTILINE)
# Non-breaking and zero-width characters are rife in RBI markup and make
# downstream diffing noisy.
_INVISIBLES = str.maketrans({"\xa0": " ", "​": "", "‌": "", "‍": "", "﻿": ""})

# Elements that only ever appear in prose, never inside a genuine data cell.
# Their presence is what distinguishes a layout table from a real one.
_BLOCK_TAGS = frozenset(
    {"p", "div", "table", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "blockquote", "pre", "hr"}
)
_TABLE_TAGS = ("table", "thead", "tbody", "tfoot", "tr", "td", "th", "caption", "colgroup", "col")


def _own_cells(table: Tag) -> list[Tag]:
    """Cells belonging to ``table`` itself, not to tables nested inside it."""
    return [c for c in table.find_all(["td", "th"]) if c.find_parent("table") is table]


def _own_rows(table: Tag) -> list[Tag]:
    return [r for r in table.find_all("tr") if r.find_parent("table") is table]


def _is_layout_table(table: Tag) -> bool:
    """Decide whether a table carries data or is just page furniture.

    RBI wraps entire Master Directions in several levels of positioning
    tables. Converted naively, a whole document collapses into one giant
    GFM table cell and every paragraph boundary is lost.

    The discriminator: a genuine data table holds inline content in its
    cells. Once a cell contains a paragraph, list or nested table, the
    table is being used for layout.
    """
    cells = _own_cells(table)
    if not cells:
        return True
    if len(_own_rows(table)) <= 1 or len(cells) <= 1:
        return True
    return any(cell.find(list(_BLOCK_TAGS)) is not None for cell in cells)


def _own_structure(table: Tag) -> list[Tag]:
    """``table``'s own structural tags, excluding those of nested tables."""
    return [
        element
        for element in table.find_all(_TABLE_TAGS[1:])
        if element.find_parent("table") is table
    ]


def unwrap_layout_tables(soup: BeautifulSoup) -> int:
    """Rewrite layout tables as plain divs so prose structure survives.

    Cells become divs rather than being dissolved, which preserves the
    block boundaries markdownify needs to emit real paragraphs.

    Only a table's *own* rows and cells are rewritten. A genuine data table
    nested inside a layout wrapper is left intact and judged on its own
    merits -- RBI routinely puts real tables (indexes, rate schedules)
    inside positioning tables, and flattening those would lose them.

    Returns the number of tables unwrapped, for logging.
    """
    unwrapped = 0

    # Document order visits outer tables first. Structural tags are
    # collected before the table itself is renamed, so nested tables are
    # still reachable and get evaluated independently later in the loop.
    for table in soup.find_all("table"):
        if not _is_layout_table(table):
            continue
        structure = _own_structure(table)
        table.name = "div"
        for element in structure:
            if element.name in ("colgroup", "col"):
                element.decompose()
            else:
                element.name = "div"
        unwrapped += 1

    # A content region selected mid-table (RBI's body lives in a
    # `tr.tablecontent2`) re-parses as orphan rows with no enclosing table.
    # markdownify would render those as a spurious one-cell table.
    for orphan in soup.find_all(_TABLE_TAGS[1:]):
        if orphan.find_parent("table") is None:
            orphan.name = "div"

    return unwrapped


class _Converter(MarkdownConverter):
    """markdownify subclass tolerant of the markup regulators publish."""

    def convert_table(self, el: Tag, text: str, parent_tags: object = None) -> str:
        # Any table reaching this point was classified as a data table, but
        # markdownify still emits GFM only for well-formed markup. Fall back
        # to the inner text rather than writing a broken table.
        rendered = super().convert_table(el, text, parent_tags)
        return rendered if "|" in rendered else f"\n\n{text.strip()}\n\n"


@converter_registry.register("html")
class HtmlToMarkdownConverter(DocumentConverter):
    """Strips chrome from a page and renders the remainder as markdown."""

    name = "html_markdownify"
    supported_media_types = frozenset({MediaType.HTML})

    def __init__(self, config: HtmlConversionConfig) -> None:
        self._config = config

    def convert(self, payload: ContentPayload, *, title: str | None = None) -> ConversionResult:
        started = time.perf_counter()
        try:
            markdown = self._render(payload.as_text())
        except ConversionError:
            raise
        except Exception as exc:
            raise ConversionError(f"HTML conversion failed for {payload.origin_url}: {exc}") from exc

        if not markdown.strip():
            raise ConversionError(f"HTML conversion produced no text for {payload.origin_url}.")

        return ConversionResult(
            markdown=markdown,
            converter=self.name,
            source_media_type=MediaType.HTML,
            duration_seconds=round(time.perf_counter() - started, 3),
        )

    def _render(self, html: str) -> str:
        soup = BeautifulSoup(html, "lxml")

        for tag_name in self._config.strip_tags:
            for element in soup.find_all(tag_name):
                element.decompose()
        for selector in self._config.strip_selectors:
            for element in soup.select(selector):
                element.decompose()

        if self._config.unwrap_layout_tables:
            count = unwrap_layout_tables(soup)
            if count:
                logger.debug("Unwrapped %d layout tables", count)

        markdown = _Converter(
            heading_style=self._config.heading_style,
            bullets=self._config.bullet_chars,
            wrap=self._config.wrap_width > 0,
            wrap_width=self._config.wrap_width or 80,
            escape_asterisks=False,
            escape_underscores=False,
        ).convert_soup(soup)

        return normalise_markdown(markdown)


def normalise_markdown(markdown: str) -> str:
    """Collapse whitespace noise so unchanged documents hash identically.

    Also decodes HTML entities. docling escapes ``&`` and ``<`` when
    exporting markdown, which leaves ``&amp;`` and ``&lt;`` sitting in the
    prose an agent will later read.
    """
    cleaned = _html.unescape(markdown)
    cleaned = cleaned.translate(_INVISIBLES)
    cleaned = _TRAILING_WS.sub("", cleaned)
    cleaned = _BLANK_LINES.sub("\n\n", cleaned)
    return cleaned.strip() + "\n"


def extract_first_match(html: str, selectors: list[str]) -> Tag | None:
    """Return the first element matching any selector, in priority order.

    Connectors use this to isolate a page's content region before handing
    it to the converter, keeping site-specific selectors in the connector
    rather than in the converter.
    """
    soup = BeautifulSoup(html, "lxml")
    for selector in selectors:
        element = soup.select_one(selector)
        if element is not None:
            return element
    return None
