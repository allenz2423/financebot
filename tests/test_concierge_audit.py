"""B0 audit chain tests: append-only, hash-chained, tamper-detecting."""

import sqlite3

import pytest

from src.services.concierge.audit import AuditLog


def make(tmp_path):
    return AuditLog(str(tmp_path / "a.db"))


def test_append_and_verify(tmp_path):
    a = make(tmp_path)
    a.append(actor="user:1", action="admin_enable", tenant="user:2", detail={"tier": "read"})
    a.append(actor="user:1", action="admin_limit", tenant="user:2", detail={"cap": 5.0})
    assert a.count() == 2
    assert a.verify_chain()["ok"] is True
    rows = a.tail(10)
    assert rows[0]["action"] == "admin_limit"


def test_tail_tenant_filter(tmp_path):
    a = make(tmp_path)
    a.append("user:1", "enable", tenant="user:2")
    a.append("user:1", "enable", tenant="user:3")
    assert len(a.tail(10, tenant="user:2")) == 1


def test_verify_detects_detail_tamper(tmp_path):
    a = make(tmp_path)
    a.append(actor="user:1", action="admin_enable", tenant="user:2", detail={"tier": "read"})
    conn = sqlite3.connect(str(tmp_path / "a.db"))
    conn.execute(
        "UPDATE audit_chain SET detail = ? WHERE seq = 1", ('{"tier":"spend"}',)
    )
    conn.commit()
    conn.close()
    res = a.verify_chain()
    assert res["ok"] is False
    assert res["broken_at"] == 1


def test_verify_detects_hole(tmp_path):
    a = make(tmp_path)
    a.append("user:1", "a1")
    a.append("user:1", "a2")
    a.append("user:1", "a3")
    conn = sqlite3.connect(str(tmp_path / "a.db"))
    conn.execute("DELETE FROM audit_chain WHERE seq = 2")
    conn.commit()
    conn.close()
    res = a.verify_chain()
    assert res["ok"] is False


def test_verify_detects_prev_hash_mismatch(tmp_path):
    a = make(tmp_path)
    a.append("user:1", "a1")
    a.append("user:1", "a2")
    conn = sqlite3.connect(str(tmp_path / "a.db"))
    conn.execute("UPDATE audit_chain SET prev_hash = ? WHERE seq = 2", ("f" * 64,))
    conn.commit()
    conn.close()
    res = a.verify_chain()
    assert res["ok"] is False
    assert res["reason"] == "prev_hash mismatch"


def test_detail_must_be_a_dict(tmp_path):
    a = make(tmp_path)
    with pytest.raises(Exception):
        a.append("user:1", "x", detail="not-a-dict")