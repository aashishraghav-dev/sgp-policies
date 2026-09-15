"""Source connectors.

``load_connectors`` imports every connector module so their
``@source_registry.register`` decorators run. Registration is by import
side effect, so a new website is wired in by adding its module here (or by
dropping it in a package this function walks) -- no factory to edit.
"""

from __future__ import annotations

import importlib
import pkgutil

from policy_scraper.sources.base import SourceConnector, build_connector, source_registry

_loaded = False


def load_connectors() -> None:
    """Import all connector modules under this package. Idempotent."""
    global _loaded
    if _loaded:
        return

    package = importlib.import_module(__name__)
    for module in pkgutil.walk_packages(package.__path__, prefix=f"{__name__}."):
        if module.name.rsplit(".", 1)[-1].startswith("_"):
            continue
        importlib.import_module(module.name)

    _loaded = True


__all__ = ["SourceConnector", "build_connector", "load_connectors", "source_registry"]
