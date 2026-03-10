from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

PACKAGE_NAME = "orchestra-agent"


def _resolve_version() -> str:
    try:
        return version(PACKAGE_NAME)
    except PackageNotFoundError:
        return "0.1.0"


__version__ = _resolve_version()
