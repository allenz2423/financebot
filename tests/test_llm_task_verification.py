from __future__ import annotations

import sqlite3
import uuid
import hashlib
from datetime import datetime, timezone

import pytest

from src.agent.task_controller import TaskController
from src.agent.runtime import CURRENT_TASK_ID
from src.db.session_store import SessionStore
from src.services.llm import (
    _chat_with_delilah_impl,
    _run_read_only_task_review,
    _should_track_task_step,
    _tool_outcome_requires_turn_stop,
    _verify_task_final_output,
)
from src.services.tool_receipts import ReceiptStore


def _make_verifying_task(objective="summarize task"):
    connection = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
    store = SessionStore(connection=connection)
    receipts = ReceiptStore(connection)
    receipts.ensure_schema()
    task_id = f"verify-task-{uuid.uuid4().hex}"
    task = store.create_task_run("owner", "verify-session", objective, task_id=task_id)
    controller = TaskController(store)
    controller.transition_phase("owner", task_id, "planning", next_action="plan")
    controller.transition_phase("owner", task_id, "verifying", next_action="verify_final_output")
    return connection, store, receipts, task_id


def _record_confirmed_tool_step(
    store, receipts, task_id, *, tool_name, step_id, result=None,
):
    turn_id = f"turn-{step_id}"
    store.begin_turn("owner", "verify-session", turn_id=turn_id)
    arguments = {"path": "report.csv"} if tool_name == "send_workspace_file" else {"query": "recent"}
    call_id = f"call-{step_id}"
    call = store.record_tool_call(
        "owner", "verify-session", tool_name=tool_name, arguments=arguments,
        call_id=call_id, turn_id=turn_id, status="running",
    )
    receipt_id = f"receipt-{step_id}"
    receipts.prepare(
        receipt_id=receipt_id, call_id=call["call_key"], user_id="owner",
        turn_id=turn_id, round_id=1, tool_name=tool_name, origin="native",
        arguments=arguments,
    )
    receipts.start(receipt_id)
    receipts.finish(
        receipt_id, status="confirmed", ok=True, complete=True,
        result_summary="workspace file sent" if tool_name == "send_workspace_file" else "headers received",
    )
    if tool_name == "send_workspace_file" and result is None:
        content = b"report contents\n"
        result = {
            "status": "sent", "path": "report.csv", "filename": "report.csv",
            "sha256": hashlib.sha256(content).hexdigest(), "byte_size": len(content),
            "message_id": "discord-message-1", "owner_id": "owner",
            "call_id": call_id, "receipt_id": receipt_id,
            "valid_csv": True,
        }
    store.finish_tool_call(
        "owner", "verify-session", call_id, status="succeeded", result=result
    )
    step_order = len(store.get_task("owner", task_id, include_steps=True).get("steps", []))
    store.add_task_step(
        "owner", task_id, step_order, step_id=step_id, status="succeeded",
        tool_call_id=int(call["id"]), receipt_id=receipt_id,
    )
    return turn_id, call, receipt_id


def test_final_output_gate_persists_pass_before_delivery():
    _connection, store, receipts, task_id = _make_verifying_task()

    result = _verify_task_final_output(
        store, receipts, "owner", task_id, "turn-1", "A grounded summary."
    )

    assert result["outcome"]["passed"] is True
    task = store.get_task("owner", task_id)
    assert task["phase"] == "verifying"
    assert task["status"] == "queued"
    assert any(event["event_type"] == "task.verification_passed" for event in task["events"])
    assert task["steps"] == []


def test_all_non_control_tool_calls_are_linked_before_task_dispatch():
    for tool_name in (
        "get_current_financial_position", "query_spending", "get_subscriptions",
        "auto_reconcile_ledger", "send_workspace_file", "future_financial_reader",
    ):
        assert _should_track_task_step(
            "task-1", tool_name, has_plan=False, delegated=False,
        ) is True, tool_name
    for control_name in ("task_plan", "task_list", "await_user", "end_turn"):
        assert _should_track_task_step(
            "task-1", control_name, has_plan=False, delegated=False,
        ) is False, control_name


@pytest.mark.parametrize("status", ["unknown", "UNKNOWN"])
def test_ambiguous_receipt_stops_dispatch_and_retry(status):
    assert _tool_outcome_requires_turn_stop(status) is True


