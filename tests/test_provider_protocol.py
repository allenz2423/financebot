from src.services.provider_protocol import parse_stream_line


def test_openai_stream_preserves_native_tool_delta_and_finish_reason():
    event = parse_stream_line(
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"search_web","arguments":"{\\"query\\":"}}]},"finish_reason":null}]}',
        "openai",
    )
    assert event is not None
    assert event.delta["tool_calls"][0]["function"]["name"] == "search_web"
    assert event.finish_reason is None
    assert not event.done

    finished = parse_stream_line(
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
        "openai",
    )
    assert finished.finish_reason == "tool_calls"


def test_openai_done_marker_is_not_a_tool_call():
    event = parse_stream_line("data: [DONE]", "openai")
    assert event.done
    assert event.delta == {}


def test_ollama_stream_preserves_structured_calls_and_done_reason():
    event = parse_stream_line(
        '{"message":{"content":"","tool_calls":[{"function":{"name":"search_web","arguments":{"query":"x"}}}]},"done":true,"done_reason":"stop"}',
        "ollama",
    )
    assert event.done
    assert event.finish_reason == "stop"
    assert event.delta["tool_calls"][0]["function"]["name"] == "search_web"
