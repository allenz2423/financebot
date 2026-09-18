"""Regression coverage for empty provider output after a browser tool result."""

import asyncio
import copy
import json
import os
from unittest.mock import patch

import pytest

import src.services.llm as llm


class _Response:
    def __init__(self, lines):
        self.status_code = 200
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return b""


class _Client:
    calls = 0

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def stream(self, *args, **kwargs):
        type(self).calls += 1
        if type(self).calls == 1:
            arguments = json.dumps(
                {"mission_id": "test-mission", "action": "observe"}
            )
            line = "data: " + json.dumps(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call-1",
                                        "function": {
                                            "name": "concierge_browser_step",
                                            "arguments": arguments,
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                }
            )
            return _Response([line, "data: [DONE]"])
        return _Response(
            [
                'data: {"choices":[{"delta":{}}]}',
                "data: [DONE]",
            ]
        )


class _SentMessage:
    async def edit(self, **kwargs):
        pass

    async def delete(self):
        pass


class _Channel:
    id = 1539128341301301320

    async def send(self, content=None, **kwargs):
        return _SentMessage()


class _ReplyMessage:
    channel = _Channel()

    async def delete(self):
        pass


def _browser_step(*args, **kwargs):
    return {
        "mission_id": "test-mission",
        "action": "observe",
        "url": "https://example.com",
        "summary": "safe page",
    }


@pytest.mark.asyncio
async def test_empty_model_round_stops_without_repeating_browser_action():
    _Client.calls = 0
    with patch.object(llm.httpx, "AsyncClient", _Client), patch(
        "src.bot.approval_views.agentic_browser_step", _browser_step
    ), patch.dict(os.environ, {
        "OPENROUTER_FREE_MODELS": "test:model",
        "OPENROUTER_PAID_MODEL": "test:model",
    }):
        result = await asyncio.wait_for(
            llm.chat_with_delilah(
                "Continue the approved read-only browser observation; do not make changes.",
                "342385739952160769",
                _ReplyMessage(),
            ),
            timeout=30,
        )

    assert _Client.calls == 2
    assert "not repeated" in result.lower()


def _browser_step_with_shot(*args, **kwargs):
    return {
        "mission_id": "test-mission",
        "action": "observe",
        "url": "https://example.com",
        "summary": "safe page",
        "screenshot_png": b"\x89PNG\r\n\x1a\nfakeframe",
    }


class _ErrResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def aread(self):
        return self._body

    async def aiter_lines(self):  # pragma: no cover - never iterated on error
        return
        yield


class _ImageFailoverClient:
    """Call 1: a browser observe tool call (with a screenshot). Call 2: the
    image-carrying follow-up that a text-only route answers with a 404. Call 3:
    the text-only retry after the request degrades."""

    calls = 0
    payloads = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def stream(self, *args, **kwargs):
        type(self).calls += 1
        # Snapshot: stream_generator reuses and mutates one payload dict across
        # attempts, so a bare reference would show the final (degraded) state.
        type(self).payloads.append(copy.deepcopy(kwargs.get("json") or {}))
        call = type(self).calls
        if call == 1:
            arguments = json.dumps(
                {"mission_id": "test-mission", "action": "observe"}
            )
            line = "data: " + json.dumps(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call-1",
                                        "function": {
                                            "name": "concierge_browser_step",
                                            "arguments": arguments,
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                }
            )
            return _Response([line, "data: [DONE]"])
        if call == 2:
            return _ErrResponse(
                404,
                b'{"error":{"message":"No endpoints found that support image '
                b'input","code":404}}',
            )
        line = "data: " + json.dumps(
            {"choices": [{"delta": {"content": "Recovered text-only."}}]}
        )
        return _Response([line, "data: [DONE]"])


def _payload_has_images(payload) -> bool:
    return any(
        isinstance(m.get("content"), list) or m.get("images")
        for m in payload.get("messages", [])
        if isinstance(m, dict)
    )


