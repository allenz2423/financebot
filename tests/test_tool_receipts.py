import sqlite3

import pytest

from src.services.tool_receipts import (
    ReceiptLifecycleError,
    ReceiptStore,
    terminal_status_for_outcome,
)


def test_ambiguous_mutation_failure_is_not_classified_as_definite_failure():
    assert terminal_status_for_outcome(succeeded=False, ambiguous=True) == "unknown"
    assert terminal_status_for_outcome(succeeded=False, ambiguous=False) == "failed"
    assert terminal_status_for_outcome(succeeded=True, ambiguous=True) == "confirmed"


@pytest.mark.parametrize("commit_mode", ["persist_then_raise", "raise_before_persist"])
def test_terminal_commit_acknowledgement_is_read_back_or_left_started(tmp_path, commit_mode):
    base = sqlite3.connect(tmp_path / f"terminal-{commit_mode}.db")

    class CommitAcknowledgement:
        mode = None
        commits_before_failure = None

        def __getattr__(self, name):
            return getattr(base, name)

        def commit(self):
            if self.commits_before_failure is not None and self.commits_before_failure > 0:
                self.commits_before_failure -= 1
                return base.commit()
            mode = self.mode
            self.mode = None
            self.commits_before_failure = None
            if mode == "persist_then_raise":
                base.commit()
                raise sqlite3.OperationalError("lost terminal commit acknowledgement")
            if mode == "raise_before_persist":
                raise sqlite3.OperationalError("terminal commit did not persist")
            base.commit()

        def rollback(self):
            base.rollback()

    connection = CommitAcknowledgement()
    store = ReceiptStore(connection)
    store.prepare(
        receipt_id="terminal-first",
        call_id="terminal-call-1",
        user_id="owner-1",
        turn_id="terminal-turn-1",
        round_id=1,
        tool_name="monitor_create_natural_rule",
        origin="native",
        arguments={"instruction": "large deposits"},
    )
    store.start("terminal-first")

    connection.mode = commit_mode
    # finish() first verifies the current receipt/schema; fail the next
    # commit, which is the terminal status update itself.
    connection.commits_before_failure = 1
    if commit_mode == "persist_then_raise":
        receipt = store.finish(
            "terminal-first", status="confirmed", ok=True, complete=True,
            result_summary="created rule #1",
        )
        assert receipt.status == "confirmed"
    else:
        with pytest.raises(ReceiptLifecycleError, match="unable to persist receipt transition"):
            store.finish(
                "terminal-first", status="confirmed", ok=True, complete=True,
                result_summary="created rule #1",
            )
        assert store.get("terminal-first").status == "started"

        store.prepare(
            receipt_id="terminal-second",
            call_id="terminal-call-2",
            user_id="owner-1",
            turn_id="terminal-turn-2",
            round_id=1,
            tool_name="monitor_add_rule",
            origin="native",
            arguments={"name": "same monitor"},
        )
        with pytest.raises(ReceiptLifecycleError, match="requires manual reconciliation"):
            store.start("terminal-second")


def test_receipt_lifecycle_is_durable_and_correlated():
    connection = sqlite3.connect(":memory:")
    store = ReceiptStore(connection)

    prepared = store.prepare(
        receipt_id="receipt-1",
        call_id="call-1",
        user_id="user-1",
        turn_id="turn-1",
        round_id=2,
        tool_name="send_push_alert",
        origin="native",
        arguments={"message": "done"},
    )
    assert prepared.status == "prepared"
    assert prepared.ok is None

    started = store.start("receipt-1")
    assert started.status == "started"

    confirmed = store.finish(
        "receipt-1",
        status="confirmed",
        ok=True,
        complete=True,
        result_summary="alert accepted",
    )
    assert confirmed.status == "confirmed"
    assert confirmed.ok is True
    assert confirmed.complete is True
    assert store.get("receipt-1").call_id == "call-1"


def test_unknown_is_terminal_and_cannot_be_marked_successful_later():
    connection = sqlite3.connect(":memory:")
    store = ReceiptStore(connection)
    store.prepare(
        receipt_id="receipt-2",
        call_id="call-2",
        user_id="user-1",
        turn_id="turn-1",
        round_id=1,
        tool_name="send_push_alert",
        origin="native",
        arguments={},
    )
    store.start("receipt-2")
    store.finish(
        "receipt-2",
        status="unknown",
        ok=False,
        complete=False,
        error="history append failed after dispatch",
    )
    with pytest.raises(ReceiptLifecycleError, match="invalid receipt transition"):
        store.finish("receipt-2", status="confirmed", ok=True, complete=True)


