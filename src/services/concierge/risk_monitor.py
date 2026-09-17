"""Concierge risk monitor (B1 skeleton) — LLM-free anomaly watcher.

Reuses the financial monitor's architecture (src.services.monitor.py):
a fixed allowlist of deterministic rule kinds, rules owned per tenant,
no dynamic code, no eval, no LLM path (nothing to prompt-inject).  The
evaluators read the hash-chained concierge audit chain and fire into a
per-tenant alert table; delivery (Discord DM) is the caller's job.

Rule kinds (B1 skeleton):
  * capture_storm             — burst of capture/login events in a window
  * repeated_stored_secret    — the same secret label stored repeatedly
  * out_of_allowlist_attempt  — refused read/login attempts (velocity)
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.services.concierge.audit import AuditLog

RULE_KINDS = frozenset({
    "capture_storm",
    "repeated_stored_secret",
    "out_of_allowlist_attempt",
})

STORM_ACTIONS = ("capture_stored", "login_spawn", "login_done")
REPEAT_ACTION = "capture_stored"
REFUSAL_ACTIONS = ("read_refused", "login_refused")


def _parse_json(value: Any, default: Dict[str, Any]) -> Dict[str, Any]:
    if value is None:
        return dict(default)
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "replace")
    if not isinstance(value, str):
        return dict(default)
    text = value.strip()
    if not text:
        return dict(default)
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return dict(default)
    return parsed if isinstance(parsed, dict) else dict(default)


def validate_risk_rule(kind: str, config: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
    """Validate a risk rule kind + config. Returns (ok, error, normalized)."""
    if kind not in RULE_KINDS:
        return False, f"Unknown risk rule kind '{kind}'. Valid: {sorted(RULE_KINDS)}", {}
    cfg = dict(config or {})
    if kind == "capture_storm":
        try:
            cfg["window_minutes"] = int(cfg.get("window_minutes", 10))
        except (TypeError, ValueError):
            return False, "capture_storm requires integer 'window_minutes'", {}
        try:
            cfg["threshold"] = int(cfg.get("threshold", 5))
        except (TypeError, ValueError):
            return False, "capture_storm requires integer 'threshold'", {}
        cfg["window_minutes"] = max(1, min(cfg["window_minutes"], 1440))
        cfg["threshold"] = max(2, cfg["threshold"])
        return True, "", cfg
    if kind == "repeated_stored_secret":
        try:
            cfg["window_minutes"] = int(cfg.get("window_minutes", 1440))
        except (TypeError, ValueError):
            return False, "repeated_stored_secret requires integer 'window_minutes'", {}
        try:
            cfg["threshold"] = int(cfg.get("threshold", 3))
        except (TypeError, ValueError):
            return False, "repeated_stored_secret requires integer 'threshold'", {}
        cfg["window_minutes"] = max(1, min(cfg["window_minutes"], 1440 * 7))
        cfg["threshold"] = max(2, cfg["threshold"])
        return True, "", cfg
    if kind == "out_of_allowlist_attempt":
        try:
            cfg["window_minutes"] = int(cfg.get("window_minutes", 60))
        except (TypeError, ValueError):
            return False, "out_of_allowlist_attempt requires integer 'window_minutes'", {}
        try:
            cfg["threshold"] = int(cfg.get("threshold", 3))
        except (TypeError, ValueError):
            return False, "out_of_allowlist_attempt requires integer 'threshold'", {}
        cfg["window_minutes"] = max(1, min(cfg["window_minutes"], 1440))
        cfg["threshold"] = max(2, cfg["threshold"])
        return True, "", cfg
    return False, "unreachable", {}


def _within_window(rows: List[Dict[str, Any]], minutes: int) -> List[Dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    out = []
    for row in rows:
        ts_text = str(row["ts"] or "")
        ts_text = ts_text.replace("Z", "+00:00")
        try:
            ts = datetime.fromisoformat(ts_text)
        except (ValueError, TypeError):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts >= cutoff:
            out.append(row)
    return out


def _eval_capture_storm(rows: List[Dict[str, Any]], cfg: Dict[str, Any], tenant: str) -> Optional[str]:
    recent = _within_window(rows, int(cfg.get("window_minutes", 10)))
    hits = [r for r in recent if r["action"] in STORM_ACTIONS]
    if len(hits) < int(cfg.get("threshold", 5)):
        return None
    kinds: Dict[str, int] = defaultdict(int)
    for r in hits:
        kinds[r["action"]] += 1
    summary = ", ".join(f"{k}: {n}" for k, n in sorted(kinds.items()))
    return f"⚠️ Capture storm for {tenant}: {len(hits)} events in {cfg['window_minutes']}m ({summary})."


def _eval_repeated_stored_secret(rows: List[Dict[str, Any]], cfg: Dict[str, Any], tenant: str) -> Optional[str]:
    recent = _within_window(rows, int(cfg.get("window_minutes", 1440)))
    counts: Dict[str, int] = defaultdict(int)
    for r in recent:
        if r["action"] != REPEAT_ACTION:
            continue
        label = str(r["detail"].get("label") or r.get("subject") or "-")
        counts[label] += 1
    repeated = {label: n for label, n in counts.items() if n >= int(cfg.get("threshold", 3))}
    if not repeated:
        return None
    parts = ", ".join(f"{label} x{n}" for label, n in sorted(repeated.items()))
    return f"⚠️ Repeated stored secret for {tenant} within {cfg['window_minutes']}m: {parts}."


def _eval_out_of_allowlist(rows: List[Dict[str, Any]], cfg: Dict[str, Any], tenant: str) -> Optional[str]:
    recent = _within_window(rows, int(cfg.get("window_minutes", 60)))
    hits = [r for r in recent if r["action"] in REFUSAL_ACTIONS]
    if len(hits) < int(cfg.get("threshold", 3)):
        return None
    samples = []
    for r in hits[:3]:
        reason = str(r["detail"].get("reason") or r["action"])
        samples.append(reason[:60])
    return (
        f"⚠️ {len(hits)} out-of-allowlist attempts for {tenant} in "
        f"{cfg['window_minutes']}m (e.g. {', '.join(samples)})."
    )


EVALUATORS: Dict[str, Callable[[List[Dict[str, Any]], Dict[str, Any], str], Optional[str]]] = {
    "capture_storm": _eval_capture_storm,
    "repeated_stored_secret": _eval_repeated_stored_secret,
    "out_of_allowlist_attempt": _eval_out_of_allowlist,
}


class RiskRuleStore:
    """Per-tenant risk rules + alerts (SQLite)."""

    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        self._local = threading.local()
        Path(self.db_path).resolve().parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn"):
            conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=10.0)
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return self._local.conn

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS concierge_risk_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant TEXT NOT NULL,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    config TEXT NOT NULL DEFAULT '{}',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    severity TEXT NOT NULL DEFAULT 'warn',
                    cooldown_hours INTEGER NOT NULL DEFAULT 6,
                    last_fired_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS concierge_risk_alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant TEXT NOT NULL,
                    rule_id INTEGER NOT NULL,
                    rule_kind TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    title TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    fired_at TEXT NOT NULL,
                    acked INTEGER NOT NULL DEFAULT 0
                )
            """)
            conn.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    def upsert(
        self,
        tenant: str,
        name: str,
        kind: str,
        config: Dict[str, Any],
        severity: str = "warn",
        cooldown_hours: int = 6,
    ) -> int:
        ok, err, cfg = validate_risk_rule(kind, config)
        if not ok:
            raise ValueError(err)
        now = self._now()
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id FROM concierge_risk_rules WHERE tenant = ? AND kind = ?",
                (tenant, kind),
            )
            row = cur.fetchone()
            if row:
                conn.execute(
                    "UPDATE concierge_risk_rules SET name = ?, config = ?, severity = ?, "
                    "cooldown_hours = ?, updated_at = ? WHERE id = ?",
                    (name, json.dumps(cfg, sort_keys=True, separators=(",", ":")),
                     severity, int(cooldown_hours), now, row["id"]),
                )
                return int(row["id"])
            cur.execute("""
                INSERT INTO concierge_risk_rules
                    (tenant, name, kind, config, enabled, severity, cooldown_hours,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)
            """, (
                tenant, name, kind, json.dumps(cfg, sort_keys=True, separators=(",", ":")),
                severity, int(cooldown_hours), now, now,
            ))
            conn.commit()
            return int(cur.lastrowid)

    def set_enabled(self, tenant: str, rule_id: int, enabled: bool) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE concierge_risk_rules SET enabled = ?, updated_at = ? "
                "WHERE id = ? AND tenant = ?",
                (1 if enabled else 0, self._now(), rule_id, tenant),
            )
            conn.commit()

    def list_rules(self, tenant: str, include_disabled: bool = False) -> List[Dict[str, Any]]:
        cur = self._conn().cursor()
        if include_disabled:
            cur.execute(
                "SELECT * FROM concierge_risk_rules WHERE tenant = ? ORDER BY id",
                (tenant,),
            )
        else:
            cur.execute(
                "SELECT * FROM concierge_risk_rules WHERE tenant = ? AND enabled = 1 ORDER BY id",
                (tenant,),
            )
        out = []
        for r in cur.fetchall():
            out.append({
                "id": r["id"],
                "tenant": r["tenant"],
                "name": r["name"],
                "kind": r["kind"],
                "config": _parse_json(r["config"], {}),
                "enabled": bool(r["enabled"]),
                "severity": r["severity"],
                "cooldown_hours": r["cooldown_hours"],
                "last_fired_at": r["last_fired_at"],
            })
        return out

    def record_alert(
        self,
        tenant: str,
        rule_id: int,
        rule_kind: str,
        severity: str,
        title: str,
        detail: str,
    ) -> int:
        now = self._now()
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO concierge_risk_alerts
                    (tenant, rule_id, rule_kind, severity, title, detail, fired_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (tenant, rule_id, rule_kind, severity, title, detail, now))
            conn.execute(
                "UPDATE concierge_risk_rules SET last_fired_at = ? WHERE id = ?",
                (now, rule_id),
            )
            conn.commit()
            return int(cur.lastrowid)

    def list_alerts(self, tenant: str, limit: int = 50) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        cur = self._conn().cursor()
        cur.execute(
            "SELECT * FROM concierge_risk_alerts WHERE tenant = ? ORDER BY id DESC LIMIT ?",
            (tenant, limit),
        )
        return [dict(r) for r in cur.fetchall()]

    def ack_alert(self, tenant: str, alert_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE concierge_risk_alerts SET acked = 1 WHERE id = ? AND tenant = ?",
                (alert_id, tenant),
            )
            conn.commit()
            return cur.rowcount == 1


