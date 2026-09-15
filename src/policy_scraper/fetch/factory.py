"""Fetcher construction from config."""

from __future__ import annotations

from policy_scraper.config.models import FetchingConfig
from policy_scraper.core.errors import ConfigurationError
from policy_scraper.core.metrics import NULL_RECORDER, MetricsRecorder
from policy_scraper.fetch.base import Fetcher
from policy_scraper.fetch.browser import BrowserFetcher
from policy_scraper.fetch.http import HttpFetcher


def build_fetcher(kind: str, config: FetchingConfig) -> Fetcher:
    if kind == "http":
        return HttpFetcher(config.http)
    if kind == "browser":
        return BrowserFetcher(config.browser)
    raise ConfigurationError(f"Unknown fetcher {kind!r}; expected 'http' or 'browser'.")


class FetcherPool:
    """Lazily creates and shares one fetcher instance per kind.

    Sources that use the same transport share it, so a run with several
    browser-based sources still starts only one Chromium process.
    """

    def __init__(
        self, config: FetchingConfig, *, recorder: MetricsRecorder = NULL_RECORDER
    ) -> None:
        self._config = config
        self._recorder = recorder
        self._fetchers: dict[str, Fetcher] = {}

    def get(self, kind: str) -> Fetcher:
        if kind not in self._fetchers:
            fetcher = build_fetcher(kind, self._config)
            # Injected here rather than in the constructors so every
            # transport is measured without each one knowing about metrics.
            fetcher.recorder = self._recorder
            fetcher.open()
            self._fetchers[kind] = fetcher
        return self._fetchers[kind]

    def close(self) -> None:
        for fetcher in self._fetchers.values():
            fetcher.close()
        self._fetchers.clear()

    def __enter__(self) -> "FetcherPool":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
