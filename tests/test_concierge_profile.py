"""B1 session-profile archive + mount-only materialization tests."""

import io
import tarfile
from pathlib import Path

import pytest

from src.security.profile import ProfileError, pack_profile_dir, unpack_profile
from src.security.vault import Vault, VaultError, VaultRevokedError


KEY = "b0-profile-test-master-key"


def _write_tree(base, files):
    for rel, data in files.items():
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(data)


def _make_profile_blob(base)->bytes:
    _write_tree(base, {
        "Default/Preferences": "cookies-json",
        "Default/Network/Cookies": "cookie-bytes",
        "Local State": "state",
    })
    return pack_profile_dir(base)


def _tar_with(members)->bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for info, data in members:
            ti = tarfile.TarInfo(info)
            ti.size = len(data)
            tar.addfile(ti, io.BytesIO(data.encode()))
    return buf.getvalue()


def test_pack_unpack_roundtrip(tmp_path):
    src = tmp_path / "prof"
    blob = _make_profile_blob(src)
    out = tmp_path / "out"
    unpack_profile(blob, out)
    assert (out / "Default" / "Preferences").read_text() == "cookies-json"
    assert (out / "Default" / "Network" / "Cookies").read_text() == "cookie-bytes"
    assert (out / "Local State").read_text() == "state"


def test_pack_requires_directory(tmp_path):
    with pytest.raises(ProfileError, match="not found"):
        pack_profile_dir(tmp_path / "missing")


def test_traversal_escape_rejected(tmp_path):
    blob = _tar_with({("../evil.txt", "boom"),})
    with pytest.raises(ProfileError, match="unsafe"):
        unpack_profile(blob, tmp_path / "out")


def test_absolute_path_rejected(tmp_path):
    blob = _tar_with({("/etc/passwd", "root"),})
    with pytest.raises(ProfileError, match="unsafe"):
        unpack_profile(blob, tmp_path / "out")


def test_symlink_member_rejected(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        ti = tarfile.TarInfo("link")
        ti.type = tarfile.SYMTYPE
        ti.linkname = "etc/passwd"
        tar.addfile(ti)
    blob = buf.getvalue()
    with pytest.raises(ProfileError, match="restricted"):
        unpack_profile(blob, tmp_path / "out")


def test_materialize_profile_roundtrip(tmp_path):
    db = tmp_path / "s.db"
    vault = Vault(str(db), master_key=KEY)
    src = tmp_path / "prof"
    blob = _make_profile_blob(src)
    rec = vault.store_session_profile(
        "user:1", "Amazon", blob, consumer_scope=["amazon.com"],
    )
    dest = tmp_path / "mnt"
    out = vault.materialize_profile("user:1", rec["vault_ref"], str(dest))
    assert (Path(out) / "Default" / "Preferences").read_text() == "cookies-json"


def test_materialize_requires_session_profile(tmp_path):
    db = tmp_path / "s.db"
    vault = Vault(str(db), master_key=KEY)
    rec = vault.store_fields(
        "user:1", "payment", "Amex",
        fields={"card_pan|Card": "378282246310005"},
        consumer_scope=["amex.com"],
    )
    with pytest.raises(VaultError, match="only session profiles"):
        vault.materialize_profile("user:1", rec["vault_ref"], str(tmp_path / "mnt"))


def test_resolve_still_refuses_mount_only(tmp_path):
    db = tmp_path / "s.db"
    vault = Vault(str(db), master_key=KEY)
    blob = _make_profile_blob(tmp_path / "prof")
    rec = vault.store_session_profile("user:1", "Amazon", blob, ["amazon.com"])
    with pytest.raises(VaultError, match="mount-only"):
        vault.resolve("user:1", rec["vault_ref"])


def test_materialize_tenant_and_revoked_guards(tmp_path):
    db = tmp_path / "s.db"
    vault = Vault(str(db), master_key=KEY)
    blob = _make_profile_blob(tmp_path / "prof")
    rec = vault.store_session_profile("user:1", "Amazon", blob, ["amazon.com"])
    with pytest.raises(VaultError):
        vault.materialize_profile("user:2", rec["vault_ref"], str(tmp_path / "mnt"))
    vault.revoke("user:1", rec["vault_ref"])
    with pytest.raises(VaultRevokedError):
        vault.materialize_profile("user:1", rec["vault_ref"], str(tmp_path / "mnt"))