@pytest.mark.parametrize("ambiguous_status", ["started", "unknown"])
def test_ambiguous_monitor_insert_blocks_later_dispatch(tmp_path, ambiguous_status):
    db_path = tmp_path / "receipts.db"
    first = ReceiptStore(sqlite3.connect(db_path, isolation_level=None))
    first.prepare(
        receipt_id="monitor-first",
        call_id="call-first",
        user_id="owner-1",
        turn_id="turn-first",
        round_id=1,
        tool_name="monitor_create_natural_rule",
        origin="native",
        arguments={"instruction": "alert me on large deposits"},
    )
    first.start("monitor-first")
    if ambiguous_status == "unknown":
        first.finish(
            "monitor-first",
            status="unknown",
            ok=False,
            complete=False,
            error="terminal receipt could not be persisted after dispatch",
        )

    # A fresh store models a later turn/process restart. The ambiguous receipt
    # must block a new call ID before the monitor implementation can run.
    later = ReceiptStore(sqlite3.connect(db_path, isolation_level=None))
    later.prepare(
        receipt_id="monitor-second",
        call_id="call-second",
        user_id="owner-1",
        turn_id="turn-second",
        round_id=1,
        tool_name="monitor_create_natural_rule",
        origin="native",
        arguments={"instruction": "notify me when rent posts"},
    )
    with pytest.raises(ReceiptLifecycleError, match="requires manual reconciliation"):
        later.start("monitor-second")

    assert later.get("monitor-first").status == ambiguous_status
    assert later.get("monitor-second").status == "failed"

    # The guard is owner-scoped; another user's unresolved receipt must not
    # prevent an otherwise authorized independent monitor operation.
    later.prepare(
        receipt_id="monitor-other-owner",
        call_id="call-other-owner",
        user_id="owner-2",
        turn_id="turn-other-owner",
        round_id=1,
        tool_name="monitor_create_natural_rule",
        origin="native",
        arguments={"instruction": "alert me on low balances"},
    )
    assert later.start("monitor-other-owner").status == "started"


def test_concurrent_monitor_starts_only_admit_one_unresolved_insert(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    db_path = tmp_path / "concurrent-receipts.db"
    setup = ReceiptStore(sqlite3.connect(db_path, isolation_level=None))
    for index in (1, 2):
        setup.prepare(
            receipt_id=f"concurrent-{index}",
            call_id=f"concurrent-call-{index}",
            user_id="owner-1",
            turn_id=f"concurrent-turn-{index}",
            round_id=1,
            tool_name="monitor_create_natural_rule",
            origin="native",
            arguments={"instruction": f"rule {index}"},
        )

    barrier = Barrier(2)

    def start_receipt(receipt_id):
        store = ReceiptStore(sqlite3.connect(db_path, timeout=10, isolation_level=None))
        barrier.wait(timeout=5)
        try:
            return store.start(receipt_id).status
        except ReceiptLifecycleError:
            return store.get(receipt_id).status

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(start_receipt, ("concurrent-1", "concurrent-2")))

    assert sorted(statuses) == ["failed", "started"]


def test_alternate_monitor_insert_tool_cannot_bypass_ambiguous_receipt_guard():
    store = ReceiptStore(sqlite3.connect(":memory:"))
    for receipt_id, call_id, turn_id, tool_name in (
        ("alternate-first", "alternate-call-1", "alternate-turn-1", "monitor_add_rule"),
        ("alternate-second", "alternate-call-2", "alternate-turn-2", "monitor_create_natural_rule"),
    ):
        store.prepare(
            receipt_id=receipt_id,
            call_id=call_id,
            user_id="owner-1",
            turn_id=turn_id,
            round_id=1,
            tool_name=tool_name,
            origin="native",
            arguments={"name": "large deposit", "instruction": "large deposits"},
        )

    store.start("alternate-first")
    with pytest.raises(ReceiptLifecycleError, match="requires manual reconciliation"):
        store.start("alternate-second")


def test_turn_observability_correlates_round_receipt_and_claim_records():
    connection = sqlite3.connect(":memory:")
    store = ReceiptStore(connection)
    store.record_provider_round({
        "turn_id": "turn-3",
        "provider": "openai",
        "requested_model": "model-a",
        "route_profile": "primary",
        "provider_order": ["ProviderA"],
        "finish_reason": "tool_calls",
        "native_tool_calls": 1,
        "tools_offered": 3,
        "text_chars": 0,
        "latency_ms": 42,
    })
    store.record_claim_decision(
        turn_id="turn-3",
        allowed=False,
        claims=["I sent the alert"],
        unsupported=["I sent the alert"],
    )
    view = store.observability_for_turn("turn-3")
    assert view["provider_rounds"][0]["finish_reason"] == "tool_calls"
    assert view["claim_decisions"][0]["unsupported"] == ["I sent the alert"]


def test_turn_observability_includes_router_authorization():
    connection = sqlite3.connect(":memory:")
    store = ReceiptStore(connection)
    store.record_router_decision({
        "turn_id": "turn-4",
        "round_id": 1,
        "mode": "ordinary",
        "decision": "authorized",
        "reason": "proposal_topk",
        "allowed_names": {"search_web", "end_turn"},
        "candidate_names": {"search_web"},
        "top1_score": 0.91,
        "latency_ms": 3.5,
    })
    view = store.observability_for_turn("turn-4")
    assert view["router_decisions"][0]["decision"] == "authorized"
    assert view["router_decisions"][0]["allowed_names"] == ["end_turn", "search_web"]
