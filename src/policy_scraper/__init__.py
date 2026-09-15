"""Regulatory policy scraper.

Discovers documents on regulator websites (RBI, NPCI), converts them to
markdown, and archives them in object storage alongside YAML manifests
that make subsequent runs incremental.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
