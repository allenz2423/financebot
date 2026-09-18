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


class FakeResponse:
    async def defer(self, ephemeral=True):
        pass


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content, ephemeral=True):
        self.sent.append(content)


class FakeMessage:
    async def edit(self, **kwargs):
        pass


class FakeInteraction:
    def __init__(self, uid):
        self.user = FakeAuthor(uid)
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.message = FakeMessage()


async def test_cancel_removes_container_even_when_session_expired(stores):
    """Cancel must still delete the disposable container when the session row
    has already aged out of 'open' (kill() would raise otherwise)."""
    from datetime import timedelta

    enable(stores["tenants"], "user:1", "amazon.com")
    serv = cl.SESSIONS.create("user:1", "amazon.com login", "amazon.com", 7202)

    removed = []
    view = cl.LoginSessionView(
        serv["session_id"], 1, serv,
        store=cl.SESSIONS, remover=lambda name: removed.append(name),
    )
    # Force the row out of 'open' before the user clicks Cancel.
    cl.SESSIONS.expire_stale(ttl=timedelta(seconds=-1))
    assert cl.SESSIONS.get(serv["session_id"])["status"] == "expired"

    cancel_btn = next(
        child for child in view.children
        if "Cancel" in str(getattr(child, "label", ""))
    )
    await cancel_btn.callback(FakeInteraction(1))
    assert removed == [serv["container_name"]]


async def test_reaper_removes_terminal_and_superseded_containers(stores):
    """The periodic reaper reclaims the container for a terminal session and
    for any superseded login, while leaving the newest ready browser alone."""
    import time

    enable(stores["tenants"], "user:1", "amazon.com")
    old = cl.SESSIONS.create("user:1", "amazon.com login", "amazon.com", 7210)
    time.sleep(0.01)
    new = cl.SESSIONS.create("user:1", "amazon.com login", "amazon.com", 7211)
    dead = cl.SESSIONS.create("user:1", "ebay login", "ebay.com", 7212)
    cl.SESSIONS.kill(dead["session_id"], "user:1")

    removed = []
    out = cl.reap_expired_sessions(remover=removed.append)
    assert old["container_name"] in removed
    assert dead["container_name"] in removed
    assert new["container_name"] not in removed
    # Reclaimed sessions are marked terminal so they are not reaped twice.
    assert cl.SESSIONS.get(old["session_id"])["status"] == "reaped"
    # And a second sweep leaves nothing to do.
    assert cl.reap_expired_sessions(remover=removed.append) == []
    assert sorted(out) == sorted(removed)


async def test_reaper_tolerates_missing_container(stores):
    """`docker rm` on an already-removed container must not abort the sweep."""
    enable(stores["tenants"], "user:1", "amazon.com")
    sess = cl.SESSIONS.create("user:1", "amazon.com login", "amazon.com", 7213)
    cl.SESSIONS.kill(sess["session_id"], "user:1")

    def _boom(name):
        raise RuntimeError("No such container: " + name)

    out = cl.reap_expired_sessions(remover=_boom)
    assert sess["container_name"] in out


async def test_reaper_loop_is_noop_when_browser_disabled(stores, monkeypatch):
    """With the concierge browser off the loop must return immediately, not
    spin or shell out to docker."""
    monkeypatch.setattr(cl, "browser_enabled", lambda: False)
    await cl.concierge_browser_reaper_loop()  # returns without looping


async def test_login_retries_next_port_when_host_port_is_taken(stores, monkeypatch):
    """A host port held by an orphaned container this process cannot see must
    not fail the login: the spawn retries on the next port."""
    enable(stores["tenants"], "user:1", "amazon.com")
    monkeypatch.setattr(cl, "docker_published_host_ports", lambda: set())

    calls = []

    def flaky_spawn(argv):
        calls.append(argv)
        if len(calls) == 1:
            raise RuntimeError(
                "Bind for 127.0.0.1:6081 failed: port is already allocated"
            )
        return "ok"

    monkeypatch.setattr(cl, "docker_spawn", flaky_spawn)
    ctx = FakeCtx(1)
    await cl.handle_login(ctx, "amazon.com")

    assert len(calls) == 2, calls
    # The success DM (not the "Couldn't start" error) was sent.
    assert ctx.author.sent_messages
    assert "Couldn't start" not in ctx.author.sent_messages[0]["content"]

    def published_port(argv):
        return argv[argv.index("-p") + 1]

    assert published_port(calls[0]) != published_port(calls[1])
    open_sessions = [
        s for s in stores["sessions"].list_for("user:1") if s["status"] == "open"
    ]
    assert len(open_sessions) == 1