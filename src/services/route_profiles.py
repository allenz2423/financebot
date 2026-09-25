"""Explicit model/provider route profiles.

OpenRouter may still be used as the transport, but Delilah owns which model
and provider combination is eligible for a request. Automatic model aliases
and gateway failover are rejected by strict profiles.
"""

from __future__ import annotations

from dataclasses import dataclass


AUTO_ROUTE_NAMES = frozenset({"auto", "auto-beta", "openrouter/auto", "openrouter/auto-beta"})


@dataclass(frozen=True)
class RouteProfile:
    name: str
    provider: str
    model: str
    provider_order: tuple[str, ...] = ()
    allow_fallbacks: bool = False
    require_parameters: bool = True


def parse_route_profiles(raw: str | None) -> tuple[RouteProfile, ...]:
    """Parse NAME=PROVIDER|MODEL|PROVIDER... entries.

    A pipe delimiter keeps OpenRouter model IDs such as model:free intact.
    """

    profiles: list[RouteProfile] = []
    for entry in str(raw or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            name, route = entry.split("=", 1)
            route_parts = [part.strip() for part in route.split("|") if part.strip()]
        except ValueError as exc:
            raise ValueError(
                "route profile must be NAME=PROVIDER|MODEL|PROVIDER..."
            ) from exc
        if len(route_parts) < 2 or not name.strip():
            raise ValueError(f"invalid empty route profile: {entry!r}")
        profiles.append(
            RouteProfile(
                name=name.strip(),
                provider=route_parts[0],
                model=route_parts[1],
                provider_order=tuple(route_parts[2:]),
            )
        )
    return tuple(profiles)


def validate_profile(profile: RouteProfile, *, tool_enabled: bool = True) -> None:
    if not profile.name or not profile.provider or not profile.model:
        raise ValueError("route profile requires name, provider, and model")
    if tool_enabled and profile.model.casefold() in AUTO_ROUTE_NAMES:
        raise ValueError(
            f"automatic model route {profile.model!r} is not allowed for tool calls"
        )
    if tool_enabled and profile.allow_fallbacks:
        raise ValueError("provider failover is not allowed for tool-enabled profiles")
    if tool_enabled and not profile.require_parameters:
        raise ValueError("tool-enabled profiles must require tool parameters")


def profile_for(
    profiles: tuple[RouteProfile, ...],
    name: str,
    *,
    tool_enabled: bool = True,
) -> RouteProfile:
    for profile in profiles:
        if profile.name == name:
            validate_profile(profile, tool_enabled=tool_enabled)
            return profile
    raise ValueError(f"unknown route profile: {name}")


__all__ = [
    "AUTO_ROUTE_NAMES",
    "RouteProfile",
    "parse_route_profiles",
    "profile_for",
    "validate_profile",
]
