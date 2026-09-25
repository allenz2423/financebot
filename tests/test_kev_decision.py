import asyncio
import json
import sqlite3

import httpx
import pytest

from src.services.kev_decision import (
    DecisionInvalidResponse,
    DecisionProvider,
    DecisionQuestion,
    DecisionRequest,
    accepted_answer,
    threshold_for,
    tool_choice_request,
)
from src.services.tool_receipts import ReceiptStore


def _response_payload():
    return {
        "model": "kev-4b",
        "answers": {
            "tool": {
                "type": "choice",
                "choice": "search_web",
                "confidence": 0.92,
                "probabilities": {
                    "search_web": 0.92,
                    "read_workspace_file": 0.04,
                    "none": 0.04,
                    "clarify": 0.0,
                },
            }
        },
    }


def test_tool_choice_request_is_closed_over_declared_candidates():
    request = tool_choice_request(
        state="Find the current price.",
        candidates=["search_web", "search_web", "read_workspace_file"],
        turn_id="turn-1",
        round_id=2,
    )
    criteria = request.questions["tool"].criteria
    assert list(criteria) == ["search_web", "read_workspace_file", "none", "clarify"]


def test_tool_choice_request_carries_bounded_descriptions():
    request = tool_choice_request(
        state="Read the uploaded resume.",
        candidates=["read_workspace_file"],
        descriptions={"read_workspace_file": "Read a text file from the workspace."},
        turn_id="turn-1",
        round_id=1,
    )
    assert request.questions["tool"].criteria["read_workspace_file"] == (
        "Read a text file from the workspace."
    )


def test_provider_parses_typed_response_without_text_generation(monkeypatch):
    async def handler(request):
        return httpx.Response(200, json=_response_payload(), request=request)

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            return await handler(httpx.Request("POST", url))

    monkeypatch.setattr("src.services.kev_decision.httpx.AsyncClient", FakeClient)
    provider = DecisionProvider(
        mode="local",
        local_url="http://kev.test/v1/systemone",
        model="kev-4b",
    )
    request = tool_choice_request(
        state="Find the current price.",
        candidates=["search_web", "read_workspace_file"],
        turn_id="turn-1",
        round_id=1,
    )
    response = asyncio.run(provider.decide(request))
    assert response.ok
    assert response.answers["tool"].selected == "search_web"
    assert response.answers["tool"].top_probability == pytest.approx(0.92)


def test_provider_builds_sibling_models_url():
    provider = DecisionProvider(
        mode="local",
        local_url="http://kev.test:8009/v1/systemone",
    )
    assert provider.models_url == "http://kev.test:8009/v1/models"


def test_provider_defaults_to_api_alias_not_checkpoint_name(monkeypatch):
    monkeypatch.delenv("KEV_MODEL", raising=False)
    provider = DecisionProvider(mode="local", local_url="http://kev.test")
    assert provider.model == "kev-latest"


def test_health_uses_model_discovery(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"models": [{"id": "kev-latest", "run": "jaredpalmer/kev-4b"}]}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, **kwargs):
            assert url.endswith("/v1/models")
            return FakeResponse()

    monkeypatch.setattr("src.services.kev_decision.httpx.AsyncClient", FakeClient)
    provider = DecisionProvider(mode="local", local_url="http://kev.test/v1/systemone")
    assert asyncio.run(provider.health()) is True


def test_provider_rejects_choice_outside_declared_options(monkeypatch):
    payload = _response_payload()
    payload["answers"]["tool"]["choice"] = "delete_everything"

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr("src.services.kev_decision.httpx.AsyncClient", FakeClient)
    provider = DecisionProvider(mode="local", local_url="http://kev.test")
    request = tool_choice_request(
        state="x",
        candidates=["search_web"],
        turn_id="turn-1",
        round_id=1,
    )
    with pytest.raises(DecisionInvalidResponse):
        asyncio.run(provider.decide(request))


def test_provider_rejects_probability_keys_outside_declared_options(monkeypatch):
    payload = _response_payload()
    payload["answers"]["tool"]["probabilities"]["untrusted_tool"] = 0.0

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr("src.services.kev_decision.httpx.AsyncClient", FakeClient)
    provider = DecisionProvider(mode="local", local_url="http://kev.test")
    request = tool_choice_request(
        state="x",
        candidates=["search_web"],
        turn_id="turn-1",
        round_id=1,
    )
    with pytest.raises(DecisionInvalidResponse):
        asyncio.run(provider.decide(request))


def test_provider_validates_noul_and_score_ranges(monkeypatch):
    payload = {
        "model": "kev-latest",
        "answers": {
            "urgent": {
                "type": "noul",
                "noul": 0.91,
                "probabilities": {"true": 0.91, "false": 0.09},
                "confidence": 0.82,
            },
            "severity": {
                "type": "score",
                "score": 1.5,
                "probabilities": {"0": 0.1, "1": 0.3, "2": 0.6},
                "confidence": 0.7,
            },
        },
    }

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr("src.services.kev_decision.httpx.AsyncClient", FakeClient)
    provider = DecisionProvider(mode="local", local_url="http://kev.test")
    request = DecisionRequest(
        state="x",
        questions={
            "urgent": DecisionQuestion(type="noul", instructions="Urgent?"),
            "severity": DecisionQuestion(
                type="score", instructions="Severity?", criteria=["low", "medium", "high"]
            ),
        },
    )
    response = asyncio.run(provider.decide(request))
    assert response.answers["urgent"].selected is True
    assert response.answers["severity"].selected == pytest.approx(1.5)


def test_conservative_thresholds_abstain_on_ambiguous_answer():
    request = tool_choice_request(
        state="x",
        candidates=["search_web"],
        turn_id="turn-1",
        round_id=1,
    )
    answer_type = request.questions["tool"].type
    from src.services.kev_decision import DecisionAnswer

    answer = DecisionAnswer(
        question_id="tool",
        type=answer_type,
        selected="search_web",
        probabilities={"search_web": 0.61, "none": 0.39},
        confidence=0.2,
    )
    assert threshold_for("tool_routing") == (0.80, 0.15)
    assert not accepted_answer(
        answer,
        decision_kind="tool_routing",
        allowed={"search_web", "none", "clarify"},
    )


def test_decision_records_are_returned_with_turn_observability():
    connection = sqlite3.connect(":memory:")
    store = ReceiptStore(connection)
    store.record_decision(
        {
            "decision_id": "kev-1",
            "turn_id": "turn-1",
            "round_id": 3,
            "decision_kind": "tool_routing",
            "provider": "kev",
            "mode": "local",
            "requested_model": "kev-4b",
            "actual_model": "kev-4b",
            "endpoint_profile": "kev-local",
            "candidates": {"search_web", "none"},
            "selected": "search_web",
            "probabilities": {"search_web": 0.9, "none": 0.1},
            "confidence": 0.9,
            "threshold": 0.8,
            "accepted": True,
            "latency_ms": 12.0,
        }
    )
    record = ReceiptStore(connection).observability_for_turn("turn-1")["decision_records"][0]
    assert record["selected"] == "search_web"
    assert record["accepted"] is True
    assert json.loads(json.dumps(record["probabilities"]))["search_web"] == 0.9
