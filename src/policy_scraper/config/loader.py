"""Load and validate the YAML configuration.

Supports ``${ENV_VAR}`` and ``${ENV_VAR:default}`` interpolation so secrets
and deployment-specific values (bucket names, docling service URL) stay out
of version control.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from policy_scraper.config.models import AppConfig
from policy_scraper.core.errors import ConfigurationError

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}")


def _interpolate(value: Any) -> Any:
    """Recursively substitute ``${VAR}`` / ``${VAR:default}`` in strings."""
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    if not isinstance(value, str):
        return value

    def replace(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        resolved = os.environ.get(name, default)
        if resolved is None:
            raise ConfigurationError(
                f"Environment variable {name!r} is referenced in the config but is not set, "
                f"and no default was given (use ${{{name}:fallback}})."
            )
        return resolved

    return _ENV_PATTERN.sub(replace, value)


def load_config(path: str | Path) -> AppConfig:
    """Read, interpolate and validate the config file."""
    config_path = Path(path).expanduser().resolve()
    if not config_path.is_file():
        raise ConfigurationError(f"Config file not found: {config_path}")

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"{config_path} is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigurationError(f"{config_path} must contain a YAML mapping at the top level.")

    try:
        return AppConfig.model_validate(_interpolate(raw))
    except ValidationError as exc:
        raise ConfigurationError(f"Invalid configuration in {config_path}:\n{exc}") from exc