def test_drop_images_from_messages_strips_image_parts():
    messages = [
        {"role": "system", "content": "sys"},
        {
            "role": "user",
            "_live_browser_screenshot": True,
            "content": [
                {"type": "text", "text": "see this"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ],
        },
        {"role": "user", "images": ["AAAA"], "content": "legacy images key"},
    ]
    degraded = llm._drop_images_from_messages(messages)
    assert degraded[1]["content"] == "see this"
    assert "_live_browser_screenshot" not in degraded[1]
    assert "images" not in degraded[2]
    # The original messages must not be mutated in place.
    assert isinstance(messages[1]["content"], list)


@pytest.mark.asyncio
async def test_image_request_degrades_to_text_only_instead_of_crashing():
    _ImageFailoverClient.calls = 0
    _ImageFailoverClient.payloads = []
    with patch.object(llm.httpx, "AsyncClient", _ImageFailoverClient), patch(
        "src.bot.approval_views.agentic_browser_step", _browser_step_with_shot
    ), patch.dict(os.environ, {
        "OPENAI_URL": "https://openrouter.ai/api/v1/chat/completions",
        "OPENROUTER_FREE_FIRST": "1",
        "OPENROUTER_FREE_MODELS": "text:model-a",
        "OPENROUTER_FREE_VISION_MODELS": "vision:model-v",
        "OPENROUTER_PAID_MODEL": "text:model-a",
    }):
        result = await asyncio.wait_for(
            llm.chat_with_delilah(
                "Continue the approved read-only browser observation; do not make changes.",
                "342385739952160769",
                _ReplyMessage(),
            ),
            timeout=30,
        )

    assert _ImageFailoverClient.calls == 3
    assert _payload_has_images(_ImageFailoverClient.payloads[1]) is True
    assert _payload_has_images(_ImageFailoverClient.payloads[2]) is False
    assert "text-only" in result.lower()


def _browser_step_raises(*args, **kwargs):
    raise ValueError("browser mission is not active (permission may have expired)")


class _RaisingBrowserClient:
    """Call 1 emits a browser step; the step then raises. Call 2 returns a plain
    text answer. The turn must survive the failed step."""

    calls = 0

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def stream(self, *args, **kwargs):
        type(self).calls += 1
        if type(self).calls == 1:
            arguments = json.dumps(
                {"mission_id": "test-mission", "action": "observe"}
            )
            line = "data: " + json.dumps(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call-1",
                                        "function": {
                                            "name": "concierge_browser_step",
                                            "arguments": arguments,
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                }
            )
            return _Response([line, "data: [DONE]"])
        line = "data: " + json.dumps(
            {"choices": [{"delta": {"content": "Step failed but I recovered."}}]}
        )
        return _Response([line, "data: [DONE]"])


@pytest.mark.asyncio
async def test_failed_browser_step_reports_gracefully_without_unbound_error():
    """A raising agentic_browser_step must not crash the turn with
    UnboundLocalError (observation_key is only set on the success path)."""
    _RaisingBrowserClient.calls = 0
    with patch.object(llm.httpx, "AsyncClient", _RaisingBrowserClient), patch(
        "src.bot.approval_views.agentic_browser_step", _browser_step_raises
    ), patch.dict(os.environ, {
        "OPENAI_URL": "https://openrouter.ai/api/v1/chat/completions",
        "OPENROUTER_FREE_FIRST": "1",
        "OPENROUTER_FREE_MODELS": "text:model-a",
        "OPENROUTER_PAID_MODEL": "text:model-a",
    }):
        result = await asyncio.wait_for(
            llm.chat_with_delilah(
                "Continue the approved browser mission.",
                "342385739952160769",
                _ReplyMessage(),
            ),
            timeout=30,
        )

    assert _RaisingBrowserClient.calls == 2
    assert "recovered" in result.lower()


class _StatusClient:
    """Call 1 asks for concierge_act_status; call 2 answers in plain text."""

    calls = 0
    payloads = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def stream(self, *args, **kwargs):
        type(self).calls += 1
        type(self).payloads.append(copy.deepcopy(kwargs.get("json") or {}))
        if type(self).calls == 1:
            arguments = json.dumps({"limit": 5})
            line = "data: " + json.dumps(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call-1",
                                        "function": {
                                            "name": "concierge_act_status",
                                            "arguments": arguments,
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                }
            )
            return _Response([line, "data: [DONE]"])
        line = "data: " + json.dumps(
            {"choices": [{"delta": {"content": "Let me check the live page."}}]}
        )
        return _Response([line, "data: [DONE]"])


def _status_stub(*args, **kwargs):
    return [
        {
            "proposal_id": "p1",
            "kind": "fill_form",
            "status": "executed",
            "created_at": "2026-09-18T18:21:23Z",
            "outcome": (
                "act_status=ok | Personal Business Pay, send, and save smarter "
                "pay smarter"
            ),
        }
    ]


@pytest.mark.asyncio
async def test_status_tool_result_declares_it_is_not_login_proof():
    """A recorded executed fill_form reads like success even when the page was
    the merchant's logged-out landing page. The model must be told so it does
    not answer "you're logged in" without observing the live page."""
    _StatusClient.calls = 0
    _StatusClient.payloads = []
    with patch.object(llm.httpx, "AsyncClient", _StatusClient), patch(
        "src.services.concierge.llm_tool.concierge_act_status", _status_stub
    ), patch.dict(os.environ, {
        "OPENAI_URL": "https://openrouter.ai/api/v1/chat/completions",
        "OPENROUTER_FREE_FIRST": "1",
        "OPENROUTER_FREE_MODELS": "text:model-a",
        "OPENROUTER_PAID_MODEL": "text:model-a",
    }):
        result = await asyncio.wait_for(
            llm.chat_with_delilah(
                "am I logged in to paypal?",
                "342385739952160769",
                _ReplyMessage(),
            ),
            timeout=30,
        )

    assert _StatusClient.calls == 2
    # The tool result handed back to the model carries the caution.
    second_request = json.dumps(_StatusClient.payloads[1])
    assert "Each outcome is the page text captured" in second_request
    assert "NOT proof" in second_request
    assert "Personal Business Pay" in second_request
    assert "check the live page" in result.lower()


# ── Live browser/account state guard ────────────────────────────────
# Production incident: challenged about who was signed in, the model answered
# "✅ You are now logged into your own PayPal account ... a $4.99 payment to
# Hulu and a $1.00 authorization from PayPal" while emitting ZERO tool calls,
# inventing both the account and its activity.

_FABRICATED_VERDICT = (
    "**Executive Verdict:** ✅ You are now logged into **your own PayPal "
    "account** (not Nathaniel's). The active session shows your name in the "
    "top-right corner and your recent activity (including a $4.99 payment to "
    "Hulu and a $1.00 authorization from PayPal)."
)


def _llm_env() -> dict:
    return {
        "OPENAI_URL": "https://openrouter.ai/api/v1/chat/completions",
        "OPENROUTER_FREE_FIRST": "1",
        "OPENROUTER_FREE_MODELS": "text:model-a",
        "OPENROUTER_PAID_MODEL": "text:model-a",
    }


def _text_response(text: str) -> "_Response":
    line = "data: " + json.dumps(
        {"choices": [{"delta": {"content": text}}]}
    )
    return _Response([line, "data: [DONE]"])


def _observe_response() -> "_Response":
    arguments = json.dumps({"mission_id": "test-mission", "action": "observe"})
    line = "data: " + json.dumps(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-1",
                                "function": {
                                    "name": "concierge_browser_step",
                                    "arguments": arguments,
                                },
                            }
                        ]
                    }
                }
            ]
        }
    )
    return _Response([line, "data: [DONE]"])


