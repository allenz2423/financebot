import pytest

from src.services.route_profiles import (
    RouteProfile,
    parse_route_profiles,
    profile_for,
    validate_profile,
)


def test_parse_explicit_model_provider_profile():
    profiles = parse_route_profiles("primary=OpenAI|gpt-5-mini|OpenAI")
    assert profiles[0].name == "primary"
    assert profiles[0].provider == "OpenAI"
    assert profiles[0].model == "gpt-5-mini"
    assert profiles[0].provider_order == ("OpenAI",)


def test_auto_model_is_rejected_for_tool_enabled_route():
    with pytest.raises(ValueError, match="automatic model route"):
        validate_profile(RouteProfile("bad", "OpenRouter", "openrouter/auto"))


def test_profile_selection_is_explicit():
    profiles = parse_route_profiles("local=ollama|gemma4|Local")
    assert profile_for(profiles, "local").model == "gemma4"
    with pytest.raises(ValueError, match="unknown route profile"):
        profile_for(profiles, "missing")
