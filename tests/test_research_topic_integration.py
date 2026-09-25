from src.core.discovery import CAPABILITY_REGISTRY, explore_domain
from src.services.llm import BOT_TOOLS_SCHEMA


def test_research_topic_is_native_tool_schema():
    names = {entry["function"]["name"] for entry in BOT_TOOLS_SCHEMA}
    assert "research_topic" in names
    schema = next(entry["function"] for entry in BOT_TOOLS_SCHEMA if entry["function"]["name"] == "research_topic")
    assert schema["parameters"]["required"] == ["query"]
    assert "max_sources" in schema["parameters"]["properties"]


def test_research_topic_is_discoverable():
    assert "research_topic" in CAPABILITY_REGISTRY["Web Research"]["Search & Extract"]
    assert "research_topic" in explore_domain("Web Research")