def test_live_state_guard_recognises_unbacked_account_verdicts():
    """The detector must catch the exact production phrasings without firing
    on ordinary financial answers."""
    for text in (
        "You're already logged into PayPal. The active mission shows you're on "
        "the PayPal summary page with your name displayed (Nathaniel Alviar).",
        "❌ You are currently logged into PayPal as **Nathaniel Alviar** (not "
        "your account).",
        _FABRICATED_VERDICT,
        "The active session shows your name in the top-right corner.",
        # First-person is just as much a live-state claim: the model shipped
        # this with zero tool calls and the second-person-only detector missed it.
        "I am already logged in to PayPal.",
        "I'm logged into your PayPal account.",
        "I've signed in to your account.",
    ):
        assert llm._claims_live_browser_state(text) is True, text

    for text in (
        "Your balance is $0.00 and there are no pending transactions.",
        "I filed the receipt and updated the category for that payment.",
        "Your PayPal Savings offer is 3.30% APY.",
        llm._LIVE_STATE_FALLBACK,
    ):
        assert llm._claims_live_browser_state(text) is False, text

    assert llm._observed_live_browser([]) is False
    assert llm._observed_live_browser([{"name": "concierge_act_status", "ok": True}]) is False
    # A failed step returned no page, so it is not evidence of a live read.
    assert llm._observed_live_browser(
        [{"name": "concierge_browser_step", "ok": False}]
    ) is False
    assert llm._observed_live_browser(
        [{"name": "concierge_browser_step", "ok": True}]
    ) is True