@pytest.mark.parametrize("status", ["confirmed", "failed", "started", "prepared"])
def test_nonambiguous_receipt_status_does_not_trigger_unknown_stop(status):
    assert _tool_outcome_requires_turn_stop(status) is False


def test_json_export_request_requires_artifact_delivery_not_success_prose():
    _connection, store, receipts, task_id = _make_verifying_task(
        "Export my transactions as JSON."
    )

    result = _verify_task_final_output(
        store, receipts, "owner", task_id, "turn-export", "Export complete."
    )

    assert result["outcome"]["passed"] is False
    assert {
        "requested_artifact_delivery", "requested_artifact_validation",
    }.issubset({check["check_id"] for check in result["outcome"]["checks"]})


@pytest.mark.parametrize(("filename", "valid_json", "expected_pass"), [
    ("transactions.csv", True, False),
    ("transactions.json", False, False),
    ("transactions.json", True, True),
])
def test_json_export_requires_json_filename_and_valid_delivered_bytes(
    filename, valid_json, expected_pass,
):
    _connection, store, receipts, task_id = _make_verifying_task(
        "Export my transactions as JSON."
    )
    payload = b"not actually json" if not valid_json else b'{"transactions":[]}'
    turn_id, _call, _receipt = _record_confirmed_tool_step(
        store, receipts, task_id, tool_name="send_workspace_file", step_id="json-export",
        result={
            "status": "sent", "path": filename, "filename": filename,
            "sha256": hashlib.sha256(payload).hexdigest(), "byte_size": len(payload),
            "message_id": "discord-json-1", "owner_id": "owner",
            "call_id": "call-json-export", "receipt_id": "receipt-json-export",
            "valid_json": valid_json,
        },
    )

    result = _verify_task_final_output(
        store, receipts, "owner", task_id, turn_id, "I exported the transactions.",
    )

    assert result["outcome"]["passed"] is expected_pass
    artifact_check = next(
        check for check in result["outcome"]["checks"]
        if check["check_id"] == "requested_artifact_validation"
    )
    assert artifact_check["passed"] is expected_pass


def test_json_export_selects_matching_file_when_task_sends_multiple_artifacts():
    _connection, store, receipts, task_id = _make_verifying_task(
        "Export my transactions as JSON."
    )
    for step_id, filename, payload, valid_json in (
        ("csv-copy", "transactions.csv", b"a,b\n1,2\n", True),
        ("json-copy", "transactions.json", b'{"transactions":[]}', True),
    ):
        _record_confirmed_tool_step(
            store, receipts, task_id, tool_name="send_workspace_file", step_id=step_id,
            result={
                "status": "sent", "path": filename, "filename": filename,
                "sha256": hashlib.sha256(payload).hexdigest(), "byte_size": len(payload),
                "message_id": f"discord-{step_id}", "owner_id": "owner",
                "call_id": f"call-{step_id}", "receipt_id": f"receipt-{step_id}",
                "valid_json": valid_json,
            },
        )
    turn_id = f"turn-json-copy"

    result = _verify_task_final_output(
        store, receipts, "owner", task_id, turn_id, "The JSON export is attached.",
    )

    artifact_check = next(
        check for check in result["outcome"]["checks"]
        if check["check_id"] == "requested_artifact_validation"
    )
    assert result["outcome"]["passed"] is True
    assert artifact_check["passed"] is True


@pytest.mark.parametrize(("payload", "valid_csv", "expected_pass"), [
    (b"date,amount\n2026-01-01,12.50\n", True, True),
    (b'{"transactions":[]}', False, False),
    (b"123", False, False),
])
def test_csv_export_requires_csv_bytes_not_only_csv_extension(
    payload, valid_csv, expected_pass,
):
    _connection, store, receipts, task_id = _make_verifying_task(
        "Export my transactions as CSV."
    )
    turn_id, _call, _receipt = _record_confirmed_tool_step(
        store, receipts, task_id, tool_name="send_workspace_file", step_id="csv-export",
        result={
            "status": "sent", "path": "transactions.csv", "filename": "transactions.csv",
            "sha256": hashlib.sha256(payload).hexdigest(), "byte_size": len(payload),
            "message_id": "discord-csv-1", "owner_id": "owner",
            "call_id": "call-csv-export", "receipt_id": "receipt-csv-export",
            "valid_csv": valid_csv,
        },
    )

    result = _verify_task_final_output(
        store, receipts, "owner", task_id, turn_id, "I exported the transactions.",
    )

    assert result["outcome"]["passed"] is expected_pass
    check = next(
        check for check in result["outcome"]["checks"]
        if check["check_id"] == "requested_artifact_validation"
    )
    assert check["passed"] is expected_pass


