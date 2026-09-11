"""Database migration package.

Each module in this package exposes an ``apply(conn)`` function that is
idempotent and backwards compatible.  Migrations are ordered by filename
prefix and run once at application startup.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from typing import List

logger = logging.getLogger(__name__)


def discover_migrations() -> List[str]:
    """Return migration module names sorted by filename prefix."""
    names: List[str] = []
    try:
        package = importlib.import_module("src.db.migrations")
    except Exception:
        return names
    for _finder, name, _is_pkg in pkgutil.iter_modules(package.__path__):
        if name.startswith("_"):
            continue
        names.append(name)
    names.sort()
    return names


def apply_all(conn) -> List[str]:
    """Apply every discovered migration in order. Returns applied names."""
    applied: List[str] = []
    for name in discover_migrations():
        try:
            module = importlib.import_module(f"src.db.migrations.{name}")
            apply = getattr(module, "apply", None)
            if not callable(apply):
                logger.warning("migration %s has no apply() function", name)
                continue
            apply(conn)
            applied.append(name)
        except Exception as exc:
            logger.error("migration %s failed: %s", name, exc)
            raise
    return applied


__all__ = ["discover_migrations", "apply_all"]