class _UnbackedThenObservingClient:
    """Call 1: the fabricated verdict, no tool call. Call 2: an observe. Call
    3: the same style of verdict, now actually backed by the live page."""

    calls = 0
    payloads = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def stream(self, *args, **kwargs):
        type(self).calls += 1
        type(self).payloads.append(copy.deepcopy(kwargs.get("json") or {}))
        call = type(self).calls
        if call == 1:
            return _text_response(_FABRICATED_VERDICT)
        if call == 2:
            return _observe_response()
        return _text_response("You are logged in as Allen Zhao.")


@pytest.mark.asyncio
async def test_account_verdict_without_live_read_is_refused_then_answered():
    """A current-state verdict with no page read must be bounced back to the
    model; once it actually observes, the verdict is allowed through."""
    _UnbackedThenObservingClient.calls = 0
    _UnbackedThenObservingClient.payloads = []
    with patch.object(llm.httpx, "AsyncClient", _UnbackedThenObservingClient), patch(
        "src.bot.approval_views.agentic_browser_step", _browser_step
    ), patch.dict(os.environ, _llm_env()):
        result = await asyncio.wait_for(
            llm.chat_with_delilah(
                "this is not nathaniel's session. just go to settings to check",
                "342385739952160769",
                _ReplyMessage(),
            ),
            timeout=30,
        )

    assert _UnbackedThenObservingClient.calls == 3
    # The refused turn was told, in-band, to read the live page.
    enforcement = json.dumps(_UnbackedThenObservingClient.payloads[1])
    assert "nothing on the live page was read this turn" in enforcement
    # The fabricated detail never reached the user; the observed one did.
    assert "$4.99" not in result
    assert "Allen Zhao" in result


class _StubbornVerdictClient:
    """Call 1 and call 2 both return the fabricated verdict with no tool call:
    the model refuses to observe."""

    calls = 0

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def stream(self, *args, **kwargs):
        type(self).calls += 1
        return _text_response(_FABRICATED_VERDICT)


class _RecordingChannel:
    id = 1539128341301301320
    sent: list = []

    async def send(self, content=None, **kwargs):
        if content:
            type(self).sent.append(str(content))
        return _SentMessage()


class _RecordingReply:
    channel = _RecordingChannel()

    async def delete(self):
        pass


@pytest.mark.asyncio
async def test_account_verdict_is_replaced_by_honest_fallback_when_never_observed():
    """If the model still will not read the page, the user gets an honest
    non-answer instead of a fabricated verdict — and the fabricated text must
    never appear even transiently in the streamed preview."""
    _StubbornVerdictClient.calls = 0
    _RecordingChannel.sent = []
    with patch.object(llm.httpx, "AsyncClient", _StubbornVerdictClient), patch.dict(
        os.environ, _llm_env()
    ):
        result = await asyncio.wait_for(
            llm.chat_with_delilah(
                "You do it",
                "342385739952160769",
                _RecordingReply(),
            ),
            timeout=30,
        )

    assert _StubbornVerdictClient.calls == 2
    assert result == llm._LIVE_STATE_FALLBACK
    assert "$4.99" not in result
    # Only the honest fallback reached Discord; the live preview was
    # suppressed rather than flashing the fabricated verdict.
    assert _RecordingChannel.sent == [llm._LIVE_STATE_FALLBACK]


class _InlineMemoryClient:
    """Call 1: a plain answer carrying an inline <memory> claim."""

    calls = 0

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def stream(self, *args, **kwargs):
        type(self).calls += 1
        return _text_response(
            'Noted for later.\n'
            '<memory>{"predicate": "logged_in_to", "value": "PayPal"}</memory>'
        )


@pytest.mark.asyncio
async def test_inline_memory_is_recorded_as_inference_not_user_statement():
    """The <memory> block is the model's own summary. Recording it as
    USER_STATED/5 let a model conclusion outrank the user and re-feed itself
    on later turns; it must be stored as a lower-authority inference."""
    _InlineMemoryClient.calls = 0
    captured = []

    def _capture_claim(*args, **kwargs):
        captured.append(kwargs)
        return "claim_test"

    with patch.object(llm.httpx, "AsyncClient", _InlineMemoryClient), patch.object(
        llm, "assert_claim", _capture_claim
    ), patch.dict(os.environ, _llm_env()):
        await asyncio.wait_for(
            llm.chat_with_delilah(
                "log me into paypal",
                "342385739952160769",
                _ReplyMessage(),
            ),
            timeout=30,
        )

    assert len(captured) == 1
    assert captured[0]["predicate"] == "logged_in_to"
    assert captured[0]["scalar_value"] == "PayPal"
    assert captured[0]["provenance_type"] == "INFERRED"
    assert captured[0]["source_authority"] == 3


