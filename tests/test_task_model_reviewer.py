import json

import pytest

from src.agent.task_model_reviewer import (
    MAX_CHECKS,
    MAX_ISSUES,
    MAX_ISSUE_CHARS,
    MAX_OBJECTIVE_CHARS,
    MAX_PROMPT_CHARS,
    MAX_RESPONSE_CHARS,
    MAX_REVIEW_RESPONSE_CHARS,
    build_review_prompt,
    parse_review_response,
    reviewer_enabled,
)


def test_reviewer_is_off_by_default_and_accepts_only_explicit_truthy_values(monkeypatch):
    monkeypatch.delenv("DELILAH_TASK_MODEL_REVIEWER", raising=False)
    assert reviewer_enabled() is False
    assert reviewer_enabled({}) is False
    for value in ("1", " TRUE ", "yes", "On"):
        assert reviewer_enabled({"DELILAH_TASK_MODEL_REVIEWER": value}) is True
    for value in ("", "0", "false", "no", "enabled", "2"):
        assert reviewer_enabled({"DELILAH_TASK_MODEL_REVIEWER": value}) is False


def test_prompt_projects_only_bounded_untrusted_text_and_check_summaries():
    prompt = build_review_prompt(
        "o" * (MAX_OBJECTIVE_CHARS + 50),
        "r" * (MAX_RESPONSE_CHARS + 50),
        [
            {
                "check_id": "c" * 500,
                "kind": "freshness",
                "passed": True,
                "reason_code": "verified",
                "tool_calls": [{"name": "delete_account", "arguments": {"x": 1}}],
                "tool_schema": {"name": "execute"},
                "private_context": "must not be copied",
            }
            for _ in range(MAX_CHECKS + 10)
        ],
    )

    assert len(prompt) <= MAX_PROMPT_CHARS
    assert "delete_account" not in prompt
    assert "tool_calls" not in prompt
    assert "tool_schema" not in prompt
    assert "must not be copied" not in prompt
    payload = json.loads(prompt.split("Input:\n", 1)[1])
    assert len(payload["objective"]) == MAX_OBJECTIVE_CHARS
    assert len(payload["final_response"]) == MAX_RESPONSE_CHARS
    assert len(payload["deterministic_checks"]) == MAX_CHECKS
    check = payload["deterministic_checks"][0]
    assert len(check["check_id"]) == 80
    assert check == {
        "check_id": "c" * 80,
        "kind": "freshness",
        "passed": True,
        "reason_code": "verified",
    }


def test_prompt_does_not_coerce_non_string_or_non_boolean_inputs():
    prompt = build_review_prompt(
        {"instruction": "not text"},
        ["not", "text"],
        [{"check_id": 42, "passed": 1, "kind": None, "reason_code": False}],
    )
    payload = json.loads(prompt.split("Input:\n", 1)[1])
    assert payload == {
        "objective": "",
        "final_response": "",
        "deterministic_checks": [{
            "check_id": "", "kind": "", "passed": None, "reason_code": "",
        }],
    }


def test_parse_valid_strict_response():
    raw = '{"passed":false,"issues":["Arithmetic check failed.","Output missing."]}'
    assert parse_review_response(raw) == {
        "passed": False,
        "issues": ["Arithmetic check failed.", "Output missing."],
    }
    assert parse_review_response('{"passed":true,"issues":[]}') == {
        "passed": True, "issues": [],
    }


@pytest.mark.parametrize("raw", [
    'prefix {"passed":true,"issues":[]}',
    '{"passed":true,"issues":[],"extra":1}',
    '{"passed":1,"issues":[]}',
    '{"passed":true,"issues":"none"}',
    '{"passed":true,"issues":[1]}',
    '{"passed":true,"issues":["conflict"]}',
    '{"passed":false,"issues":[]}',
    '{"passed":true,"issues":[" "]}',
    '{"passed":true,"passed":false,"issues":[]}',
    '{"passed":NaN,"issues":[]}',
    "null",
    "[]",
    "",
    None,
])
def test_parse_rejects_invalid_or_nonconforming_response(raw):
    assert parse_review_response(raw) is None


def test_parse_rejects_markdown_fences():
    fence = chr(96) * 3
    raw = f'{fence}json\n{{"passed":true,"issues":[]}}\n{fence}'
    assert parse_review_response(raw) is None


def test_parse_rejects_oversized_response_issue_count_and_issue_text():
    assert parse_review_response(" " * (MAX_REVIEW_RESPONSE_CHARS + 1)) is None
    too_many = json.dumps({"passed": True, "issues": ["issue"] * (MAX_ISSUES + 1)})
    assert parse_review_response(too_many) is None
    too_long = json.dumps({"passed": False, "issues": ["x" * (MAX_ISSUE_CHARS + 1)]})
    assert parse_review_response(too_long) is None


def test_parse_accepts_exact_issue_limits():
    issues = ["x" * MAX_ISSUE_CHARS for _ in range(MAX_ISSUES)]
    raw = json.dumps({"passed": False, "issues": issues})
    assert len(raw) <= MAX_REVIEW_RESPONSE_CHARS
    assert parse_review_response(raw) == {"passed": False, "issues": issues}
