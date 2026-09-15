"""Exception hierarchy.

Split into ``TransientError`` (worth retrying) and ``PermanentError`` (not),
so the retry decorator can make that call without inspecting messages.
"""

from __future__ import annotations


class ScraperError(Exception):
    """Base class for every error raised by this package."""


class TransientError(ScraperError):
    """A failure that may succeed on retry (timeouts, 5xx, rate limits)."""


class PermanentError(ScraperError):
    """A failure that retrying cannot fix (404, malformed document)."""


class ConfigurationError(PermanentError):
    """The YAML config is invalid or references an unregistered component."""


class FetchError(TransientError):
    """A document or listing page could not be retrieved."""


class AccessDeniedError(PermanentError):
    """The origin actively refused us (bot wall, 403) -- needs a different fetcher."""


class ConversionError(PermanentError):
    """A payload could not be turned into markdown."""


class StorageError(TransientError):
    """The object store rejected a read or write."""