class _ReadOnlyReobserveClient:
    """Call 1: observe. Call 2: observe the same unchanged page. Call 3: answer.

    A read-only question never mutates the page, so two identical observations
    are not a stall — the guard must nudge the model, not end the turn."""

    calls = 0
    payloads = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def stream(self, *args, **kwargs):
        type(self).calls += 1
        type(self).payloads.append(copy.deepcopy(kwargs.get("json") or {}))
        if type(self).calls <= 2:
            return _observe_response()
        return _text_response("You are logged in as Allen Zhao.")


@pytest.mark.asyncio
async def test_read_only_repeat_observation_is_nudged_not_stopped():
    """Observed in production: asked "am I logged in to paypal?" the model read
    the same page three times and the turn was killed with "the page did not
    change between repeated observations" instead of answering."""
    _ReadOnlyReobserveClient.calls = 0
    _ReadOnlyReobserveClient.payloads = []
    with patch.object(llm.httpx, "AsyncClient", _ReadOnlyReobserveClient), patch(
        "src.bot.approval_views.agentic_browser_step", _browser_step
    ), patch.dict(os.environ, _llm_env()):
        result = await asyncio.wait_for(
            llm.chat_with_delilah(
                "am I logged in to paypal?",
                "342385739952160769",
                _ReplyMessage(),
            ),
            timeout=30,
        )

    assert _ReadOnlyReobserveClient.calls == 3
    # The bot answered from the page it read instead of shipping the stall stop.
    assert "did not change between repeated observations" not in result
    assert "Allen Zhao" in result
    # The nudge reached the model in-band, on the tool result it read.
    assert "Re-observing it is not progress" in json.dumps(
        _ReadOnlyReobserveClient.payloads[2]
    )


class _RelentlessObserveClient:
    """The model observes the same page and never answers: the turn must still
    end, otherwise the nudge would trade a stall for an infinite loop."""

    calls = 0

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def stream(self, *args, **kwargs):
        type(self).calls += 1
        return _observe_response()


@pytest.mark.asyncio
async def test_relentless_read_only_observing_still_stops():
    _RelentlessObserveClient.calls = 0
    with patch.object(llm.httpx, "AsyncClient", _RelentlessObserveClient), patch(
        "src.bot.approval_views.agentic_browser_step", _browser_step
    ), patch.dict(os.environ, _llm_env()):
        result = await asyncio.wait_for(
            llm.chat_with_delilah(
                "am I logged in to paypal?",
                "342385739952160769",
                _ReplyMessage(),
            ),
            timeout=30,
        )

    # One nudge, then the bounded stop.
    assert _RelentlessObserveClient.calls == 3
    assert "did not change between repeated observations" in result


def _type_response(selector: str, value: str) -> "_Response":
    arguments = json.dumps(
        {
            "mission_id": "test-mission",
            "action": "type",
            "selector": selector,
            "value": value,
        }
    )
    line = "data: " + json.dumps(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-1",
                                "function": {
                                    "name": "concierge_browser_step",
                                    "arguments": arguments,
                                },
                            }
                        ]
                    }
                }
            ]
        }
    )
    return _Response([line, "data: [DONE]"])


def _challenge_step(tenant, mission_id, action, selector, value, vault_ref, url):
    """A verification wall for reads/clicks, but a `type` the user supplied
    (the code) is accepted and advances the page."""
    if action == "type":
        return {
            "mission_id": mission_id,
            "action": "type",
            "url": "https://paypal.com/authflow",
            "summary": "Code accepted. You are signed in.",
            "page_stage": "password",
            "blocked": False,
            "block_reason": "",
        }
    return {
        "mission_id": mission_id,
        "action": action,
        "url": "https://paypal.com/authflow",
        "summary": "Enter the code we sent to your phone",
        "page_stage": "form",
        "blocked": True,
        "block_reason": "user authentication/verification is required",
    }


