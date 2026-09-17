"""Concierge risk monitor watchdog + Discord DM delivery.

B1 proved the rules and audit chain; this wires the autonomous loop that runs
the pass per concierge tenant and DMs new alerts to the owning user — mirroring
src.services.monitor.watchdog_loop + _deliver_alerts, but for the per-tenant
concierge_risk_alerts tables. Delivery is the caller's job: it lives here.

Tenant rows are "user:<discord_id>" so the DM target is parsed directly from
the tenant identity (never model-supplied); rules enforce per-tenant cooldown,
so each alert fires at most once per cooldown — no separate delivered flag
needed.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from typing import Any, Dict, List, Optional

from src.security.vault import DEFAULT_DB_PATH as _DB
from src.services.concierge.audit import AuditLog
from src.services.concierge.risk_monitor import RiskRuleStore, run_risk_pass
from src.services.concierge.tenants import TenantStore

MONITOR_POLL_INTERVAL_SECONDS = int(os.getenv("MONITOR_POLL_INTERVAL_SECONDS", "1800"))


def _tenant_user_id(tenant: str) -> Optional[str]:
    """Concierge tenants are interaction-derived "user:<discord_id>"."""
    if not tenant or not tenant.startswith("user:"):
        return None
    return tenant.split(":", 1)[1]


def concierge_alerts_due(conn: sqlite3.Connection, user_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    """Undelivered (unacked) concierge alerts for a tenant/user.

    Defensive: returns [] on any SQLite error.
    """
    try:
        limit = max(1, min(int(limit), 500))
        rows = conn.execute(
            """
            SELECT id, rule_id, rule_kind, severity, title, detail,
                   fired_at, acked
            FROM concierge_risk_alerts
            WHERE tenant = ? AND acked = 0
            ORDER BY id DESC LIMIT ?
            """,
            ("user:" + str(user_id), limit),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [
        {
            "id": r[0],
            "rule_id": r[1],
            "rule_kind": r[2],
            "severity": r[3],
            "title": r[4],
            "detail": r[5],
            "fired_at": r[6],
            "acked": bool(r[7]),
        }
        for r in rows
    ]


def _fetch_alert_rows(db_path: str, alert_ids: List[int]) -> List[Dict[str, Any]]:
    """Load specific fired alerts by id (the watchdog delivers these)."""
    if not alert_ids:
        return []
    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        placeholders = ", ".join("?" for _ in alert_ids)
        rows = conn.execute(
            f"SELECT id, rule_kind, severity, title, detail FROM concierge_risk_alerts "
            f"WHERE id IN ({placeholders}) ORDER BY id ASC",
            list(alert_ids),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return [
        {
            "id": r[0],
            "rule_kind": r[1],
            "severity": r[2],
            "title": r[3],
            "detail": r[4],
        }
        for r in rows
    ]


async def _deliver_concierge_alerts(alert_rows: List[Dict[str, Any]], bot, user_id: str) -> int:
    """Best-effort Discord DM delivery; never raises. Mirrors _deliver_alerts."""
    if not alert_rows or not bot or not user_id:
        return 0
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return 0
    delivered = 0
    try:
        user = await bot.fetch_user(uid)
    except Exception as exc:
        print(f" [CONCIERGE MONITOR] fetch_user({uid}) failed: {type(exc).__name__}: {exc}")
        return 0
    for alert in alert_rows:
        try:
            await user.send(
                f"[Concierge Risk {alert['severity'].upper()}] {alert['title']}\n{alert['detail']}"
            )
            delivered += 1
        except Exception as exc:
            print(f" [CONCIERGE MONITOR] dm delivery failed for {uid}: {type(exc).__name__}: {exc}")
    return delivered


async def run_once(store: RiskRuleStore, audit: AuditLog, tenants: TenantStore, bot) -> Dict[str, Any]:
    """One watchdog pass across all concierge tenants."""
    summary = {"rules_evaluated": 0, "rules_fired": 0, "alert_ids": [], "delivered": 0, "errors": []}
    for status in tenants.list_tenants():
        tenant = status["tenant"]
        uid = _tenant_user_id(tenant)
        try:
            res = run_risk_pass(store, audit, tenant)
        except Exception as exc:
            summary["errors"].append(f"{tenant}: {type(exc).__name__}: {exc}")
            continue
        summary["rules_evaluated"] += res.get("rules_evaluated", 0)
        summary["rules_fired"] += res.get("rules_fired", 0)
        fired = res.get("alert_ids", []) or []
        summary["alert_ids"].extend(fired)
        if fired and uid:
            rows = _fetch_alert_rows(store.db_path, fired)
            try:
                summary["delivered"] += await _deliver_concierge_alerts(rows, bot, uid)
            except Exception as exc:
                summary["errors"].append(f"deliver {tenant}: {type(exc).__name__}: {exc}")
    return summary


async def concierge_risk_watchdog_loop() -> None:
    """Long-lived background task; modeled on monitor_watchdog_loop.

    Polls every MONITOR_POLL_INTERVAL_SECONDS. Each iteration runs one pass
    over all concierge tenants; a tenant error never kills the loop.
    """
    from src.core.state import bot as _bot
    store = RiskRuleStore(_DB)
    audit = AuditLog(_DB)
    tenants = TenantStore(_DB)
    bot = _bot
    print(" [CONCIERGE MONITOR] watchdog loop starting (interval={}s).".format(MONITOR_POLL_INTERVAL_SECONDS))
    while True:
        try:
            summary = await run_once(store, audit, tenants, bot)
            if summary["rules_fired"]:
                print(" [CONCIERGE MONITOR] pass evaluated={} rules_fired={} delivered={} errors={}".format(
                    summary["rules_evaluated"], summary["rules_fired"], summary["delivered"], len(summary["errors"])))
        except Exception as exc:
            print(f" [CONCIERGE MONITOR] watchdog iteration failed: {type(exc).__name__}: {exc}")
        await asyncio.sleep(MONITOR_POLL_INTERVAL_SECONDS)


__all__ = [
    "MONITOR_POLL_INTERVAL_SECONDS",
    "concierge_alerts_due",
    "run_once",
    "concierge_risk_watchdog_loop",
]