@pytest.mark.asyncio
async def test_chat_refuses_to_run_without_durable_task_context():
    result = await _chat_with_delilah_impl("Check my balance", "owner", None)

    assert "durable task storage is unavailable" in result
    assert "No tools were run" in result
    assert CURRENT_TASK_ID.get() is None


def test_optional_model_review_is_persisted_as_an_independent_gate():
    _connection, store, receipts, task_id = _make_verifying_task(
        "Search and summarize two recent emails."
    )
    turn_id, _call, _receipt = _record_confirmed_tool_step(
        store, receipts, task_id, tool_name="search_gmail", step_id="mail-search",
    )
    _record_confirmed_tool_step(
        store, receipts, task_id, tool_name="read_gmail_message", step_id="mail-read",
    )

    result = _verify_task_final_output(
        store, receipts, "owner", task_id, turn_id, "Two messages were found.",
        model_review={"passed": False, "issues": ["The response omits the requested dates."]},
    )

    assert result["outcome"]["passed"] is False
    assert any(
        check["check_id"] == "read_only_model_review"
        and check["reason_code"] == "model_review_rejected"
        for check in result["outcome"]["checks"]
    )
    task = store.get_task("owner", task_id)
    assert task["status"] == "partial"
    assert any(event["event_type"] == "task.verification_failed" for event in task["events"])


def test_model_review_cannot_replace_required_deterministic_financial_checks():
    _connection, store, receipts, task_id = _make_verifying_task(
        "Can you tell me my balance?"
    )

    result = _verify_task_final_output(
        store, receipts, "owner", task_id, "turn-no-source", "Your balance is $0.00.",
        model_review={"passed": True, "issues": []},
    )

    assert result["outcome"]["passed"] is False
    assert any(
        check["kind"] == "freshness" and not check["passed"]
        for check in result["outcome"]["checks"]
    )


@pytest.mark.asyncio
async def test_optional_reviewer_invocation_is_bounded_and_has_no_tools():
    calls = []

    async def fake_runner(messages, *, force_no_tools, num_predict):
        calls.append((messages, force_no_tools, num_predict))
        return '{"passed":true,"issues":[]}', []

    result = await _run_read_only_task_review(
        fake_runner,
        "Summarize the records.",
        "The summary is complete.",
        [{"check_id": "receipt", "passed": True, "reason_code": "verified"}],
    )

    assert result == {"passed": True, "issues": []}
    assert len(calls) == 1
    messages, force_no_tools, num_predict = calls[0]
    assert force_no_tools is True
    assert num_predict == 350
    assert len(messages) == 2
    assert "do not call tools" in messages[0]["content"].casefold()
    assert "receipt" in messages[1]["content"]


@pytest.mark.asyncio
async def test_optional_reviewer_unavailable_fails_closed():
    async def broken_runner(*_args, **_kwargs):
        raise RuntimeError("provider unavailable")

    result = await _run_read_only_task_review(
        broken_runner, "objective", "answer", [],
    )

    assert result == {
        "passed": False,
        "issues": ["read_only_reviewer_unavailable_or_invalid"],
    }


