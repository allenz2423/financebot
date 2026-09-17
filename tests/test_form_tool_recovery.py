"""Regression tests for recovering degraded tool-call output from the advisor.

When the free-tier model emits ONLY the arguments object of a tool
(no {"name": ...} envelope, no <function=...> wrapper), the fallback parser
must still dispatch it. This was observed live: the model emitted a bare
{"form_schema": {...}} JSON, the parser saw no tool name, and the raw JSON
was sent to the user as the final answer ("NO TOOLS - PLAIN TEXT RESPONSE").
"""

import json

from src.services.llm import _bind_bare_argument_shape


# Exact raw payload from the live failing turn (form tool, bare arguments).
BARE_FORM_PAYLOAD = json.loads(
    '{"form_schema":{"title":"Quick VOYXACT Knowledge Check",'
    '"purpose":"A short test of the key facts about VOYXACT and its 28-day dosing schedule.",'
    '"pages":[{"page_title":"Medication Check","questions":['
    '{"key":"voyxact_indication","label":"What condition is VOYXACT prescribed to treat?",'
    '"input_type":"paragraph","required":true,"required":true,'
    '"help_text":"Answer in your own words."},'
    '{"key":"voyxact_dosing_interval","label":"How often is VOYXACT administered?",'
    '"input_type":"short","required":true,"help_text":"Include the number of days."}]}]}}'
)


def test_bare_form_schema_binds_to_request_user_form():
    bound = _bind_bare_argument_shape(BARE_FORM_PAYLOAD)
    assert bound is not None
    name, args = bound
    assert name == "request_user_form"
    # the whole object is passed through as the tool arguments
    assert args["form_schema"]["title"] == "Quick VOYXACT Knowledge Check"


def test_bare_form_schema_keeps_pages_and_questions():
    bound = _bind_bare_argument_shape(BARE_FORM_PAYLOAD)
    name, args = bound
    pages = args["form_schema"]["pages"]
    assert len(pages) == 1
    assert len(pages[0]["questions"]) == 2


def test_non_matching_objects_are_not_bound():
    assert _bind_bare_argument_shape({"query": "hello"}) is None
    assert _bind_bare_argument_shape({"url": "https://x"}) is None
    assert _bind_bare_argument_shape({"form_schema": "not-a-dict"}) is None
    assert _bind_bare_argument_shape({}) is None
    assert _bind_bare_argument_shape({"name": "search_web", "arguments": {}}) is None


def test_bare_form_schema_passes_schema_validation_and_splits_pages():
    # The live payload had 6 questions on one page; the schema now splits
    # over-long pages so the recovered call renders instead of erroring back.
    from src.services.form_flow import validate_form_schema

    bound = _bind_bare_argument_shape(BARE_FORM_PAYLOAD)
    title, purpose, pages = validate_form_schema(bound[1]["form_schema"])
    assert title == "Quick VOYXACT Knowledge Check"
    assert all(len(p["questions"]) <= 5 for p in pages)