def run_risk_pass(
    store: RiskRuleStore,
    audit: AuditLog,
    tenant: str,
    tail_limit: int = 500,
) -> Dict[str, Any]:
    """Evaluate all enabled risk rules for one tenant against the audit chain.

    Deterministic and LLM-free; delivery to Discord is the caller's job.
    Returns a summary dict with fired alert ids.
    """
    rules = store.list_rules(tenant, include_disabled=False)
    rows = audit.tail(limit=tail_limit, tenant=tenant)
    evaluated = 0
    fired = 0
    alert_ids: List[int] = []
    errors: List[str] = []
    for rule in rules:
        kind = rule["kind"]
        evaluator = EVALUATORS.get(kind)
        if evaluator is None:
            errors.append(f"rule #{rule['id']}: no evaluator for {kind}")
            continue
        last = rule.get("last_fired_at")
        if last:
            try:
                last_dt = datetime.strptime(str(last)[:19], "%Y-%m-%dT%H:%M:%S").replace(
                    tzinfo=timezone.utc
                )
                if datetime.now(timezone.utc) - last_dt < timedelta(
                    hours=int(rule.get("cooldown_hours") or 0)
                ):
                    continue
            except (ValueError, TypeError):
                pass
        evaluated += 1
        try:
            detail = evaluator(rows, rule["config"], tenant)
            if not detail:
                continue
            title = f"risk {kind}: {rule['name']}"
            alert_id = store.record_alert(
                tenant, int(rule["id"]), kind, rule["severity"], title, detail,
            )
            fired += 1
            alert_ids.append(alert_id)
        except Exception as exc:
            errors.append(f"rule #{rule['id']}: {type(exc).__name__}: {exc}")
    return {
        "tenant": tenant,
        "rules_evaluated": evaluated,
        "rules_fired": fired,
        "alert_ids": alert_ids,
        "errors": errors,
    }


__all__ = [
    "RiskRuleStore",
    "run_risk_pass",
    "validate_risk_rule",
    "RULE_KINDS",
    "EVALUATORS",
]