def test_final_output_gate_accepts_matching_confirmed_receipt_and_call():
    _connection, store, receipts, task_id = _make_verifying_task()
    turn_id = "turn-with-confirmed-call"
    store.begin_turn("owner", "verify-session", turn_id=turn_id)
    arguments = {"query": "recent"}
    call = store.record_tool_call(
        "owner", "verify-session", tool_name="search_gmail", arguments=arguments,
        call_id="call-confirmed", turn_id=turn_id, status="running",
    )
    receipt_id = "receipt-confirmed"
    receipts.prepare(
        receipt_id=receipt_id, call_id=call["call_key"], user_id="owner",
        turn_id=turn_id, round_id=1, tool_name="search_gmail", origin="native",
        arguments=arguments,
    )
    receipts.start(receipt_id)
    receipts.finish(
        receipt_id, status="confirmed", ok=True, complete=True,
        result_summary="message headers received",
    )
    store.finish_tool_call("owner", "verify-session", "call-confirmed", status="succeeded")
    store.add_task_step(
        "owner", task_id, 0, step_id="step-confirmed", status="succeeded",
        tool_call_id=int(call["id"]), receipt_id=receipt_id,
    )

    result = _verify_task_final_output(
        store, receipts, "owner", task_id, turn_id, "A grounded summary."
    )

    assert result["outcome"]["passed"] is True
    assert result["outcome"]["checks"][0]["reason_code"] == "verified"


def test_final_output_gate_blocks_missing_receipt_and_creates_repair_step():
    _connection, store, receipts, task_id = _make_verifying_task()
    receipt_id = f"receipt-{uuid.uuid4().hex}"
    receipts.prepare(
        receipt_id=receipt_id,
        call_id="call-without-durable-link",
        user_id="owner",
        turn_id="turn-2",
        round_id=1,
        tool_name="search_gmail",
        origin="native",
        arguments={"query": "recent"},
    )
    store.add_task_step(
        "owner", task_id, 0, step_id="step-with-missing-call",
        status="succeeded", receipt_id=receipt_id,
    )

    result = _verify_task_final_output(
        store, receipts, "owner", task_id, "turn-2", "The requested work is done."
    )

    assert result["outcome"]["passed"] is False
    task = store.get_task("owner", task_id)
    assert task["status"] == "partial"
    assert task["phase"] == "partial"
    repairs = [step for step in task["steps"] if step.get("next_action") == "repair_failed_checks"]
    assert len(repairs) == 1
    assert repairs[0]["status"] == "ready"
    assert "task_receipt_0_step-with-missing-call" in repairs[0]["completion_criteria"]
    failed = [event for event in task["events"] if event["event_type"] == "task.verification_failed"]
    assert len(failed) == 1
    assert "call-without-durable-link" not in str(failed[0]["payload"])


def test_financial_summary_without_typed_freshness_or_arithmetic_fails_and_repairs():
    _connection, store, receipts, task_id = _make_verifying_task(
        "Summarize my financial position and budget."
    )
    turn_id, _call, _receipt_id = _record_confirmed_tool_step(
        store, receipts, task_id, tool_name="get_current_financial_position",
        step_id="financial-position",
    )

    result = _verify_task_final_output(
        store, receipts, "owner", task_id, turn_id,
        "Your financial position is in good shape.",
    )

    assert result["outcome"]["passed"] is False
    assert any(check["kind"] == "freshness" and not check["passed"] for check in result["outcome"]["checks"])
    assert any(check["kind"] == "arithmetic" and not check["passed"] for check in result["outcome"]["checks"])
    task = store.get_task("owner", task_id)
    assert task["status"] == "partial"
    assert task["phase"] == "partial"
    repairs = [step for step in task["steps"] if step.get("next_action") == "repair_failed_checks"]
    assert len(repairs) == 1
    assert repairs[0]["status"] == "ready"


def test_financial_tool_requires_freshness_even_if_task_objective_is_generic():
    _connection, store, receipts, task_id = _make_verifying_task("summarize task")
    turn_id, _call, _receipt_id = _record_confirmed_tool_step(
        store, receipts, task_id, tool_name="get_current_financial_position",
        step_id="unclassified-financial-read",
    )

    result = _verify_task_final_output(
        store, receipts, "owner", task_id, turn_id, "Here is the result."
    )

    assert result["outcome"]["passed"] is False
    assert any(check["kind"] == "freshness" and not check["passed"] for check in result["outcome"]["checks"])


