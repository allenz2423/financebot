"""Bounded result fact contracts for high-risk/common tools."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ResultContract:
    tool_name: str
    fact_keys: tuple[str, ...]
    side_effect: str


RESULT_CONTRACTS = {
    "get_current_financial_position": ResultContract(
        "get_current_financial_position",
        (
            "available", "total_liquid", "net_worth", "net_cash", "date",
            "source_timestamp_utc", "data_stale",
        ),
        "read",
    ),
    "search_gmail": ResultContract(
        "search_gmail", ("message_id", "thread_id", "subject", "date"), "read"
    ),
    "read_gmail_message": ResultContract(
        "read_gmail_message", ("message_id", "thread_id", "subject", "from", "date"), "read"
    ),
    "search_web": ResultContract(
        "search_web", ("url", "title", "snippet"), "read"
    ),
    "scrape_rendered_page": ResultContract(
        "scrape_rendered_page", ("url", "title", "headings", "text"), "read"
    ),
    "send_push_alert": ResultContract(
        "send_push_alert", (), "mutation"
    ),
    "send_workspace_file": ResultContract(
        "send_workspace_file",
        (
            "status", "owner_id", "call_id", "receipt_id", "path", "filename",
            "sha256", "byte_size", "message_id", "valid_json", "valid_csv",
        ),
        "mutation",
    ),
    "save_memory": ResultContract(
        "save_memory", ("claim_id", "predicate"), "mutation"
    ),
    "delete_memory": ResultContract(
        "delete_memory", ("claim_id",), "mutation"
    ),
}


def contract_for(tool_name: str) -> ResultContract:
    return RESULT_CONTRACTS.get(
        str(tool_name),
        ResultContract(str(tool_name), (), "read"),
    )


def extract_facts(tool_name: str, raw_result: Any) -> dict[str, Any]:
    """Extract only contract-approved facts from JSON-like tool output."""

    contract = contract_for(tool_name)
    if not contract.fact_keys:
        return {}
    value = raw_result
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    if not isinstance(value, dict):
        return {}
    return {
        key: value[key]
        for key in contract.fact_keys
        if key in value and isinstance(value[key], (str, int, float, bool, type(None), list))
    }


__all__ = ["RESULT_CONTRACTS", "ResultContract", "contract_for", "extract_facts"]
