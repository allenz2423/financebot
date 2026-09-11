"""
Delilah Financial OS - Plaid Sync Module

Securely synchronizes financial data from Plaid API.
All database operations use parameterized queries to prevent SQL injection.
"""

import os
import asyncio
from datetime import datetime
from typing import Optional, Tuple, List
import httpx
import sqlite3
import logging

from src.config.settings import get_plaid_settings
from src.security.utils import (
    validate_table_name,
    validate_column_name,
    validate_sql_identifier,
)
from src.db.queries import get_plaid_credential
from src.core.state import CURRENT_USER_ID

logger = logging.getLogger(__name__)


def _get_user_plaid_creds(user_id: Optional[str] = None) -> Tuple[Optional[str], Optional[str], List[str]]:
    """
    Securely retrieve Plaid credentials for a user.
    
    Args:
        user_id: User identifier (uses CURRENT_USER_ID if not provided)
    
    Returns:
        Tuple of (client_id, secret, access_tokens)
    """
    uid = str(user_id or CURRENT_USER_ID.get() or "").strip()
    
    client_id = get_plaid_credential(uid, "plaid_client_id")
    secret = get_plaid_credential(uid, "plaid_secret")
    tokens_str = get_plaid_credential(uid, "plaid_access_tokens") or ""
    
    tokens = [t.strip() for t in tokens_str.split(",") if t.strip()]
    return client_id, secret, tokens


# Load settings
_plaid_settings = get_plaid_settings()
PLAID_CLIENT_ID = _plaid_settings.PLAID_CLIENT_ID
PLAID_SECRET = _plaid_settings.PLAID_SECRET.get_secret_value() if _plaid_settings.PLAID_SECRET else None
PLAID_ENV = _plaid_settings.PLAID_ENV
PLAID_ACCESS_TOKENS = _plaid_settings.PLAID_ACCESS_TOKENS

LOW_BALANCE_THRESHOLD = _plaid_settings.LOW_BALANCE_THRESHOLD
PLAID_POLL_INTERVAL_SECONDS = _plaid_settings.PLAID_POLL_INTERVAL_SECONDS
PLAID_HOURLY_UPDATE_INTERVAL_SECONDS = int(
    os.getenv("PLAID_HOURLY_UPDATE_INTERVAL_SECONDS", "3600")
)


def _format_net_cash(net_cash: float) -> str:
    """Format net cash value with sign indicator."""
    return f"+${net_cash:.2f}" if net_cash >= 0 else f"-${abs(net_cash):.2f}"


def _ensure_visibility_tables(conn: sqlite3.Connection) -> None:
    """
    Ensure all required tables exist with proper schema.
    Uses safe schema migration with whitelisted column names.
    """
    c = conn.cursor()
    
    # Create tables with explicit schema
    c.execute("""
    CREATE TABLE IF NOT EXISTS plaid_accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        plaid_account_id TEXT UNIQUE,
        name TEXT,
        official_name TEXT,
        mask TEXT,
        type TEXT,
        subtype TEXT,
        institution_name TEXT,
        currency TEXT DEFAULT 'USD',
        current_balance REAL,
        available_balance REAL,
        credit_limit REAL,
        last_synced_at TEXT,
        active INTEGER DEFAULT 1,
        user_id TEXT
    )
    """)
    
    c.execute("""
    CREATE TABLE IF NOT EXISTS balance_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        captured_at TEXT NOT NULL,
        account_id TEXT,
        account_name TEXT,
        account_type TEXT,
        account_subtype TEXT,
        current_balance REAL,
        available_balance REAL,
        credit_limit REAL,
        liquid_balance REAL,
        debt_balance REAL,
        net_worth_contribution REAL,
        user_id TEXT
    )
    """)
    
    c.execute("""
    CREATE TABLE IF NOT EXISTS financial_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        captured_at TEXT NOT NULL,
        checking_balance REAL,
        total_liquid REAL,
        total_credit_debt REAL,
        net_cash REAL,
        total_depository REAL,
        total_investment REAL,
        total_credit REAL,
        total_loan REAL,
        net_worth REAL,
        user_id TEXT
    )
    """)
    
    # Safe schema migration with whitelisted columns
    override_columns = {
        "pending_transaction_id": "TEXT",
        "override_amount": "REAL",
        "override_reason": "TEXT",
        "override_at": "TEXT",
        "override_source": "TEXT",
    }
    
    for column, dtype in override_columns.items():
        # Validate column name against whitelist
        if not validate_column_name(column):
            logger.warning(f"Skipping invalid column name: {column}")
            continue
        
        try:
            c.execute(
                f"ALTER TABLE transactions ADD COLUMN {column} {dtype}"
            )
        except sqlite3.OperationalError as e:
            # Column already exists - this is expected
            if "duplicate column" not in str(e).lower():
                logger.warning(f"Schema migration failed for {column}: {e}")
    
    # Create indexes
    c.execute("CREATE INDEX IF NOT EXISTS idx_balance_snapshots_captured ON balance_snapshots(captured_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_balance_snapshots_account ON balance_snapshots(account_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_financial_snapshots_captured ON financial_snapshots(captured_at)")
    
    # Add user_id columns safely
    tables_with_user_id = ("plaid_accounts", "balance_snapshots", "financial_snapshots", "transactions")
    for table in tables_with_user_id:
        if not validate_table_name(table):
            logger.warning(f"Skipping invalid table name: {table}")
            continue
        try:
            c.execute(f"ALTER TABLE {table} ADD COLUMN user_id TEXT")
        except sqlite3.OperationalError:
            pass  # Column already exists
    
    c.execute("CREATE INDEX IF NOT EXISTS idx_financial_snapshots_user_id ON financial_snapshots(user_id)")
    conn.commit()