def test_multi_source_financial_reads_require_arithmetic_proof():
    _connection, store, receipts, task_id = _make_verifying_task("summarize task")
    turn_id, _call, _receipt_id = _record_confirmed_tool_step(
        store, receipts, task_id, tool_name="get_current_financial_position",
        step_id="position-read",
    )
    _record_confirmed_tool_step(
        store, receipts, task_id, tool_name="get_cash_flow_summary",
        step_id="cash-flow-read",
    )

    result = _verify_task_final_output(
        store, receipts, "owner", task_id, turn_id, "Here is the result."
    )

    assert result["outcome"]["passed"] is False
    assert any(check["kind"] == "arithmetic" and not check["passed"] for check in result["outcome"]["checks"])


def test_fresh_position_does_not_mask_missing_freshness_from_another_financial_source():
    _connection, store, receipts, task_id = _make_verifying_task("summarize task")
    fresh_result = {
        "available": True, "data_stale": False,
        "source_timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "total_liquid": "100.00", "net_cash": "80.00",
        "verification_snapshot": {
            "checking_balance": "100.00", "total_credit_debt": "20.00",
            "net_cash": "80.00",
        },
    }
    turn_id, _call, _receipt_id = _record_confirmed_tool_step(
        store, receipts, task_id, tool_name="get_current_financial_position",
        step_id="fresh-position", result=fresh_result,
    )
    _record_confirmed_tool_step(
        store, receipts, task_id, tool_name="get_cash_flow_summary",
        step_id="unsupported-cash-flow", result="Cash flow text without typed provenance",
    )

    result = _verify_task_final_output(
        store, receipts, "owner", task_id, turn_id, "Net cash is $80.00."
    )

    assert result["outcome"]["passed"] is False
    freshness_checks = [
        check for check in result["outcome"]["checks"]
        if check["kind"] == "freshness"
    ]
    assert len(freshness_checks) == 2
    assert sum(check["passed"] for check in freshness_checks) == 1


def test_untrusted_wrong_currency_claim_fails_despite_valid_source_arithmetic():
    _connection, store, receipts, task_id = _make_verifying_task(
        "Summarize my financial position."
    )
    result_data = {
        "available": True, "data_stale": False,
        "source_timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "total_liquid": "100.00", "net_cash": "80.00",
        "verification_snapshot": {
            "checking_balance": "100.00", "total_credit_debt": "20.00",
            "net_cash": "80.00",
        },
    }
    turn_id, _call, _receipt_id = _record_confirmed_tool_step(
        store, receipts, task_id, tool_name="get_current_financial_position",
        step_id="valid-position", result=result_data,
    )

    result = _verify_task_final_output(
        store, receipts, "owner", task_id, turn_id,
        "Your net cash is $999.00.",
    )

    assert result["outcome"]["passed"] is False
    assert any(
        check["check_id"] == "financial_numeric_claims"
        and check["reason_code"] == "unsupported_numeric_claim"
        for check in result["outcome"]["checks"]
    )


@pytest.mark.parametrize("objective", [
    "What is my current balance?",
    "Can you tell me my balance?",
    "How much money do I have?",
])
def test_direct_financial_balance_question_without_tool_evidence_fails_closed(objective):
    _connection, store, receipts, task_id = _make_verifying_task(
        objective
    )

    result = _verify_task_final_output(
        store, receipts, "owner", task_id, "turn-no-balance-source", "Your balance is $0.00."
    )

    assert result["outcome"]["passed"] is False
    assert any(
        check["kind"] == "freshness" and not check["passed"]
        for check in result["outcome"]["checks"]
    )


def test_financial_read_tools_each_get_a_source_freshness_check():
    _connection, store, receipts, task_id = _make_verifying_task(
        "Review my recent finances."
    )
    turn_id, _call, _receipt_id = _record_confirmed_tool_step(
        store, receipts, task_id, tool_name="query_spending", step_id="spending-read",
    )
    turn_id, _call, _receipt_id = _record_confirmed_tool_step(
        store, receipts, task_id, tool_name="get_subscriptions", step_id="subscriptions-read",
    )

    result = _verify_task_final_output(
        store, receipts, "owner", task_id, turn_id, "I reviewed the available information."
    )

    freshness_checks = [
        check for check in result["outcome"]["checks"] if check["kind"] == "freshness"
    ]
    assert len(freshness_checks) == 2
    assert all(not check["passed"] for check in freshness_checks)


