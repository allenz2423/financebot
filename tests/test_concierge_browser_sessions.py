"""B1 noVNC login-intake tests: session store, docker argv, profile vaulting."""

from pathlib import Path

import pytest

from src.security.profile import pack_profile_dir
from src.security.vault import Vault, VaultError
from src.services.concierge.browser_sessions import (
    BrowserSessionError,
    BrowserSessionStore,
    CONTAINER_PREFIX,
    SessionNotFound,
    SessionNotOpen,
    VNC_PASSWORD_ALPHABET,
    VNC_PASSWORD_LENGTH,
    build_docker_run,
    complete_login_session,
    generate_vnc_password,
)

KEY = "b1-browser-session-test-key"


def make(tmp_path):
    db = str(tmp_path / "s.db")
    return BrowserSessionStore(db), Vault(db, master_key=KEY)


def open_session(store, tenant="user:1", domain="amazon.com", port=6090):
    sess = store.create(tenant, domain + " login", domain, port)
    return sess


def test_generate_vnc_password_is_random_and_safe():
    p1 = generate_vnc_password()
    p2 = generate_vnc_password()
    assert len(p1) == VNC_PASSWORD_LENGTH
    assert len(p2) == VNC_PASSWORD_LENGTH
    assert p1 != p2
    for ch in p1:
        assert ch in VNC_PASSWORD_ALPHABET


def test_create_session_is_open_and_tenant_bound(tmp_path):
    store, _ = make(tmp_path)
    sess = open_session(store)
    assert sess["status"] == "open"
    assert sess["session_id"].startswith("ls_")
    assert sess["container_name"] == CONTAINER_PREFIX + sess["session_id"]
    assert sess["domain"] == "amazon.com"
    with pytest.raises(SessionNotFound):
        store.get(sess["session_id"], tenant="user:2")


def test_next_vnc_port_skips_open_ports(tmp_path):
    store, _ = make(tmp_path)
    first = store.next_vnc_port(base=6090)
    assert first == 6090
    store.create("user:1", "l", "amazon.com", first)
    assert store.next_vnc_port(base=6090) == 6091


def test_next_vnc_port_skips_excluded_host_ports(tmp_path):
    """Ports known busy on the host (e.g. a sibling container's published
    noVNC port) must be skipped even though the in-container OS probe cannot
    see them — the exact 6081 'port is already allocated' spawn failure."""
    store, _ = make(tmp_path)
    assert store.next_vnc_port(base=6090, exclude={6090, 6091}) == 6092


def test_finish_records_vault_ref_and_blocks_double(tmp_path):
    store, vault = make(tmp_path)
    sess = open_session(store)
    done = store.finish(sess["session_id"], "user:1", "profile_x")
    assert done["status"] == "done"
    assert done["vault_ref"] == "profile_x"
    with pytest.raises(SessionNotOpen):
        store.finish(sess["session_id"], "user:1", "profile_y")


def test_kill_and_expire_stale(tmp_path):
    store, _ = make(tmp_path)
    sess = open_session(store)
    killed = store.kill(sess["session_id"], "user:1")
    assert killed["status"] == "killed"
    assert store.expire_stale() >= 0
    fresh = store.create("user:1", "l", "amazon.com", 6095)
    assert fresh["status"] == "open"
    store.expire_stale()  # default TTL is 30 min; fresh stays open
    assert store.get(fresh["session_id"])["status"] == "open"


def test_build_docker_run_cpu_mode(tmp_path):
    store, _ = make(tmp_path)
    sess = open_session(store)
    argv = build_docker_run(sess, accel="cpu")
    assert argv[0] == "docker" and argv[1] == "run"
    assert "--rm" in argv
    assert "--name" in argv and CONTAINER_PREFIX + sess["session_id"] in argv
    assert "-e" in argv and "VNC_PASSWORD=" + sess["vnc_password"] in argv
    assert "127.0.0.1:" + str(sess["vnc_port"]) + ":6080" in argv
    assert any(a.startswith("--tmpfs") for a in argv)
    assert "--device" not in argv
    assert argv[-1] == "amazon.com"


