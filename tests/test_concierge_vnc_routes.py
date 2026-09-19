"""noVNC routing: a live browser keeps serving; a dead one degrades readably.

Regression guard for the "Host Error" the user saw on
``concierge.incoming.nyc/concierge/vnc/<id>/vnc.html``: a still-running
container whose DB row had lapsed to ``expired`` 404'd, and a reused row whose
``--rm`` container was gone 502'd (the edge proxy renders a raw 5xx as an
opaque "Host Error" page).
"""

from datetime import timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.services.concierge import routes as R
from src.services.concierge.browser_sessions import BrowserSessionStore


def _client(monkeypatch, tmp_path):
    store = BrowserSessionStore(str(tmp_path / "v.db"))
    # _vnc_session reads the production DB path; point it at this tmp store.
    monkeypatch.setattr(R, "BrowserSessionStore", lambda _db: store)
    app = FastAPI()
    app.include_router(R.vnc_router)
    return TestClient(app), store


class _FakeResp:
    status_code = 200
    content = b"<html>noVNC canvas</html>"
    headers = {"content-type": "text/html"}


class _FakeClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def request(self, *args, **kwargs):
        return _FakeResp()


def test_lapsed_but_running_session_still_proxies(monkeypatch, tmp_path):
    client, store = _client(monkeypatch, tmp_path)
    sess = store.create("user:1", "amazon login", "amazon.com", 6099)
    store.expire_stale(ttl=timedelta(seconds=-1))  # TTL lapsed, container up
    assert store.get(sess["session_id"])["status"] == "expired"

    monkeypatch.setattr(R.httpx, "AsyncClient", _FakeClient)
    resp = client.get("/concierge/vnc/" + sess["session_id"] + "/vnc.html")
    assert resp.status_code == 200
    assert "noVNC canvas" in resp.text


def test_vnc_session_serves_open_done_expired_only(monkeypatch, tmp_path):
    _, store = _client(monkeypatch, tmp_path)
    sess = store.create("user:1", "amazon login", "amazon.com", 6098)
    assert R._vnc_session(sess["session_id"]) is not None
    store.expire_stale(ttl=timedelta(seconds=-1))
    assert R._vnc_session(sess["session_id"])["status"] == "expired"
    store.retire(sess["session_id"])
    assert R._vnc_session(sess["session_id"]) is None


def test_dead_upstream_returns_readable_page_not_502(monkeypatch, tmp_path):
    """No container to proxy to: show the "ended" page (404), never a 5xx."""
    client, store = _client(monkeypatch, tmp_path)
    sess = store.create("user:1", "amazon login", "amazon.com", 6097)

    def _boom(*args, **kwargs):
        raise R.httpx.ConnectError("connection refused")

    monkeypatch.setattr(R.httpx, "AsyncClient", _boom)
    resp = client.get("/concierge/vnc/" + sess["session_id"] + "/vnc.html")
    assert resp.status_code == 404
    assert "Browser session ended" in resp.text


def test_unknown_session_is_a_readable_404(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    resp = client.get("/concierge/vnc/ls_nope/vnc.html")
    assert resp.status_code == 404
    assert "Browser session ended" in resp.text