def test_auto_reconcile_tool_requires_explicit_reconciliation_validation():
    _connection, store, receipts, task_id = _make_verifying_task(
        "Review my ledger."
    )
    turn_id, _call, _receipt_id = _record_confirmed_tool_step(
        store, receipts, task_id, tool_name="auto_reconcile_ledger", step_id="auto-reconcile",
    )

    result = _verify_task_final_output(
        store, receipts, "owner", task_id, turn_id, "Reconciliation complete."
    )

    assert any(
        check["check_id"] == "reconciliation_result_validation" and not check["passed"]
        for check in result["outcome"]["checks"]
    )


def test_currency_scanner_rejects_unsupported_currency_markers():
    currencies = (
        "Your balance is €80.00.", "Your balance is £80.00.",
        "Your balance is ¥80.00.", "Your balance is ₹80.00.",
        "Your balance is EUR 80.00.", "Your balance is 80.00 GBP.",
        "Your balance is C$80.00.", "Your balance is 80.00 CAD.",
        "Your balance is CAD $80.00.", "Your balance is CAD$80.00.",
    )
    for index, text in enumerate(currencies):
        _connection, store, receipts, task_id = _make_verifying_task(
            "Summarize my financial position."
        )
        turn_id, _call, _receipt_id = _record_confirmed_tool_step(
            store, receipts, task_id, tool_name="get_current_financial_position",
            step_id=f"unsupported-currency-position-{index}", result={
                "available": True, "data_stale": False,
                "source_timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "total_liquid": "100.00", "net_cash": "80.00",
                "verification_snapshot": {
                    "checking_balance": "100.00", "total_credit_debt": "20.00",
                    "net_cash": "80.00",
                },
            },
        )
        result = _verify_task_final_output(
            store, receipts, "owner", task_id, turn_id, text
        )
        numeric = next(
            check for check in result["outcome"]["checks"]
            if check["check_id"] == "financial_numeric_claims"
        )
        assert numeric["passed"] is False, text
        assert numeric["reason_code"] == "unsupported_currency_claim", text


def test_currency_scanner_rejects_more_than_two_decimal_places_without_truncation():
    _connection, store, receipts, task_id = _make_verifying_task(
        "Summarize my financial position."
    )
    turn_id, _call, _receipt_id = _record_confirmed_tool_step(
        store, receipts, task_id, tool_name="get_current_financial_position",
        step_id="malformed-currency-position", result={
            "available": True, "data_stale": False,
            "source_timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "total_liquid": "100.99", "net_cash": "80.00",
            "verification_snapshot": {
                "checking_balance": "100.99", "total_credit_debt": "20.99",
                "net_cash": "80.00",
            },
        },
    )

    result = _verify_task_final_output(
        store, receipts, "owner", task_id, turn_id, "Your balance is $100.999."
    )

    numeric = next(
        check for check in result["outcome"]["checks"]
        if check["check_id"] == "financial_numeric_claims"
    )
    assert numeric["passed"] is False
    assert numeric["reason_code"] == "malformed_number"


@pytest.mark.parametrize("claim", [
    "Net cash is 100.00.",
    "Net cash is USD -100.00.",
    "Net cash is 100.00 USD.",
])
def test_labeled_financial_claims_without_standard_currency_prefix_fail_closed(claim):
    _connection, store, receipts, task_id = _make_verifying_task(
        "Summarize my financial position."
    )
    turn_id, _call, _receipt_id = _record_confirmed_tool_step(
        store, receipts, task_id, tool_name="get_current_financial_position",
        step_id="untrusted-bare-number", result={
            "available": True, "data_stale": False,
            "source_timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "total_liquid": "100.00", "net_cash": "80.00",
            "verification_snapshot": {
                "checking_balance": "100.00", "total_credit_debt": "20.00",
                "net_cash": "80.00",
            },
        },
    )

    result = _verify_task_final_output(
        store, receipts, "owner", task_id, turn_id, claim,
    )

    numeric = next(
        check for check in result["outcome"]["checks"]
        if check["check_id"] == "financial_numeric_claims"
    )
    assert numeric["passed"] is False


