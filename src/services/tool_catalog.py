"""Fail-closed validation for the model-facing tool catalog.

The catalog is part of the execution boundary: a duplicate or malformed
schema can make a provider call a different handler than the one operators
think they exposed.  Keep this check dependency-free so it can run at import
time and in CI without contacting any external service.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


class ToolCatalogError(RuntimeError):
    """Raised when advertised tool metadata is unsafe or inconsistent."""


def validate_tool_catalog(
    schemas: Iterable[Mapping[str, Any]],
    *,
    expected_names: Iterable[str] | None = None,
    registered_names: Iterable[str] | None = None,
) -> frozenset[str]:
    """Validate and return the unique names in a provider tool catalog.

    This checks structure, duplicate names, optional expected-name parity, and
    that every separately registered handler is actually advertised.  It does
    not execute handlers; execution safety belongs to the receipt/dispatch
    boundary.
    """

    names: list[str] = []
    for index, schema in enumerate(schemas):
        if not isinstance(schema, Mapping):
            raise ToolCatalogError(f"tool schema {index} is not an object")
        function = schema.get("function")
        if not isinstance(function, Mapping):
            raise ToolCatalogError(f"tool schema {index} is missing function metadata")
        name = str(function.get("name") or "").strip()
        if not name:
            raise ToolCatalogError(f"tool schema {index} has an empty function name")
        if str(schema.get("type") or "function") != "function":
            raise ToolCatalogError(f"tool schema {name!r} is not a function schema")
        parameters = function.get("parameters")
        if not isinstance(parameters, Mapping):
            raise ToolCatalogError(f"tool schema {name!r} has invalid parameters")
        names.append(name)

    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ToolCatalogError(
            "duplicate tool schema name(s): " + ", ".join(duplicates)
        )

    actual = frozenset(names)
    if expected_names is not None:
        expected = frozenset(str(name).strip() for name in expected_names if str(name).strip())
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        if missing or extra:
            raise ToolCatalogError(
                f"tool schema drift detected; missing={missing}, extra={extra}"
            )

    if registered_names is not None:
        registered = frozenset(
            str(name).strip() for name in registered_names if str(name).strip()
        )
        missing_handlers = sorted(registered - actual)
        if missing_handlers:
            raise ToolCatalogError(
                "registered handler(s) are not advertised: "
                + ", ".join(missing_handlers)
            )

    return actual


__all__ = ["ToolCatalogError", "validate_tool_catalog"]