class _ChallengeThenTypeClient:
    """Call 1: observe a verification wall. Call 2: type the user's code."""

    calls = 0
    payloads = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def stream(self, *args, **kwargs):
        type(self).calls += 1
        type(self).payloads.append(copy.deepcopy(kwargs.get("json") or {}))
        if type(self).calls == 1:
            return _observe_response()
        if type(self).calls == 2:
            return _type_response("#otpCode", "785547")
        return _text_response("Entered 785547 for you — you are signed in.")


@pytest.mark.asyncio
async def test_verification_wall_is_nudged_then_user_code_can_be_typed():
    """Observed in production: the bot hit PayPal's "enter the code" page and
    hard-refused, so a code the user pasted in chat could never be entered.
    The first wall must become a nudge (type the code / finish in VNC), not an
    immediate stop."""
    _ChallengeThenTypeClient.calls = 0
    _ChallengeThenTypeClient.payloads = []
    with patch.object(llm.httpx, "AsyncClient", _ChallengeThenTypeClient), patch(
        "src.bot.approval_views.agentic_browser_step", _challenge_step
    ), patch.dict(os.environ, _llm_env()):
        result = await asyncio.wait_for(
            llm.chat_with_delilah(
                "I gave you the code. 785547",
                "342385739952160769",
                _ReplyMessage(),
            ),
            timeout=30,
        )

    assert _ChallengeThenTypeClient.calls == 3
    assert "will not handle the code" not in result
    assert "signed in" in result
    # The nudge reached the model in-band, on the tool result it read.
    assert "verification/security step" in json.dumps(
        _ChallengeThenTypeClient.payloads[1]
    )


class _RelentlessChallengeObserveClient:
    """The model keeps observing the wall and never types: the turn must still
    end, or the nudge trades a refusal for an infinite loop."""

    calls = 0

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def stream(self, *args, **kwargs):
        type(self).calls += 1
        return _observe_response()


@pytest.mark.asyncio
async def test_relentless_verification_wall_observing_still_stops():
    _RelentlessChallengeObserveClient.calls = 0
    with patch.object(llm.httpx, "AsyncClient", _RelentlessChallengeObserveClient), patch(
        "src.bot.approval_views.agentic_browser_step", _challenge_step
    ), patch.dict(os.environ, _llm_env()):
        result = await asyncio.wait_for(
            llm.chat_with_delilah(
                "log me into paypal",
                "342385739952160769",
                _ReplyMessage(),
            ),
            timeout=30,
        )

    # One nudge, then the bounded refusal.
    assert _RelentlessChallengeObserveClient.calls == 2
    assert "will not handle the code" in result


@pytest.mark.parametrize(
    "args,expected",
    [
        # Gmail search syntax is a mailbox query no matter the wording.
        ({"query": "from:paypal newer_than:1d PayPal verification code"}, True),
        ({"query": "in:inbox"}, True),
        ({"query": "subject:verification newer_than:1d"}, True),
        # Explicit "code from my email/inbox" intent.
        ({"query": "PayPal verification code email site:gmail.com"}, True),
        ({"query": "get the one-time code from my inbox"}, True),
        # Batched queries are inspected too.
        ({"queries": ["from:paypal newer_than:1d", "paypal 2fa code"]}, True),
        # Real research queries must pass through untouched.
        ({"query": "best coffee grinder 2026"}, False),
        ({"query": "PayPal stock price"}, False),
        ({"query": "email marketing trends"}, False),
        ({"query": "how do one-time passwords work"}, False),
        ({}, False),
    ],
)
def test_search_web_mailbox_guard(args, expected):
    assert llm._web_query_is_mailbox(args) is expected


def test_gmail_only_turn_offers_read_and_browser_tools():
    """A Gmail-isolated turn must be able to READ a message (not just list
    headers) and act on an approved browser mission — otherwise a login that
    depends on an emailed verification code can never be completed."""
    schema_names = {t["function"]["name"] for t in llm.BOT_TOOLS_SCHEMA}
    for name in ("search_gmail", "read_gmail_message", "read_gmail_thread"):
        assert name in llm._GMAIL_ONLY_TOOLS, name
        assert name in schema_names, name
    assert "concierge_browser_step" in llm._GMAIL_ONLY_TOOLS
    # No typo'd/unknown tool names in the allowlist.
    assert llm._GMAIL_ONLY_TOOLS <= schema_names, llm._GMAIL_ONLY_TOOLS - schema_names
