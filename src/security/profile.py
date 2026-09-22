"""Session-profile archive helpers (B1).

Browser profiles (cookies, localStorage, permissions) are packed to a
single byte blob at capture, for vault storage, and unpacked to a tmpfs dir at
materialization/replay.  Extraction is path-traversal-safe: any entry that
would escape the destination -- or a link kind -- is rejected before extract.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path
from typing import Union


class ProfileError(ValueError):
    pass


def pack_profile_dir(directory: Union[str, Path]) -> bytes:
    """tar.gz a browser profile directory to vault-ready bytes."""
    src = Path(directory)
    if not src.is_dir():
        raise ProfileError("profile directory not found")
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for entry in sorted(src.rglob("*")):
            if not entry.is_file():
                continue  # dirs are recreated on extract
            tar.add(entry, arcname=entry.relative_to(src).as_posix())
    return out.getvalue()


def unpack_profile(blob: bytes, dest: Union[str, Path]) -> Path:
    """Extract a packed profile to ``dest`` (new/empty dir allowed)."""
    target = Path(dest)
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        for member in tar.getmembers():
            if member.islnk() or member.issym() or not (member.isfile() or member.isdir()):
                raise ProfileError("profile archive entries are restricted to files/dirs")
            name = member.name
            if not name or name.startswith("/") or ".." in Path(name).parts:
                raise ProfileError("profile archive contains an unsafe path")
            dst = (target / name).resolve()
            if not dst.is_relative_to(target.resolve()):
                raise ProfileError("profile archive entry escapes the destination")
        for member in tar.getmembers():
            tar.extract(member, target, filter="data")
    return target


__all__ = ["pack_profile_dir", "unpack_profile", "ProfileError"]