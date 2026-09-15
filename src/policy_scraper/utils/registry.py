"""A tiny name -> class registry.

The pipeline has four plugin points (fetchers, source connectors,
converters, object stores) that all need the same thing: map a string from
the YAML config onto an implementation class.  One generic registry keeps
that logic in a single place instead of four near-identical factories.
"""

from __future__ import annotations

from typing import Callable, Generic, Iterable, TypeVar

from policy_scraper.core.errors import ConfigurationError

T = TypeVar("T")


class Registry(Generic[T]):
    """Maps config keys to implementations, with decorator-based signup."""

    def __init__(self, kind: str) -> None:
        self._kind = kind
        self._items: dict[str, type[T]] = {}

    def register(self, key: str) -> Callable[[type[T]], type[T]]:
        """Decorator: ``@some_registry.register("rbi.master_directions")``."""

        def decorator(cls: type[T]) -> type[T]:
            if key in self._items and self._items[key] is not cls:
                raise ConfigurationError(f"Duplicate {self._kind} registration for {key!r}.")
            self._items[key] = cls
            return cls

        return decorator

    def get(self, key: str) -> type[T]:
        try:
            return self._items[key]
        except KeyError:
            raise ConfigurationError(
                f"Unknown {self._kind} {key!r}. Registered: {', '.join(self.keys()) or '(none)'}."
            ) from None

    def keys(self) -> Iterable[str]:
        return sorted(self._items)

    def __contains__(self, key: object) -> bool:
        return key in self._items