def test_build_docker_run_igpu_requires_drm(tmp_path):
    store, _ = make(tmp_path)
    sess = open_session(store)
    with pytest.raises(BrowserSessionError, match="igpu requires"):
        build_docker_run(sess, accel="igpu", drm_device="", drm_gid="")
    argv = build_docker_run(
        sess, accel="igpu", drm_device="/dev/dri/renderD129", drm_gid="105",
    )
    assert "--device" in argv and "/dev/dri/renderD129:/dev/dri/renderD129" in argv
    assert "--group-add" in argv and "105" in argv


def test_build_docker_run_injects_extra_env(tmp_path):
    store, _ = make(tmp_path)
    sess = open_session(store)
    argv = build_docker_run(sess, extra_env={"PROXY": "http://squid:3128"})
    assert "PROXY=http://squid:3128" in argv


def test_complete_login_session_vaults_profile(tmp_path):
    store, vault = make(tmp_path)
    sess = open_session(store)
    prof = tmp_path / "prof"
    (prof / "Default").mkdir(parents=True)
    (prof / "Default" / "Preferences").write_text("cookies-json")

    def fake_fetch(container_name, dest_name):
        return str(prof)

    rec = complete_login_session(
        store, vault, sess["session_id"], "user:1", fake_fetch,
        consumer_scope=["amazon.com"],
    )
    assert rec["status"] == "stored"
    assert rec["kind"] == "session_profile"
    assert rec["delivery_mode"] == "mount_only"
    assert rec["consumer_scope"] == ["amazon.com"]
    assert store.get(sess["session_id"])["status"] == "done"
    assert store.get(sess["session_id"])["vault_ref"] == rec["vault_ref"]
    # The stored profile round-trips through the mount-only path only.
    seen = vault.get_record("user:1", rec["vault_ref"])
    assert seen["display"] == sess["label"]
    # resolve() keeps refusing mount-only payloads.
    with pytest.raises(VaultError, match="mount-only"):
        vault.resolve("user:1", rec["vault_ref"])
    # materialize_profile can unpack it.
    out = vault.materialize_profile("user:1", rec["vault_ref"], str(tmp_path / "mnt"))
    assert (Path(out) / "Default" / "Preferences").read_text() == "cookies-json"


def test_complete_login_session_requires_open_and_owner(tmp_path):
    store, vault = make(tmp_path)
    sess = open_session(store)
    prof = tmp_path / "prof"
    prof.mkdir(parents=True)
    (prof / "f").write_text("x")

    def fake_fetch(container_name, dest_name):
        return str(prof)

    with pytest.raises(SessionNotFound):
        complete_login_session(
            store, vault, sess["session_id"], "user:2", fake_fetch,
            consumer_scope=["amazon.com"],
        )
    killed = store.kill(sess["session_id"], "user:1")
    assert killed["status"] == "killed"
    with pytest.raises(SessionNotOpen):
        complete_login_session(
            store, vault, sess["session_id"], "user:1", fake_fetch,
            consumer_scope=["amazon.com"],
        )


def test_latest_ready_for_domain_matches_www_alias(tmp_path):
    """A session stored for www.example.com must be found by a lookup of
    example.com (and vice versa): callers always strip the www. prefix."""
    store, _ = make(tmp_path)
    sess = store.create("user:1", "www login", "www.example.com", 6092)

    stripped = store.latest_ready_for_domain("user:1", "example.com")
    assert stripped is not None and stripped["session_id"] == sess["session_id"]

    verbatim = store.latest_ready_for_domain("user:1", "www.example.com")
    assert verbatim is not None and verbatim["session_id"] == sess["session_id"]


def test_pack_profile_dir_stores_browser_tree(tmp_path):
    prof = tmp_path / "prof"
    (prof / "Default" / "Network").mkdir(parents=True)
    (prof / "Default" / "Preferences").write_text("prefs")
    (prof / "Default" / "Network" / "Cookies").write_text("cookies")
    (prof / "Local State").write_text("state")
    blob = pack_profile_dir(prof)
    assert len(blob) > 0
    from io import BytesIO
    import tarfile
    names = []
    with tarfile.open(fileobj=BytesIO(blob), mode="r:gz") as tar:
        names = tar.getnames()
    assert "Default/Preferences" in names
    assert "Default/Network/Cookies" in names


