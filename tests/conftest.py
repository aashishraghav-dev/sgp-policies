"""Shared fixtures.

Every test here is offline. Connectors take a ``Fetcher``, so a stub that
serves canned responses is all that is needed to exercise the parsing and
diffing logic without touching rbi.org.in or npci.org.in.
"""

from __future__ import annotations

import pytest

from policy_scraper.config.models import SourceConfig
from policy_scraper.fetch.base import FetchResponse, Fetcher


class StubFetcher(Fetcher):
    """Serves responses from a ``{url: body}`` mapping and records calls."""

    def __init__(self, responses: dict[str, bytes | str] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[str] = []

    def _fetch(self, url: str, *, headers: dict[str, str] | None = None) -> FetchResponse:
        self.calls.append(url)
        body = self.responses.get(url, "")
        data = body.encode("utf-8") if isinstance(body, str) else body
        return FetchResponse(
            url=url,
            status=200,
            content=data,
            headers={"content-type": "text/html; charset=utf-8"},
        )


@pytest.fixture
def stub_fetcher() -> StubFetcher:
    return StubFetcher()


@pytest.fixture
def rbi_config() -> SourceConfig:
    return SourceConfig(
        name="rbi",
        type="rbi.master_directions",
        options={"index_url": "https://www.rbi.org.in/Scripts/BS_ViewMasterDirections.aspx"},
    )


# A faithful reduction of the real index page: category headers and date
# sub-headers share ``class="tableheader"``, which is the thing that makes
# this parse non-trivial.
RBI_INDEX_HTML = """
<html><body><table>
  <tr><td class="tableheader"><b>Commercial Banks</b></td></tr>
  <tr><td class="tableheader"><b>Sep 12, 2025</b></td></tr>
  <tr>
    <td><a class="link2" href="BS_ViewMasDirections.aspx?id=13141">
        Know Your Customer Directions, 2025 (Updated as on November 22, 2018)</a></td>
    <td><a href="https://rbidocs.rbi.org.in/rdocs/notification/PDFs/61MD0825F72.PDF">pdf</a>
        <span id="SPDF_13141">256 kb</span></td>
  </tr>
  <tr><td class="tableheader"><b>Consumer Education and Protection</b></td></tr>
  <tr><td class="tableheader"><b>Jan 05, 2024</b></td></tr>
  <tr>
    <td><a class="link2" href="BS_ViewMasDirections.aspx?id=12000">Ombudsman Scheme</a></td>
  </tr>
  <tr><td>a row with no link at all</td></tr>
  <tr><td class="tableheader"><b>Commercial Banks</b></td></tr>
  <tr>
    <td><a class="link2" href="BS_ViewMasDirections.aspx?id=13400">Asset Classification</a></td>
  </tr>
</table></body></html>
"""
