"""B1 noVNC login intake — command-layer tests (flag gates, allowlist, spawn)."""

import pytest

from src.bot import concierge_login as cl
from src.services.concierge.audit import AuditLog
from src.services.concierge.browser_sessions import BrowserSessionStore
from src.services.concierge.tenants import TenantStore

DB = "cmd-test.db"


class FakeAuthor:
    def __init__(self, uid):
        self.id = uid
        self.sent_messages = []

    async def send(self, content, view=None):
        self.sent_messages.append({"content": content, "view": view})


class FakeCtx:
    def __init__(self, uid):
        self.author = FakeAuthor(uid)
        self.sent_replies = []

    async def send(self, content):
        self.sent_replies.append(content)


@pytest.fixture()
def stores(tmp_path, monkeypatch):
    db = str(tmp_path / DB)
    tenants = TenantStore(db, admins=["user:99"])
    sessions = BrowserSessionStore(db)
    audit = AuditLog(db)
    monkeypatch.setattr(cl, "TENANTS", tenants)
    monkeypatch.setattr(cl, "SESSIONS", sessions)
    monkeypatch.setattr(cl, "AUDIT", audit)
    monkeypatch.setattr(cl, "browser_enabled", lambda: True)
    monkeypatch.setattr(cl, "docker_spawn", lambda argv: "ok")
    return {"tenants": tenants, "sessions": sessions, "audit": audit}


def enable(tenants, tenant, domain):
    tenants.enable("user:99", tenant, tier="read")
    tenants.allow_domain("user:99", tenant, domain)


async def test_login_refused_when_not_enabled(stores):
    ctx = FakeCtx(1)
    await cl.handle_login(ctx, "amazon.com")
    assert ctx.sent_replies
    assert "not enabled" in ctx.sent_replies[0]


async def test_login_refused_when_domain_not_allowlisted(stores):
    stores["tenants"].enable("user:99", "user:1", tier="read")
    ctx = FakeCtx(1)
    await cl.handle_login(ctx, "amazon.com")
    assert ctx.sent_replies
    assert "allowlist" in ctx.sent_replies[0]


async def test_login_refused_when_browser_disabled(stores, monkeypatch):
    enable(stores["tenants"], "user:1", "amazon.com")
    monkeypatch.setattr(cl, "browser_enabled", lambda: False)
    ctx = FakeCtx(1)
    await cl.handle_login(ctx, "amazon.com")
    assert ctx.sent_replies
    assert "disabled" in ctx.sent_replies[0]


async def test_login_refused_on_invalid_domain(stores):
    ctx = FakeCtx(1)
    await cl.handle_login(ctx, "https://evil.com/../../x")
    assert ctx.sent_replies
    assert "plain hostname" in ctx.sent_replies[0]


async def test_login_spawns_session_and_dms_novnc_link(stores):
    enable(stores["tenants"], "user:1", "amazon.com")
    ctx = FakeCtx(1)
    await cl.handle_login(ctx, "amazon.com")
    assert ctx.author.sent_messages
    msg = ctx.author.sent_messages[0]["content"]
    assert "amazon.com" in msg
    view = ctx.author.sent_messages[0]["view"]
    assert view is not None
    assert view.session["domain"] == "amazon.com"
    sess = stores["sessions"].list_for("user:1")
    assert len(sess) == 1
    assert sess[0]["status"] == "open"


async def test_done_button_flows_profile_into_vault(stores, tmp_path):
    from src.security.vault import Vault

    enable(stores["tenants"], "user:1", "amazon.com")
    vault = Vault(str(tmp_path / DB), master_key="cmd-test-key")
    serv = cl.SESSIONS.create("user:1", "amazon.com login", "amazon.com", 7200)
    prof = tmp_path / "p"
    (prof / "Default").mkdir(parents=True)
    (prof / "Default" / "Preferences").write_text("cookies-json")

    def fake_fetch(container_name, dest_name):
        return str(prof)

    view = cl.LoginSessionView(
        serv["session_id"], 1, serv,
        store=cl.SESSIONS, vault=vault, fetcher=fake_fetch,
        remover=lambda name: None,
    )
    rec = cl.complete_login_session(
        cl.SESSIONS, vault, serv["session_id"], "user:1", fake_fetch,
        consumer_scope=["amazon.com"],
    )
    assert rec["status"] == "stored"
    assert rec["kind"] == "session_profile"
    assert cl.SESSIONS.get(serv["session_id"])["status"] == "done"
    assert vault.get_record("user:1", rec["vault_ref"])["kind"] == "session_profile"
    assert view.session_id == serv["session_id"]


async def test_done_button_owner_check_present(stores):
    enable(stores["tenants"], "user:1", "amazon.com")
    serv = cl.SESSIONS.create("user:1", "amazon.com login", "amazon.com", 7201)
    view = cl.LoginSessionView(serv["session_id"], 1, serv, store=cl.SESSIONS)
    assert view._is_owner is not None
    labels = []
    for child in view.children:
        labels.append(str(getattr(child, "label", "")))
    assert any("Done" in label for label in labels)
    assert any("Cancel" in label for label in labels)