def test_retirable_reclaims_terminal_and_superseded_sessions(tmp_path):
    """Only the newest open/done browser per (tenant, domain) may survive: the
    rest — plus every terminal session — must be reclaimable, otherwise an
    abandoned login leaks a headed Chromium and its published noVNC port."""
    import time

    store, _ = make(tmp_path)
    old = store.create("user:1", "l", "amazon.com", 6090)
    time.sleep(0.01)
    new = store.create("user:1", "l", "amazon.com", 6091)
    other = store.create("user:1", "l", "paypal.com", 6092)
    dead = store.create("user:1", "l", "ebay.com", 6093)
    store.kill(dead["session_id"], "user:1")

    retirable = {s["session_id"] for s in store.retirable()}
    assert old["session_id"] in retirable        # superseded by `new`
    assert dead["session_id"] in retirable       # terminal
    assert new["session_id"] not in retirable    # newest ready for amazon.com
    assert other["session_id"] not in retirable  # sole session for paypal.com


def test_retirable_matches_www_alias_per_domain(tmp_path):
    import time

    store, _ = make(tmp_path)
    a = store.create("user:1", "l", "amazon.com", 6090)
    time.sleep(0.01)
    b = store.create("user:1", "l", "www.amazon.com", 6091)
    retirable = {s["session_id"] for s in store.retirable()}
    # Same (tenant, stripped-domain) key: the older one is dead weight.
    assert a["session_id"] in retirable
    assert b["session_id"] not in retirable


def test_retire_marks_live_session_reaped(tmp_path):
    store, _ = make(tmp_path)
    sess = store.create("user:1", "l", "amazon.com", 6090)
    store.retire(sess["session_id"])
    # 'reaped' (not 'expired') so the reaper never selects it again.
    assert store.get(sess["session_id"])["status"] == "reaped"

    # A second retire is idempotent.
    store.retire(sess["session_id"])
    assert store.get(sess["session_id"])["status"] == "reaped"


def test_retirable_never_reclaims_the_newest_live_session(tmp_path):
    """A live open session is the browser a mission attaches to and the user
    may still be viewing in VNC: the reaper must not age it out, however old."""
    store, _ = make(tmp_path)
    sess = store.create("user:1", "l", "amazon.com", 6090)
    # Simulate a very old row without a newer sibling.
    store._conn().execute(
        "UPDATE concierge_browser_sessions SET created_at = ?, updated_at = ? "
        "WHERE session_id = ?",
        ("2020-01-01T00:00:00.000000Z", "2020-01-01T00:00:00.000000Z",
         sess["session_id"]),
    )
    assert store.retirable() == []
    assert store.get(sess["session_id"])["status"] == "open"


def test_latest_ready_alive_filter_skips_dead_container(tmp_path):
    """A newer row whose container is gone must not shadow an older live one.

    Before this, ``handle_login`` reused the newest *row* even after Docker
    removed its ``--rm`` container, DMing the user a VNC link that 502s (an
    edge "Host Error")."""
    from datetime import timedelta

    store, _ = make(tmp_path)
    live = store.create("user:1", "amazon login", "amazon.com", 7450)
    store.expire_stale(ttl=timedelta(seconds=-1))  # lapsed but still running
    dead = store.create("user:1", "amazon login", "amazon.com", 7451)

    def alive(name):
        return name == live["container_name"]

    got = store.latest_ready_for_domain("user:1", "amazon.com", alive=alive)
    assert got is not None and got["session_id"] == live["session_id"]
    assert got["session_id"] != dead["session_id"]
    # With no liveness filter the pure-SQL lookup still prefers the newest.
    assert store.latest_ready_for_domain("user:1", "amazon.com")["session_id"] == dead["session_id"]


def test_latest_ready_alive_filter_includes_lapsed_session(tmp_path):
    from datetime import timedelta

    store, _ = make(tmp_path)
    sess = store.create("user:1", "amazon login", "amazon.com", 7452)
    store.expire_stale(ttl=timedelta(seconds=-1))
    assert store.get(sess["session_id"])["status"] == "expired"
    # Pure-SQL lookup ignores a lapsed row...
    assert store.latest_ready_for_domain("user:1", "amazon.com") is None
    # ...but with a liveness check a lapsed-but-running browser is still reused.
    got = store.latest_ready_for_domain("user:1", "amazon.com", alive=lambda n: True)
    assert got is not None and got["session_id"] == sess["session_id"]