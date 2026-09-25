"""Opt-in local-model behavioral workflow.
Run with ``DELILAH_LOCAL_E2E=1`` and a reachable Ollama instance. It is skipped
by default because model availability and latency are environment-dependent.
"""

import os

import httpx
import pytest

from src.services.claim_evidence import evaluate_claims


def _enabled() -> bool:
    return os.getenv("DELILAH_LOCAL_E2E", "0").strip().lower() in {
        "1", "true", "yes", "on"
    }


@pytest.mark.asyncio
async def test_local_model_native_tool_workflow_and_claim_boundary():
    if not _enabled():
        pytest.skip("set DELILAH_LOCAL_E2E=1 to run the Ollama workflow")

    url = os.getenv("OLLAMA_URL", "http://localhost:11434/api/chat")
    model = os.getenv("DELILAH_LOCAL_E2E_MODEL", os.getenv("ADVISOR_MODEL", "gemma4:26b"))
    tools = [{
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "Search public web pages for a question.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    }]
    messages = [
        {
            "role": "system",
            "content": (
                "Use native structured tool calls. Do not claim a search happened "
                "unless the tool result is present."
            ),
        },
        {"role": "user", "content": "Find the official Python website."},
    ]
    async with httpx.AsyncClient(timeout=90.0) as client:
        first = await client.post(
            url,
            json={"model": model, "messages": messages, "tools": tools, "stream": False},
        )
        first.raise_for_status()
        message = first.json().get("message") or {}
        calls = message.get("tool_calls") or []
        assert calls, "local model narrated the tool instead of emitting a native call"
        assert calls[0].get("function", {}).get("name") == "search_web"

        call = calls[0]
        call_id = str(call.get("id") or "local-e2e-call-1")
        messages.extend([
            {"role": "assistant", "content": message.get("content") or "", "tool_calls": calls},
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": '{"status":"confirmed","complete":true,"url":"https://www.python.org/"}',
            },
        ])
        second = await client.post(
            url,
            json={"model": model, "messages": messages, "tools": tools, "stream": False},
        )
        second.raise_for_status()
        final = str((second.json().get("message") or {}).get("content") or "")

    decision = evaluate_claims(
        final,
        [{"name": "search_web", "status": "confirmed", "ok": True, "complete": True}],
    )
    assert decision.allowed, f"local model produced an unsupported final claim: {final!r}"
