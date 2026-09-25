from src.services.tool_execution_evidence import tool_result_indicates_failure


def test_tool_result_evidence_fails_closed_for_missing_or_error_results():
    assert tool_result_indicates_failure(None)
    assert tool_result_indicates_failure("")
    assert tool_result_indicates_failure("Unknown tool 'sync_plaid_accounting'.")
    assert tool_result_indicates_failure("Error executing tool 'sync_plaid_accounting'.")
    assert tool_result_indicates_failure("Plaid accounting workflow failed: timeout")
    assert tool_result_indicates_failure("Fetch failed: Connection timed out")
    assert tool_result_indicates_failure("Registry write reported success but verification failed")
    assert tool_result_indicates_failure({"ok": False, "error": "denied"})


def test_tool_result_evidence_accepts_non_error_dispatch_results():
    assert not tool_result_indicates_failure("Plaid accounting workflow complete.")
    assert not tool_result_indicates_failure({"ok": True, "status": "confirmed"})
