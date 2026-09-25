"""Durable lifecycle records for advisor tool executions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from typing import Any


TERMINAL_STATUSES = frozenset({"confirmed", "failed", "unknown"})
ALLOWED_TRANSITIONS = {
    "prepared": frozenset({"started", "failed", "unknown"}),
    "started": TERMINAL_STATUSES,
    "confirmed": frozenset(),
    "failed": frozenset(),
    "unknown": frozenset(),
}


class ReceiptLifecycleError(RuntimeError):
    """Raised when a receipt lifecycle cannot be advanced safely."""


@dataclass(frozen=True)
class ToolReceipt:
    receipt_id: str
    call_id: str
    user_id: str
    turn_id: str
    round_id: int
    tool_name: str
    origin: str
    status: str
    ok: bool | None
    complete: bool | None
    arguments_hash: str
    result_summary: str = ""
    error: str = ""


def arguments_hash(arguments: Any) -> str:
    payload = json.dumps(
        arguments,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def terminal_status_for_outcome(*, succeeded: bool, ambiguous: bool = False) -> str:
    """Map execution evidence to a terminal receipt without hiding ambiguity."""
    if succeeded:
        return "confirmed"
    return "unknown" if ambiguous else "failed"


class ReceiptStore:
    """SQLite-backed compact receipt store.

    The schema is created through this explicit migration helper rather than
    modifying the existing high-volume tool log table.
    """

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def ensure_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS tool_receipts (
                receipt_id TEXT PRIMARY KEY,
                call_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                round_id INTEGER NOT NULL,
                tool_name TEXT NOT NULL,
                origin TEXT NOT NULL,
                status TEXT NOT NULL,
                ok INTEGER,
                complete INTEGER,
                arguments_hash TEXT NOT NULL,
                result_summary TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_tool_receipts_user_created
                ON tool_receipts(user_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_tool_receipts_call
                ON tool_receipts(call_id);
            CREATE TABLE IF NOT EXISTS provider_rounds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                turn_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                requested_model TEXT NOT NULL,
                actual_model TEXT,
                route_profile TEXT,
                provider_order TEXT NOT NULL DEFAULT '[]',
                finish_reason TEXT,
                tools_offered INTEGER NOT NULL DEFAULT 0,
                native_tool_calls INTEGER NOT NULL DEFAULT 0,
                text_chars INTEGER NOT NULL DEFAULT 0,
                latency_ms INTEGER,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_provider_rounds_turn
                ON provider_rounds(turn_id, id);
            CREATE TABLE IF NOT EXISTS claim_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                turn_id TEXT NOT NULL,
                allowed INTEGER NOT NULL,
                claims TEXT NOT NULL,
                unsupported TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_claim_decisions_turn
                ON claim_decisions(turn_id, id);
            CREATE TABLE IF NOT EXISTS router_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                turn_id TEXT NOT NULL,
                round_id INTEGER NOT NULL,
                mode TEXT NOT NULL,
                decision TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                allowed_names TEXT NOT NULL DEFAULT '[]',
                candidate_names TEXT NOT NULL DEFAULT '[]',
                top1_score REAL,
                latency_ms REAL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_router_decisions_turn
                ON router_decisions(turn_id, id);
            CREATE TABLE IF NOT EXISTS decision_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                round_id INTEGER NOT NULL,
                decision_kind TEXT NOT NULL,
                provider TEXT NOT NULL,
                mode TEXT NOT NULL,
                requested_model TEXT NOT NULL,
                actual_model TEXT,
                endpoint_profile TEXT,
                candidates TEXT NOT NULL DEFAULT '[]',
                selected TEXT,
                probabilities TEXT NOT NULL DEFAULT '{}',
                confidence REAL,
                threshold REAL,
                accepted INTEGER NOT NULL DEFAULT 0,
                abstained INTEGER NOT NULL DEFAULT 0,
                fallback_reason TEXT NOT NULL DEFAULT '',
                latency_ms REAL,
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_decision_records_turn
                ON decision_records(turn_id, id);
            """
        )
        self.connection.commit()

    def record_provider_round(self, record: dict[str, Any]) -> None:
        self.ensure_schema()
        self.connection.execute(
            """
            INSERT INTO provider_rounds (
                turn_id, provider, requested_model, actual_model,
                route_profile, provider_order, finish_reason,
                tools_offered, native_tool_calls, text_chars, latency_ms, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(record.get("turn_id") or ""),
                str(record.get("provider") or ""),
                str(record.get("requested_model") or ""),
                record.get("actual_model"),
                record.get("route_profile"),
                json.dumps(record.get("provider_order") or []),
                record.get("finish_reason"),
                int(record.get("tools_offered") or 0),
                int(record.get("native_tool_calls") or 0),
                int(record.get("text_chars") or 0),
                record.get("latency_ms"),
                _now(),
            ),
        )
        self.connection.commit()

    def record_claim_decision(
        self,
        *,
        turn_id: str,
        allowed: bool,
        claims: list[str],
        unsupported: list[str],
    ) -> None:
        self.ensure_schema()
        self.connection.execute(
            """
            INSERT INTO claim_decisions (
                turn_id, allowed, claims, unsupported, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                str(turn_id),
                int(bool(allowed)),
                json.dumps([str(item) for item in claims[:20]]),
                json.dumps([str(item) for item in unsupported[:20]]),
                _now(),
            ),
        )
        self.connection.commit()

    def record_router_decision(self, record: dict[str, Any]) -> None:
        self.ensure_schema()
        self.connection.execute(
            """
            INSERT INTO router_decisions (
                turn_id, round_id, mode, decision, reason,
                allowed_names, candidate_names, top1_score, latency_ms, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(record.get("turn_id") or ""),
                int(record.get("round_id") or 0),
                str(record.get("mode") or "ordinary"),
                str(record.get("decision") or "unknown"),
                str(record.get("reason") or ""),
                json.dumps(sorted(str(x) for x in (record.get("allowed_names") or []))),
                json.dumps(sorted(str(x) for x in (record.get("candidate_names") or []))),
                record.get("top1_score"),
                record.get("latency_ms"),
                _now(),
            ),
        )
        self.connection.commit()

    def record_decision(self, record: dict[str, Any]) -> None:
        """Persist a bounded Kev decision for turn reconstruction."""
        self.ensure_schema()
        self.connection.execute(
            """
            INSERT INTO decision_records (
                decision_id, turn_id, round_id, decision_kind, provider, mode,
                requested_model, actual_model, endpoint_profile, candidates,
                selected, probabilities, confidence, threshold, accepted,
                abstained, fallback_reason, latency_ms, error, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(record.get("decision_id") or ""),
                str(record.get("turn_id") or ""),
                int(record.get("round_id") or 0),
                str(record.get("decision_kind") or "general"),
                str(record.get("provider") or "kev"),
                str(record.get("mode") or "local"),
                str(record.get("requested_model") or ""),
                record.get("actual_model"),
                str(record.get("endpoint_profile") or ""),
                json.dumps(sorted(str(x) for x in (record.get("candidates") or []))),
                record.get("selected"),
                json.dumps(record.get("probabilities") or {}, sort_keys=True),
                record.get("confidence"),
                record.get("threshold"),
                int(bool(record.get("accepted"))),
                int(bool(record.get("abstained"))),
                str(record.get("fallback_reason") or "")[:500],
                record.get("latency_ms"),
                str(record.get("error") or "")[:1000],
                _now(),
            ),
        )
        self.connection.commit()

    def observability_for_turn(self, turn_id: str) -> dict[str, list[dict[str, Any]]]:
        self.ensure_schema()
        receipts = self.connection.execute(
            """
            SELECT receipt_id, call_id, user_id, turn_id, round_id, tool_name,
                   origin, status, ok, complete, result_summary, error,
                   created_at, updated_at
            FROM tool_receipts WHERE turn_id = ? ORDER BY created_at, receipt_id
            """,
            (str(turn_id),),
        ).fetchall()
        rounds = self.connection.execute(
            """
            SELECT turn_id, provider, requested_model, actual_model,
                   route_profile, provider_order, finish_reason,
                   tools_offered, native_tool_calls, text_chars, latency_ms,
                   created_at
            FROM provider_rounds WHERE turn_id = ? ORDER BY id
            """,
            (str(turn_id),),
        ).fetchall()
        claims = self.connection.execute(
            """
            SELECT turn_id, allowed, claims, unsupported, created_at
            FROM claim_decisions WHERE turn_id = ? ORDER BY id
            """,
            (str(turn_id),),
        ).fetchall()
        routers = self.connection.execute(
            """
            SELECT turn_id, round_id, mode, decision, reason,
                   allowed_names, candidate_names, top1_score, latency_ms, created_at
            FROM router_decisions WHERE turn_id = ? ORDER BY id
            """,
            (str(turn_id),),
        ).fetchall()
        decisions = self.connection.execute(
            """
            SELECT decision_id, turn_id, round_id, decision_kind, provider,
                   mode, requested_model, actual_model, endpoint_profile,
                   candidates, selected, probabilities, confidence, threshold,
                   accepted, abstained, fallback_reason, latency_ms, error,
                   created_at
            FROM decision_records WHERE turn_id = ? ORDER BY id
            """,
            (str(turn_id),),
        ).fetchall()
        return {
            "receipts": [
                {
                    "receipt_id": row[0],
                    "call_id": row[1],
                    "user_id": row[2],
                    "turn_id": row[3],
                    "round_id": row[4],
                    "tool_name": row[5],
                    "origin": row[6],
                    "status": row[7],
                    "ok": None if row[8] is None else bool(row[8]),
                    "complete": None if row[9] is None else bool(row[9]),
                    "result_summary": row[10],
                    "error": row[11],
                    "created_at": row[12],
                    "updated_at": row[13],
                }
                for row in receipts
            ],
            "provider_rounds": [
                {
                    "turn_id": row[0],
                    "provider": row[1],
                    "requested_model": row[2],
                    "actual_model": row[3],
                    "route_profile": row[4],
                    "provider_order": json.loads(row[5] or "[]"),
                    "finish_reason": row[6],
                    "tools_offered": row[7],
                    "native_tool_calls": row[8],
                    "text_chars": row[9],
                    "latency_ms": row[10],
                    "created_at": row[11],
                }
                for row in rounds
            ],
            "claim_decisions": [
                {
                    "turn_id": row[0],
                    "allowed": bool(row[1]),
                    "claims": json.loads(row[2] or "[]"),
                    "unsupported": json.loads(row[3] or "[]"),
                    "created_at": row[4],
                }
                for row in claims
            ],
            "router_decisions": [
                {
                    "turn_id": row[0],
                    "round_id": row[1],
                    "mode": row[2],
                    "decision": row[3],
                    "reason": row[4],
                    "allowed_names": json.loads(row[5] or "[]"),
                    "candidate_names": json.loads(row[6] or "[]"),
                    "top1_score": row[7],
                    "latency_ms": row[8],
                    "created_at": row[9],
                }
                for row in routers
            ],
            "decision_records": [
                {
                    "decision_id": row[0],
                    "turn_id": row[1],
                    "round_id": row[2],
                    "decision_kind": row[3],
                    "provider": row[4],
                    "mode": row[5],
                    "requested_model": row[6],
                    "actual_model": row[7],
                    "endpoint_profile": row[8],
                    "candidates": json.loads(row[9] or "[]"),
                    "selected": row[10],
                    "probabilities": json.loads(row[11] or "{}"),
                    "confidence": row[12],
                    "threshold": row[13],
                    "accepted": bool(row[14]),
                    "abstained": bool(row[15]),
                    "fallback_reason": row[16],
                    "latency_ms": row[17],
                    "error": row[18],
                    "created_at": row[19],
                }
                for row in decisions
            ],
        }

    def prepare(
        self,
        *,
        receipt_id: str,
        call_id: str,
        user_id: str,
        turn_id: str,
        round_id: int,
        tool_name: str,
        origin: str,
        arguments: Any,
    ) -> ToolReceipt:
        self.ensure_schema()
        now = _now()
        try:
            self.connection.execute(
                """
                INSERT INTO tool_receipts (
                    receipt_id, call_id, user_id, turn_id, round_id,
                    tool_name, origin, status, ok, complete,
                    arguments_hash, result_summary, error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'prepared', NULL, NULL, ?, '', '', ?, ?)
                """,
                (
                    receipt_id,
                    call_id,
                    str(user_id),
                    str(turn_id),
                    int(round_id),
                    str(tool_name),
                    str(origin),
                    arguments_hash(arguments),
                    now,
                    now,
                ),
            )
            self.connection.commit()
        except Exception as exc:
            self.connection.rollback()
            raise ReceiptLifecycleError(
                f"unable to persist prepared receipt {receipt_id}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        return self.get(receipt_id)

    def start(self, receipt_id: str) -> ToolReceipt:
        # Monitor inserts have no provider idempotency key. If a prior insert
        # for this user is still started/unknown, do not dispatch another one:
        # even changed arguments or a different insert tool could describe the
        # same intended rule. The writer lock makes this check and the new
        # start transition atomic across ReceiptStore instances sharing DB.
        self.ensure_schema()
        current = self.get(receipt_id)
        monitor_insert_tools = {"monitor_create_natural_rule", "monitor_add_rule"}
        if current.tool_name in monitor_insert_tools:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                unresolved = self.connection.execute(
                    """SELECT receipt_id FROM tool_receipts
                       WHERE user_id = ?
                         AND tool_name IN ('monitor_create_natural_rule', 'monitor_add_rule')
                         AND status IN ('started', 'unknown')
                       LIMIT 1""",
                    (current.user_id,),
                ).fetchone()
                if unresolved is not None:
                    reason = (
                        "Blocked monitor creation: an earlier monitor insert has an "
                        "ambiguous outcome and requires manual reconciliation."
                    )
                    self.connection.execute(
                        """UPDATE tool_receipts
                           SET status = 'failed', ok = 0, complete = 0,
                               error = ?, updated_at = ?
                           WHERE receipt_id = ? AND status = 'prepared'""",
                        (reason, _now(), receipt_id),
                    )
                    self.connection.commit()
                    raise ReceiptLifecycleError(reason)
                transition = self.connection.execute(
                    """UPDATE tool_receipts SET status = 'started', updated_at = ?
                       WHERE receipt_id = ? AND status = 'prepared'""",
                    (_now(), receipt_id),
                )
                if transition.rowcount != 1:
                    raise ReceiptLifecycleError(
                        f"invalid receipt transition for {receipt_id}: expected prepared"
                    )
                self.connection.commit()
                return self.get(receipt_id)
            except ReceiptLifecycleError:
                if self.connection.in_transaction:
                    self.connection.rollback()
                raise
            except Exception as exc:
                self.connection.rollback()
                raise ReceiptLifecycleError(
                    f"unable to safely start monitor receipt {receipt_id}: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
        return self._transition(receipt_id, "started")

    def finish(
        self,
        receipt_id: str,
        *,
        status: str,
        ok: bool,
        complete: bool,
        result_summary: str = "",
        error: str = "",
    ) -> ToolReceipt:
        status = str(status).casefold()
        if status not in TERMINAL_STATUSES:
            raise ReceiptLifecycleError(f"invalid terminal receipt status: {status}")
        return self._transition(
            receipt_id,
            status,
            ok=bool(ok),
            complete=bool(complete),
            result_summary=str(result_summary or "")[:2000],
            error=str(error or "")[:1000],
        )

    def get(self, receipt_id: str) -> ToolReceipt:
        self.ensure_schema()
        row = self.connection.execute(
            """
            SELECT receipt_id, call_id, user_id, turn_id, round_id, tool_name,
                   origin, status, ok, complete, arguments_hash,
                   result_summary, error
            FROM tool_receipts WHERE receipt_id = ?
            """,
            (receipt_id,),
        ).fetchone()
        if row is None:
            raise ReceiptLifecycleError(f"receipt not found: {receipt_id}")
        return ToolReceipt(
            receipt_id=row[0],
            call_id=row[1],
            user_id=row[2],
            turn_id=row[3],
            round_id=int(row[4]),
            tool_name=row[5],
            origin=row[6],
            status=row[7],
            ok=None if row[8] is None else bool(row[8]),
            complete=None if row[9] is None else bool(row[9]),
            arguments_hash=row[10],
            result_summary=row[11] or "",
            error=row[12] or "",
        )

    def _transition(self, receipt_id: str, status: str, **fields: Any) -> ToolReceipt:
        current = self.get(receipt_id)
        if status not in ALLOWED_TRANSITIONS.get(current.status, frozenset()):
            raise ReceiptLifecycleError(
                f"invalid receipt transition {current.status!r} -> {status!r}"
            )
        now = _now()
        assignments = ["status = ?", "updated_at = ?"]
        values: list[Any] = [status, now]
        for key in ("ok", "complete", "result_summary", "error"):
            if key in fields:
                assignments.append(f"{key} = ?")
                values.append(fields[key])
        values.append(receipt_id)
        try:
            self.connection.execute(
                f"UPDATE tool_receipts SET {', '.join(assignments)} "
                "WHERE receipt_id = ?",
                values,
            )
            self.connection.commit()
        except Exception as exc:
            self.connection.rollback()
            # A lost commit acknowledgement does not imply that SQLite
            # rolled back. Read the canonical row back: if the exact terminal
            # evidence is durable, return it; otherwise preserve the prior
            # state (typically started), which keeps retry guards fail-closed.
            try:
                persisted = self.get(receipt_id)
                if persisted.status == status and all(
                    getattr(persisted, key) == value
                    for key, value in fields.items()
                ):
                    return persisted
            except Exception:
                pass
            raise ReceiptLifecycleError(
                f"unable to persist receipt transition {receipt_id}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        return self.get(receipt_id)


__all__ = [
    "ALLOWED_TRANSITIONS",
    "ReceiptLifecycleError",
    "ReceiptStore",
    "TERMINAL_STATUSES",
    "ToolReceipt",
    "arguments_hash",
]
