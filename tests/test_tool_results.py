from src.services.tool_results import ToolResultEnvelope


def test_success_result_is_compact_and_receipt_provenant():
    result = ToolResultEnvelope.from_runtime(
        tool_name="search_web",
        call_id="call-1",
        receipt_id="receipt-1",
        ok=True,
        complete=True,
        status="confirmed",
        summary="One result found.",
        facts={"count": 1},
    )
    assert result.model_content() == "One result found."
    assert result.trace_fields()["evidence_refs"] == ["receipt-1"]
    assert result.trace_fields()["facts"] == {"count": 1}


def test_unknown_and_partial_results_cannot_be_presented_as_complete_success():
    unknown = ToolResultEnvelope.from_runtime(
        tool_name="send_push_alert",
        call_id="call-2",
        receipt_id="receipt-2",
        ok=False,
        complete=False,
        status="unknown",
        summary="External outcome could not be confirmed.",
    )
    partial = ToolResultEnvelope.from_runtime(
        tool_name="search_gmail",
        call_id="call-3",
        receipt_id="receipt-3",
        ok=True,
        complete=False,
        status="partial",
        summary="The first page was retrieved.",
    )
    assert unknown.model_content().startswith("UNKNOWN:")
    assert partial.complete is False
    assert partial.status == "partial"
