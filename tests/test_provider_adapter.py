from src.services.provider_adapter import (
    CompatibleProviderAdapter,
    ProviderEventKind,
    ProviderRequest,
    events_from_stream_line,
)


def test_provider_request_is_normalized_and_payload_is_provider_neutral():
    messages = [{"role": "user", "content": "hello"}]
    request = ProviderRequest.from_values(
        provider=" OpenAI ",
        model="model-a",
        messages=messages,
        tools=[{"type": "function", "function": {"name": "search_web"}}],
        stream=True,
        temperature=0.2,
        max_tokens=128,
        request_id="req-1",
    )

    assert request.provider == "openai"
    assert request.messages[0]["content"] == "hello"
    assert request.as_payload() == {
        "model": "model-a",
        "messages": messages,
        "tools": [{"type": "function", "function": {"name": "search_web"}}],
        "stream": True,
        "temperature": 0.2,
        "max_tokens": 128,
    }


def test_openai_line_becomes_text_and_structured_tool_events():
    events = events_from_stream_line(
        'data: {"choices":[{"delta":{"content":"Hi","tool_calls":[{"id":"c1","type":"function","function":{"name":"search_web","arguments":"{\\"q\\":"}}]},"finish_reason":null}]}',
        "openai",
        request_id="req-2",
    )

    assert [event.kind for event in events] == [ProviderEventKind.TEXT, ProviderEventKind.TOOL_CALL]
    assert events[0].text == "Hi"
    assert events[1].tool_calls[0]["function"]["name"] == "search_web"
    assert events[1].request_id == "req-2"


def test_ollama_done_line_becomes_finish_event_and_malformed_lines_are_ignored():
    events = events_from_stream_line(
        '{"message":{"content":"done"},"done":true,"done_reason":"stop"}',
        "ollama",
    )
    assert [event.kind for event in events] == [ProviderEventKind.TEXT, ProviderEventKind.FINISH]
    assert events[-1].finish_reason == "stop"
    assert events[-1].done
    assert events_from_stream_line("not-json", "ollama") == ()


def test_openai_error_frame_is_a_typed_error_event():
    events = events_from_stream_line(
        'data: {"type":"error","error":{"code":"rate_limit","message":"slow down"}}',
        "openai",
        request_id="req-3",
    )
    assert len(events) == 1
    assert events[0].kind is ProviderEventKind.ERROR
    assert events[0].error == "slow down"
    assert events[0].request_id == "req-3"


def test_compatible_adapter_rejects_cross_provider_requests():
    adapter = CompatibleProviderAdapter("openai")
    request = ProviderRequest.from_values(
        provider="ollama",
        model="model-a",
        messages=[{"role": "user", "content": "hello"}],
    )
    try:
        adapter.build_request(request)
    except ValueError as exc:
        assert "does not match" in str(exc)
    else:  # pragma: no cover - assertion keeps failure explicit
        raise AssertionError("cross-provider request was accepted")