def test_currency_value_cannot_be_relabelled_as_a_different_financial_fact():
    _connection, store, receipts, task_id = _make_verifying_task(
        "Summarize my financial position."
    )
    result_data = {
        "available": True, "data_stale": False,
        "source_timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "total_liquid": "100.00", "net_cash": "80.00",
        "verification_snapshot": {
            "checking_balance": "100.00", "total_credit_debt": "20.00",
            "net_cash": "80.00",
        },
    }
    turn_id, _call, _receipt_id = _record_confirmed_tool_step(
        store, receipts, task_id, tool_name="get_current_financial_position",
        step_id="labeled-claim-position", result=result_data,
    )

    result = _verify_task_final_output(
        store, receipts, "owner", task_id, turn_id,
        "Your net cash is $100.00.",
    )

    assert result["outcome"]["passed"] is False
    numeric_check = next(
        check for check in result["outcome"]["checks"]
        if check["check_id"] == "financial_numeric_claims"
    )
    assert numeric_check["reason_code"] == "unsupported_numeric_claim"


def test_reconciliation_tool_requires_freshness_and_arithmetic_evidence():
    _connection, store, receipts, task_id = _make_verifying_task(
        "Reconcile my statement."
    )
    position_result = {
        "available": True, "data_stale": False,
        "source_timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "total_liquid": "100.00", "net_cash": "80.00",
        "verification_snapshot": {
            "checking_balance": "100.00", "total_credit_debt": "20.00",
            "net_cash": "80.00",
        },
    }
    turn_id, _call, _receipt_id = _record_confirmed_tool_step(
        store, receipts, task_id, tool_name="get_current_financial_position",
        step_id="unrelated-fresh-position", result=position_result,
    )
    turn_id, _call, _receipt_id = _record_confirmed_tool_step(
        store, receipts, task_id,
        tool_name="reconcile_expected_and_planned_transactions",
        step_id="reconciliation",
    )

    result = _verify_task_final_output(
        store, receipts, "owner", task_id, turn_id, "Reconciliation complete."
    )

    assert result["outcome"]["passed"] is False
    assert any(check["kind"] == "freshness" and not check["passed"] for check in result["outcome"]["checks"])
    assert any(check["kind"] == "arithmetic" and not check["passed"] for check in result["outcome"]["checks"])
    assert any(
        check["check_id"] == "reconciliation_result_validation" and not check["passed"]
        for check in result["outcome"]["checks"]
    )


def test_current_position_passes_with_linked_fresh_snapshot_and_exact_arithmetic():
    _connection, store, receipts, task_id = _make_verifying_task(
        "Summarize my financial position."
    )
    now = datetime.now(timezone.utc).isoformat()
    turn_id, _call, _receipt_id = _record_confirmed_tool_step(
        store, receipts, task_id, tool_name="get_current_financial_position",
        step_id="verified-position",
        result={
            "available": True,
            "data_stale": False,
            "source_timestamp_utc": now,
            "total_liquid": "100.00",
            "net_cash": "80.00",
            "verification_snapshot": {
                "checking_balance": "100.00",
                "total_credit_debt": "20.00",
                "net_cash": "80.00",
            },
        },
    )

    result = _verify_task_final_output(
        store, receipts, "owner", task_id, turn_id, "Net cash is $80.00."
    )

    assert result["outcome"]["passed"] is True, result["outcome"]
    assert {check["kind"] for check in result["outcome"]["checks"]} >= {
        "freshness", "arithmetic",
    }