def _ensure_cursor_table(conn):
    _ensure_visibility_tables(conn)
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS plaid_cursors (
            user_id TEXT NOT NULL,
            token_key TEXT NOT NULL,
            cursor TEXT,
            PRIMARY KEY (user_id, token_key)
        )
        """
    )
    for table in ("plaid_accounts", "balance_snapshots", "financial_snapshots", "transactions"):
        try:
            c.execute(f"ALTER TABLE {table} ADD COLUMN user_id TEXT")
        except Exception: pass
    c.execute("CREATE INDEX IF NOT EXISTS idx_financial_snapshots_user_id ON financial_snapshots(user_id)")
    conn.commit()

def _get_cursor(conn, user_id: str, token_key: str) -> str | None:
    c = conn.cursor()
    c.execute("SELECT cursor FROM plaid_cursors WHERE user_id = ? AND token_key = ?", (str(user_id), token_key))
    row = c.fetchone()
    return row[0] if row else None

def _set_cursor(conn, user_id: str, token_key: str, cursor: str | None):
    c = conn.cursor()
    c.execute(
        """
        INSERT INTO plaid_cursors(user_id, token_key, cursor)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id, token_key) DO UPDATE SET cursor=excluded.cursor
        """,
        (str(user_id), token_key, cursor),
    )
    conn.commit()

async def get_plaid_balances(client: httpx.AsyncClient, user_id=None) -> dict:
    client_id, secret, tokens = _get_user_plaid_creds(user_id=user_id)
    all_accounts = []
    for token in tokens:
        url = f"https://{PLAID_ENV}.plaid.com/accounts/balance/get"
        payload = {"client_id": client_id, "secret": secret, "access_token": token}
        try:
            resp = await client.post(url, json=payload, timeout=30.0)
            if resp.status_code == 200:
                all_accounts.extend(resp.json().get("accounts", []))
        except Exception as exc:
            print(f" Plaid balance error: {exc}")
    return {"accounts": all_accounts}

def _compute_checking_position(accounts: list) -> dict:
    checking_balance = 0.0
    total_credit_debt = 0.0
    credit_cards = []
    for acc in accounts:
        balances = acc.get("balances", {})
        current = balances.get("current") or 0.0
        available = balances.get("available")
        limit_amt = balances.get("limit")
        if acc.get("subtype") == "checking":
            checking_balance += (available if available is not None else current)
        elif acc.get("type") == "credit":
            total_credit_debt += current
            credit_cards.append({"name": acc.get("name"), "current": current, "limit": limit_amt})
    return {"checking_balance": checking_balance, "total_credit_debt": total_credit_debt, "net_cash": checking_balance - total_credit_debt, "credit_cards": credit_cards}

async def _store_account_and_balance_history(conn, accounts, pos, user_id: str):
    _ensure_visibility_tables(conn)
    c = conn.cursor()
    captured_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total_depository = total_investment = total_credit = total_loan = net_worth = 0.0
    for acc in accounts:
        balances = acc.get("balances", {})
        current = balances.get("current")
        available = balances.get("available")
        limit_amt = balances.get("limit")
        acc_type = acc.get("type")
        subtype = acc.get("subtype")
        if acc_type == "depository": total_depository += float(current or 0.0)
        elif acc_type == "investment": total_investment += float(current or 0.0)
        elif acc_type == "credit": total_credit += float(current or 0.0)
        elif acc_type == "loan": total_loan += float(current or 0.0)
        contribution = float(current or 0.0)
        if acc_type in {"credit", "loan"}: contribution = -abs(contribution)
        net_worth += contribution
        c.execute(
            """
            INSERT INTO plaid_accounts (plaid_account_id, name, official_name, mask, type, subtype, current_balance, available_balance, credit_limit, last_synced_at, active, user_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(plaid_account_id) DO UPDATE SET name=excluded.name, official_name=excluded.official_name, mask=excluded.mask, type=excluded.type, subtype=excluded.subtype, current_balance=excluded.current_balance, available_balance=excluded.available_balance, credit_limit=excluded.credit_limit, last_synced_at=excluded.last_synced_at, active=1
            """,
            (acc.get("account_id"), acc.get("name"), acc.get("official_name"), acc.get("mask"), acc_type, subtype, current, available, limit_amt, captured_at, user_id),
        )
        liquid = float(available if available is not None else current or 0.0) if acc_type == "depository" else 0.0
        debt = float(current or 0.0) if acc_type in {"credit", "loan"} else 0.0
        c.execute(
            """
            INSERT INTO balance_snapshots (captured_at, account_id, account_name, account_type, account_subtype, current_balance, available_balance, credit_limit, liquid_balance, debt_balance, net_worth_contribution, user_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (captured_at, acc.get("account_id"), acc.get("name"), acc_type, subtype, current, available, limit_amt, liquid, debt, contribution, user_id),
        )
    c.execute(
        """
        INSERT INTO financial_snapshots (captured_at, checking_balance, total_liquid, total_credit_debt, net_cash, total_depository, total_investment, total_credit, total_loan, net_worth, user_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (captured_at, pos["checking_balance"], total_depository, total_credit + total_loan, pos["net_cash"], total_depository, total_investment, total_credit, total_loan, net_worth, user_id),
    )
    conn.commit()

async def run_hourly_balance_update(conn, bot, channel_id: int, *, user_id=None, post_discord: bool = True):
    async with httpx.AsyncClient() as client:
        balance_data = await get_plaid_balances(client, user_id=user_id)
    accounts = balance_data.get("accounts", [])
    if not accounts: return None
    pos = _compute_checking_position(accounts)
    await _store_account_and_balance_history(conn, accounts, pos, user_id=str(user_id))
    card_summary = " | ".join(f"{c['name']}: ${c['current']:.2f}" for c in pos["credit_cards"]) or "No active debt"
    all_balances_snapshot = f"Checking: ${pos['checking_balance']:.2f} | Cards: {card_summary}"
    txn_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c = conn.cursor()
    c.execute(
        """
        INSERT INTO transactions (transaction_id, message_id, date, merchant, amount, account_used, total_liquid, net_cash, all_balances, status, judgment, user_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (f"sync_{int(datetime.now().timestamp())}", None, txn_date, "System Balance Sync", 0.0, "Plaid Sync", pos["checking_balance"], _format_net_cash(pos["net_cash"]), all_balances_snapshot, "Evaluated", "Balance audit synced.", user_id),
    )
    conn.commit()
    if bot and channel_id and post_discord:
        channel = bot.get_channel(channel_id)
        if channel:
            msg = f" **Hourly Balance Update**\n Liquid Checking: ${pos['checking_balance']:.2f}\n Cards: {card_summary}\n Net Cash: {_format_net_cash(pos['net_cash'])}"
            await channel.send(msg)
    return pos

async def _handle_new_transaction(conn, tx_queue: asyncio.Queue, txn: dict, account_map: dict, bot=None, channel_id: int | None = None, send_alert: bool = True, post_discord: bool = True, *, user_id: str):
    used_account = account_map.get(txn.get("account_id"))
    account_name = used_account.get("name") if used_account else "Unknown Account"
    amount = float(txn.get("amount", 0.0))
    raw_timestamp = txn.get("datetime") or txn.get("authorized_datetime") or txn.get("date")
    txn_date = raw_timestamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    transaction_id = txn.get("transaction_id")
    pending_transaction_id = txn.get("pending_transaction_id")
    merchant_name = txn.get("name")
    total_liquid = total_debt = 0.0
    for acc in account_map.values():
        balances = acc.get("balances", {})
        current = balances.get("current") or 0.0
        if acc.get("type") == "depository":
            available = balances.get("available")
            total_liquid += (available if available is not None else current)
        elif acc.get("type") == "credit": total_debt += current
    net_cash = total_liquid - total_debt
    c = conn.cursor()
    if pending_transaction_id: c.execute("DELETE FROM transactions WHERE transaction_id = ?", (pending_transaction_id,))
    c.execute(
        """
        INSERT OR IGNORE INTO transactions (transaction_id, pending_transaction_id, message_id, date, merchant, amount, account_used, total_liquid, net_cash, status, user_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (transaction_id, pending_transaction_id, None, txn_date, merchant_name, amount, account_name, total_liquid, _format_net_cash(net_cash), "Pending Evaluation", user_id),
    )
    conn.commit()
    if send_alert and post_discord:
        await _send_transaction_alert(bot, channel_id, merchant_name, amount, account_name, total_liquid, net_cash, transaction_id, txn_date)

async def _handle_modified_transaction(conn, tx_queue: asyncio.Queue, txn: dict, *, user_id: str):
    transaction_id = txn.get("transaction_id")
    if not transaction_id: return
    raw_timestamp = txn.get("datetime") or txn.get("authorized_datetime") or txn.get("date")
    c = conn.cursor()
    c.execute(
        """
        UPDATE transactions SET date = ?, merchant = ?, amount = CASE WHEN override_amount IS NULL THEN ? ELSE amount END, status = ?, pending_transaction_id = ? WHERE transaction_id = ?
        """,
        (raw_timestamp, txn.get("name"), float(txn.get("amount", 0.0)), "Pending Evaluation" if txn.get("pending") else "Evaluated", txn.get("pending_transaction_id"), transaction_id),
    )
    conn.commit()

def _handle_removed_transaction(conn, removed: dict):
    tid = removed.get("transaction_id")
    if not tid: return
    c = conn.cursor()
    c.execute("UPDATE transactions SET status = 'Removed' WHERE transaction_id = ?", (tid,))
    conn.commit()

async def sync_plaid_transactions(conn, tx_queue: asyncio.Queue, bot=None, channel_id: int | None = None, *, post_discord: bool = True, user_id: str):
    user_id = str(user_id or "").strip()
    if not user_id: raise ValueError("user_id is required")
    client_id, secret, tokens = _get_user_plaid_creds(user_id=user_id)
    _ensure_cursor_table(conn)
    async with httpx.AsyncClient() as client:
        balance_data = await get_plaid_balances(client, user_id=user_id)
        account_map = {acc["account_id"]: acc for acc in balance_data.get("accounts", [])}
        for token in tokens:
            token_key = f"CURSOR_{token[-10:]}"
            cursor = _get_cursor(conn, user_id, token_key)
            is_initial_sync, has_more, count = cursor is None, True, 0
            while has_more:
                url = f"https://{PLAID_ENV}.plaid.com/transactions/sync"
                payload = {"client_id": client_id, "secret": secret, "access_token": token}
                if cursor: payload["cursor"] = cursor
                try:
                    resp = await client.post(url, json=payload, timeout=30.0)
                    if resp.status_code != 200: break
                    data = resp.json()
                    for txn in data.get("added", []):
                        await _handle_new_transaction(conn, tx_queue, txn, account_map, bot, channel_id, send_alert=not is_initial_sync, post_discord=post_discord, user_id=user_id)
                        if is_initial_sync: count += 1
                    for txn in data.get("modified", []): await _handle_modified_transaction(conn, tx_queue, txn, user_id=user_id)
                    for r in data.get("removed", []): _handle_removed_transaction(conn, r)
                    cursor = data.get("next_cursor")
                    has_more = bool(data.get("has_more", False))
                except Exception: break
            _set_cursor(conn, user_id, token_key, cursor)
            if is_initial_sync and count and bot and channel_id and post_discord:
                channel = bot.get_channel(channel_id)
                if channel: await channel.send(f" **Initial sync complete** for account ending `...{token[-6:]}`: {count} transactions imported.")

async def _send_transaction_alert(bot, channel_id, merchant, amount, account_name, total_liquid, net_cash, transaction_id, txn_date):
    if not bot or not channel_id: return
    channel = bot.get_channel(channel_id)
    if not channel: return
    is_purchase = amount > 0
    action = "New Purchase Detected" if is_purchase else "Payment/Refund Received"
    await channel.send(f" **{action}**\n Merchant: {merchant or 'Unknown'}\n Amount: ${abs(amount):.2f}\n Account Used: {account_name}\n Total Liquid Cash: ${total_liquid:.2f}\n Net Cash: {_format_net_cash(net_cash)}\n Date & Time: {txn_date}\n Transaction ID: `{transaction_id}`")

async def run_plaid_accounting_pass(conn, tx_queue: asyncio.Queue, bot=None, channel_id: int | None = None, *, post_discord: bool = True, user_id: str):
    import src.core.state as state
    state.PLAID_SYNC_STATE["active"] = True
    try:
        await sync_plaid_transactions(conn, tx_queue, bot, channel_id, post_discord=post_discord, user_id=user_id)
        return await run_hourly_balance_update(conn, bot, channel_id, user_id=user_id, post_discord=post_discord)
    finally: state.PLAID_SYNC_STATE["active"] = False

async def plaid_polling_loop(conn, tx_queue: asyncio.Queue, bot, channel_id: int):
    import glob
    import src.core.state as state
    from src.db.queries import PLAID_SYNC_STATE
    last_balance_update = {}
    while True:
        try:
            for db_path in glob.glob("data/finances_*.db"):
                uid = db_path.split("_")[-1].split(".")[0]
                state.CURRENT_USER_ID.set(uid)
                client_id, secret, tokens = _get_user_plaid_creds(user_id=uid)
                if not tokens: continue
                PLAID_SYNC_STATE[uid] = True
                try:
                    await sync_plaid_transactions(conn, tx_queue, bot, channel_id, post_discord=True, user_id=uid)
                    now = asyncio.get_event_loop().time()
                    if now - last_balance_update.get(uid, 0.0) >= PLAID_HOURLY_UPDATE_INTERVAL_SECONDS:
                        await run_hourly_balance_update(conn, bot, channel_id, post_discord=True, user_id=uid)
                        last_balance_update[uid] = now
                finally: PLAID_SYNC_STATE[uid] = False
        except Exception as exc: print(f" Plaid polling loop error: {exc}")
        await asyncio.sleep(PLAID_POLL_INTERVAL_SECONDS)



def _ensure_cursor_table(conn: sqlite3.Connection) -> None:
    """Ensure cursor table exists with proper schema."""
    _ensure_visibility_tables(conn)
    c = conn.cursor()
    
    c.execute("""
    CREATE TABLE IF NOT EXISTS plaid_cursors (
        user_id TEXT NOT NULL,
        token_key TEXT NOT NULL,
        cursor TEXT,
        PRIMARY KEY (user_id, token_key)
    )
    """)
    
    # Add user_id columns safely (idempotent)
    tables_with_user_id = ("plaid_accounts", "balance_snapshots", "financial_snapshots", "transactions")
    for table in tables_with_user_id:
        if not validate_table_name(table):
            logger.warning(f"Skipping invalid table name: {table}")
            continue
        try:
            c.execute(f"ALTER TABLE {table} ADD COLUMN user_id TEXT")
        except sqlite3.OperationalError:
            pass
    
    c.execute("CREATE INDEX IF NOT EXISTS idx_financial_snapshots_user_id ON financial_snapshots(user_id)")
    conn.commit()


def _get_cursor(conn: sqlite3.Connection, user_id: str, token_key: str) -> Optional[str]:
    """Get sync cursor for a user/token combination."""
    c = conn.cursor()
    c.execute(
        "SELECT cursor FROM plaid_cursors WHERE user_id = ? AND token_key = ?",
        (str(user_id), token_key)
    )
    row = c.fetchone()
    return row[0] if row else None


def _set_cursor(conn: sqlite3.Connection, user_id: str, token_key: str, cursor: Optional[str]) -> None:
    """Set sync cursor for a user/token combination."""
    c = conn.cursor()
    c.execute(
        """
        INSERT INTO plaid_cursors(user_id, token_key, cursor)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id, token_key) DO UPDATE SET cursor=excluded.cursor
        """,
        (str(user_id), token_key, cursor),
    )
    conn.commit()


async def get_plaid_balances(client: httpx.AsyncClient, user_id: Optional[str] = None) -> dict:
    """
    Fetch account balances from Plaid API.
    
    Args:
        client: HTTPX async client
        user_id: User identifier
    
    Returns:
        Dictionary with 'accounts' key containing list of accounts
    """
    client_id, secret, tokens = _get_user_plaid_creds(user_id=user_id)
    all_accounts = []
    
    for token in tokens:
        url = f"https://{PLAID_ENV}.plaid.com/accounts/balance/get"
        payload = {"client_id": client_id, "secret": secret, "access_token": token}
        
        try:
            resp = await client.post(url, json=payload, timeout=30.0)
            if resp.status_code == 200:
                all_accounts.extend(resp.json().get("accounts", []))
            else:
                logger.warning(f"Plaid balance request failed: {resp.status_code}")
        except httpx.HTTPError as exc:
            logger.error(f"Plaid balance HTTP error: {exc}")
        except Exception as exc:
            logger.error(f"Plaid balance error: {exc}", exc_info=True)
    
    return {"accounts": all_accounts}


def _compute_checking_position(accounts: list) -> dict:
    """
    Compute checking account position and credit card debt.
    
    Args:
        accounts: List of Plaid account objects
    
    Returns:
        Dictionary with checking_balance, total_credit_debt, net_cash, credit_cards
    """
    checking_balance = 0.0
    total_credit_debt = 0.0
    credit_cards = []
    
    for acc in accounts:
        balances = acc.get("balances", {})
        current = balances.get("current") or 0.0
        available = balances.get("available")
        limit_amt = balances.get("limit")
        
        if acc.get("subtype") == "checking":
            checking_balance += (available if available is not None else current)
        elif acc.get("type") == "credit":
            total_credit_debt += current
            credit_cards.append({
                "name": acc.get("name"),
                "current": current,
                "limit": limit_amt
            })
    
    return {
        "checking_balance": checking_balance,
        "total_credit_debt": total_credit_debt,
        "net_cash": checking_balance - total_credit_debt,
        "credit_cards": credit_cards
    }


async def _store_account_and_balance_history(
    conn: sqlite3.Connection,
    accounts: list,
    pos: dict,
    user_id: str
) -> None:
    """Store account information and balance history."""
    _ensure_visibility_tables(conn)
    c = conn.cursor()
    captured_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    total_depository = total_investment = total_credit = total_loan = net_worth = 0.0
    
    for acc in accounts:
        balances = acc.get("balances", {})
        current = balances.get("current")
        available = balances.get("available")
        limit_amt = balances.get("limit")
        acc_type = acc.get("type")
        subtype = acc.get("subtype")
        
        # Aggregate by type
        if acc_type == "depository":
            total_depository += float(current or 0.0)
        elif acc_type == "investment":
            total_investment += float(current or 0.0)
        elif acc_type == "credit":
            total_credit += float(current or 0.0)
        elif acc_type == "loan":
            total_loan += float(current or 0.0)
        
        # Calculate net worth contribution
        contribution = float(current or 0.0)
        if acc_type in {"credit", "loan"}:
            contribution = -abs(contribution)
        net_worth += contribution
        
        # Store account info
        c.execute("""
        INSERT INTO plaid_accounts (
            plaid_account_id, name, official_name, mask, type, subtype,
            current_balance, available_balance, credit_limit, last_synced_at, active, user_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
        ON CONFLICT(plaid_account_id) DO UPDATE SET
            name=excluded.name,
            official_name=excluded.official_name,
            mask=excluded.mask,
            type=excluded.type,
            subtype=excluded.subtype,
            current_balance=excluded.current_balance,
            available_balance=excluded.available_balance,
            credit_limit=excluded.credit_limit,
            last_synced_at=excluded.last_synced_at,
            active=1
        """, (
            acc.get("account_id"),
            acc.get("name"),
            acc.get("official_name"),
            acc.get("mask"),
            acc_type,
            subtype,
            current,
            available,
            limit_amt,
            captured_at,
            user_id,
        ))
        
        # Store balance snapshot
        liquid = float(available if available is not None else current or 0.0) if acc_type == "depository" else 0.0
        debt = float(current or 0.0) if acc_type in {"credit", "loan"} else 0.0
        
        c.execute("""
        INSERT INTO balance_snapshots (
            captured_at, account_id, account_name, account_type, account_subtype,
            current_balance, available_balance, credit_limit,
            liquid_balance, debt_balance, net_worth_contribution, user_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            captured_at,
            acc.get("account_id"),
            acc.get("name"),
            acc_type,
            subtype,
            current,
            available,
            limit_amt,
            liquid,
            debt,
            contribution,
            user_id,
        ))
    
    # Store financial snapshot
    c.execute("""
    INSERT INTO financial_snapshots (
        captured_at, checking_balance, total_liquid, total_credit_debt, net_cash,
        total_depository, total_investment, total_credit, total_loan, net_worth, user_id
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        captured_at,
        pos["checking_balance"],
        total_depository,
        total_credit + total_loan,
        pos["net_cash"],
        total_depository,
        total_investment,
        total_credit,
        total_loan,
        net_worth,
        user_id,
    ))
    
    conn.commit()


async def run_hourly_balance_update(
    conn: sqlite3.Connection,
    bot,
    channel_id: int,
    *,
    user_id: Optional[str] = None,
    post_discord: bool = True
) -> Optional[dict]:
    """Run hourly balance update and optionally post to Discord."""
    async with httpx.AsyncClient() as client:
        balance_data = await get_plaid_balances(client, user_id=user_id)
    
    accounts = balance_data.get("accounts", [])
    if not accounts:
        logger.warning("No accounts returned from Plaid")
        return None
    
    pos = _compute_checking_position(accounts)
    await _store_account_and_balance_history(conn, accounts, pos, user_id=str(user_id))
    
    card_summary = " | ".join(
        f"{c['name']}: ${c['current']:.2f}" for c in pos["credit_cards"]
    ) or "No active debt"
    
    all_balances_snapshot = f"Checking: ${pos['checking_balance']:.2f} | Cards: {card_summary}"
    txn_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    c = conn.cursor()
    c.execute("""
    INSERT INTO transactions (
        transaction_id, message_id, date, merchant, amount, account_used,
        total_liquid, net_cash, all_balances, status, judgment, user_id
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        f"sync_{int(datetime.now().timestamp())}",
        None,
        txn_date,
        "System Balance Sync",
        0.0,
        "Plaid Sync",
        pos["checking_balance"],
        _format_net_cash(pos["net_cash"]),
        all_balances_snapshot,
        "Evaluated",
        "Balance audit synced.",
        user_id,
    ))
    conn.commit()
    
    if bot and channel_id and post_discord:
        channel = bot.get_channel(channel_id)
        if channel:
            msg = (
                f" **Hourly Balance Update**\n"
                f" Liquid Checking: ${pos['checking_balance']:.2f}\n"
                f" Cards: {card_summary}\n"
                f" Net Cash: {_format_net_cash(pos['net_cash'])}"
            )
            await channel.send(msg)
    
    return pos
