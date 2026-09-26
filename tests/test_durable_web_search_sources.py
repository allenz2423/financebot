from src.services.llm import (
    _durable_research_packet,
    _durable_tool_result_summary,
    _durable_web_search_sources,
)


def test_structured_search_evidence_is_retained_bounded_and_sanitized():
    result = _durable_web_search_sources({
        "text": "model-facing formatted output",
        "results": [{
            "url": "https://example.org/reset/opaque-token?state=private",
            "title": "T" * 900,
            "snippet": "S" * 9000,
            "engine": "private implementation detail",
        }, {"url": "https://example.net/no-snippet"}],
    })

    assert len(result["results"]) == 1
    assert set(result["results"][0]) == {"url", "title", "snippet"}
    assert result["results"][0]["url"] == "https://example.org"
    assert "opaque-token" not in repr(result)
    assert "state=private" not in repr(result)
    receipt_summary = _durable_tool_result_summary(
        "search_web", "raw path token", result
    )
    assert "opaque-token" not in receipt_summary
    assert "state=private" not in receipt_summary
    assert "raw path token" not in receipt_summary
    assert len(result["results"][0]["title"]) == 500
    assert len(result["results"][0]["snippet"]) == 4000


def test_direct_user_url_is_not_misrepresented_as_search_evidence():
    assert _durable_web_search_sources({
        "direct_url": "https://example.org/",
        "results": [{
            "url": "https://example.org/",
            "snippet": "User-supplied direct URL.",
        }],
    }) == {"results": []}


def test_unsafe_search_text_is_not_added_to_durable_reflection_cards():
    result = _durable_web_search_sources({
        "results": [{
            "url": "https://example.org/reset/opaque-token",
            "title": "Account reset page",
            "snippet": "My 2FA code is 123456.",
        }, {
            "url": "https://example.net/",
            "title": "A safe source",
            "snippet": "A neutral source-backed description of a public topic.",
        }],
    })

    assert result["results"] == [{
        "url": "https://example.net",
        "title": "A safe source",
        "snippet": "A neutral source-backed description of a public topic.",
    }]


def test_receipt_safe_research_packet_drops_secret_urls_and_unsafe_excerpts():
    raw = {
        "status": "success",
        "queries": ["private query"],
        "evidence_text": "contains raw reset URL",
        "sources": [{
            "url": "https://example.org/reset/opaque-token?state=private",
            "title": "Safe title",
            "snippet": "A neutral public source description.",
            "excerpt": "My 2FA code is 123456.",
            "fetched": True,
        }],
    }
    safe = _durable_research_packet(raw)
    summary = _durable_tool_result_summary("research_topic", raw, safe)

    assert safe["sources"] == [{
        "url": "https://example.org",
        "title": "Safe title",
        "snippet": "A neutral public source description.",
        "excerpt": "",
        "fetched": False,
    }]
    assert "opaque-token" not in summary
    assert "state=private" not in summary
    assert "123456" not in summary
    assert "private query" not in summary
    assert "evidence_text" not in summary
    failed_summary = _durable_tool_result_summary(
        "research_topic", "raw failure: https://example.org/reset/opaque-token", safe
    )
    assert "opaque-token" not in failed_summary