def test_financial_position_source_timestamp_and_snapshot_are_read_only_and_fail_closed(monkeypatch):
    from src.db import queries

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE transactions (
            id INTEGER PRIMARY KEY, user_id TEXT, merchant TEXT,
            total_liquid REAL, net_cash REAL,
            all_balances TEXT, date TEXT
        );
        CREATE TABLE financial_snapshots (
            user_id TEXT, captured_at TEXT, checking_balance REAL,
            total_credit_debt REAL, net_cash REAL
        );
    """)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    connection.execute(
        "INSERT INTO financial_snapshots VALUES (?, ?, ?, ?, ?)",
        ("owner", timestamp, 100.0, 20.0, 80.0),
    )
    connection.execute(
        """INSERT INTO transactions(
               user_id, merchant, total_liquid, net_cash, all_balances, date
           ) VALUES (?, ?, ?, ?, ?, ?)""",
        ("owner", "System Balance Sync", 100.0, 80.0, "Checking: $100.00", timestamp),
    )
    monkeypatch.setattr(queries, "c", connection.cursor())

    current = queries.get_current_financial_position(user_id="owner")
    assert current["data_stale"] is False
    assert current["source_timestamp_utc"].endswith("+00:00")
    assert current["verification_snapshot"]["net_cash"] == 80.0

    connection.execute(
        "UPDATE transactions SET date='invalid' WHERE user_id='owner'"
    )
    malformed = queries.get_current_financial_position(user_id="owner")
    assert malformed["data_stale"] is True
    assert malformed["source_timestamp_utc"] is None
    assert malformed["verification_snapshot"] is None
    connection.close()


def test_artifact_creation_without_linked_send_workspace_file_fails():
    _connection, store, receipts, task_id = _make_verifying_task(
        "Create a spreadsheet report from the supplied records."
    )
    turn_id, _call, _receipt_id = _record_confirmed_tool_step(
        store, receipts, task_id, tool_name="search_gmail", step_id="unrelated-search",
    )

    result = _verify_task_final_output(
        store, receipts, "owner", task_id, turn_id,
        "I created the spreadsheet report.",
    )

    assert result["outcome"]["passed"] is False
    assert any(
        check["check_id"] == "requested_artifact_delivery" and not check["passed"]
        for check in result["outcome"]["checks"]
    )
    task = store.get_task("owner", task_id)
    assert task["status"] == "partial"
    repairs = [step for step in task["steps"] if step.get("next_action") == "repair_failed_checks"]
    assert len(repairs) == 1


def test_artifact_requires_delivery_and_integrity_evidence_for_exact_output():
    _connection, store, receipts, task_id = _make_verifying_task(
        "Create report.csv from the supplied records."
    )
    turn_id, call, receipt_id = _record_confirmed_tool_step(
        store, receipts, task_id, tool_name="send_workspace_file", step_id="send-report",
    )

    result = _verify_task_final_output(
        store, receipts, "owner", task_id, turn_id,
        "The spreadsheet report was sent.",
    )

    assert result["outcome"]["passed"] is True, result["outcome"]
    check_results = {check["check_id"]: check for check in result["outcome"]["checks"]}
    assert check_results["requested_artifact_delivery"]["passed"] is True
    assert check_results["requested_artifact_validation"]["passed"] is True
    task = store.get_task("owner", task_id)
    assert task["status"] == "queued"
    step = next(step for step in task["steps"] if step["step_id"] == "send-report")
    assert step["receipt_id"] == receipt_id
    linked_call = store.get_tool_call("owner", int(step["tool_call_id"]))
    assert linked_call["id"] == call["id"]
    assert linked_call["call_key"] == call["call_key"]
    linked_receipt = receipts.get(receipt_id)
    assert linked_receipt.user_id == "owner"
    assert linked_receipt.call_id == call["call_key"]
    assert linked_receipt.tool_name == "send_workspace_file"
    assert linked_receipt.status == "confirmed"


def test_artifact_delivery_fails_when_exact_requested_path_does_not_match():
    _connection, store, receipts, task_id = _make_verifying_task(
        "Create report.csv from the supplied records."
    )
    result_data = {
        "status": "sent", "path": "different.csv", "filename": "different.csv",
        "sha256": hashlib.sha256(b"report contents").hexdigest(),
        "byte_size": len(b"report contents"), "message_id": "discord-message-1",
        "owner_id": "owner", "call_id": "call-send-wrong-path",
        "receipt_id": "receipt-send-wrong-path",
    }
    turn_id, _call, _receipt_id = _record_confirmed_tool_step(
        store, receipts, task_id, tool_name="send_workspace_file",
        step_id="send-wrong-path", result=result_data,
    )

    result = _verify_task_final_output(
        store, receipts, "owner", task_id, turn_id, "The requested report was sent."
    )

    assert result["outcome"]["passed"] is False
    assert any(
        check["check_id"] == "requested_artifact_validation"
        and check["reason_code"] == "artifact_integrity_invalid"
        for check in result["outcome"]["checks"]
    )
