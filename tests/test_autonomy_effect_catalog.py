"""Completeness checks for the autonomy effect classification boundary."""

from __future__ import annotations

import pytest

from src.services import llm


@pytest.mark.parametrize(
    "tool_name",
    sorted(
        (llm.MUTATION_TOOLS | llm.FALLBACK_BLOCKED_TOOLS)
        - {"delegate_task", "task_plan"}
    ),
)
def test_mutation_and_fallback_blocked_catalog_never_classifies_as_read(tool_name):
    effect = llm._autonomy_contract_effect(tool_name, {})

    assert effect in {"mutation", "conditional", "external"}, (
        f"{tool_name!r} is in a mutation/blocklist catalog but classified {effect!r}"
    )


def test_resolved_mutation_aliases_never_classify_as_read():
    resolver = getattr(llm, "_resolve_tool_alias", None)
    aliases = getattr(llm, "TOOL_ALIASES", {})
    assert callable(resolver), "expected the tool alias resolver to be exposed"

    catalog = llm.MUTATION_TOOLS | llm.FALLBACK_BLOCKED_TOOLS
    checked = []
    for alias in sorted(aliases):
        canonical_name = resolver(alias)
        if canonical_name not in catalog:
            continue
        checked.append(alias)
        effect = llm._autonomy_contract_effect(alias, {})
        assert effect in {"mutation", "conditional", "external"}, (
            f"alias {alias!r} resolves to {canonical_name!r} but classified {effect!r}"
        )

    assert checked, "expected at least one alias resolving to a blocked tool"


@pytest.mark.parametrize("tool_name", ["delegate_task", "task_plan"])
def test_durable_task_controls_are_not_misclassified_as_external_mutations(tool_name):
    assert llm._autonomy_contract_effect(tool_name, {}) == "read"


@pytest.mark.parametrize(
    ("tool_name", "arguments", "expected_effect"),
    [
        ("canonicalize_transactions", {}, "mutation"),
        ("canonicalize_transactions", {"dry_run": False}, "mutation"),
        ("canonicalize_transactions", {"dry_run": True}, "read"),
        ("scan_and_auto_tag_deductions", {}, "read"),
        ("scan_and_auto_tag_deductions", {"auto_apply": False}, "read"),
        ("scan_and_auto_tag_deductions", {"auto_apply": True}, "mutation"),
    ],
)
def test_conditional_legacy_tool_effects_follow_explicit_arguments(
    tool_name, arguments, expected_effect
):
    assert llm._autonomy_contract_effect(tool_name, arguments) == expected_effect
