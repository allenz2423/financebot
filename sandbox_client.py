"""Client for the isolated financebot sandbox."""

import base64
import csv
import hashlib
import io
import json
import os
import posixpath

import discord
import httpx


SANDBOX_INTERNAL_TOKEN = os.getenv(
    "SANDBOX_INTERNAL_TOKEN",
    "",
).strip()


def _sandbox_headers() -> dict[str, str]:
    """Build sandbox auth headers on demand.

    The token check is deferred to call time rather than import time so the
    module can be imported in contexts where the sandbox is not available
    (tests, offline runs, the advisor loop before the sandbox container is
    up).  A missing token raises at the point of use, not at import.
    """
    if not SANDBOX_INTERNAL_TOKEN:
        raise RuntimeError(
            "SANDBOX_INTERNAL_TOKEN must be configured for sandbox API access."
        )
    return {"X-Sandbox-Token": SANDBOX_INTERNAL_TOKEN}

SANDBOX_URL = os.getenv(
    "SANDBOX_URL",
    "http://sandbox:8100/execute",
)

SANDBOX_SHELL_URL = os.getenv(
    "SANDBOX_SHELL_URL",
    "http://sandbox:8100/shell",
)

SANDBOX_INSTALL_URL = os.getenv(
    "SANDBOX_INSTALL_URL",
    "http://sandbox-installer:8100/install",
)

SANDBOX_WORKSPACE_READ_URL = os.getenv(
    "SANDBOX_WORKSPACE_READ_URL",
    "http://sandbox:8100/workspace/read",
)

SANDBOX_WORKSPACE_LIST_URL = os.getenv(
    "SANDBOX_WORKSPACE_LIST_URL",
    "http://sandbox:8100/workspace/list",
)

SANDBOX_WORKSPACE_WRITE_URL = os.getenv(
    "SANDBOX_WORKSPACE_WRITE_URL",
    "http://sandbox:8100/workspace/write",
)

SANDBOX_DEFAULT_TIMEOUT = int(
    os.getenv("SANDBOX_DEFAULT_TIMEOUT", "60")
)

SANDBOX_MAX_TIMEOUT = int(
    os.getenv("SANDBOX_MAX_TIMEOUT", "120")
)


def _get_scrubbed_db_base64(user_id: str) -> str:

    import sqlite3
    import tempfile

    try:
        from src.core.state import DB_PATH
    except ImportError:
        DB_PATH = "data/finances.db"

    with tempfile.NamedTemporaryFile(
        delete=False,
        suffix=".db",
    ) as tmp:
        tmp_path = tmp.name

    try:
        source_conn = sqlite3.connect(DB_PATH)

        dest_conn = sqlite3.connect(
            tmp_path
        )

        with source_conn, dest_conn:
            source_conn.backup(dest_conn)

        source_conn.close()

        dest_conn.execute(
            "PRAGMA journal_mode=DELETE"
        )

        with dest_conn as conn:

            conn.create_function(
                "plaid_sync_active",
                0,
                lambda: 1,
            )

            c = conn.cursor()

            c.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table'"
            )

            tables = [
                r[0]
                for r in c.fetchall()
            ]

            for table in tables:

                if table.startswith("sqlite_"):
                    continue

                c.execute(
                    f"PRAGMA table_info({table})"
                )

                cols = [
                    r[1]
                    for r in c.fetchall()
                ]

                if (
                    "user_id" not in cols
                    and "requester_user_id" not in cols
                ):
                    try:
                        c.execute(
                            f"ALTER TABLE {table} "
                            "ADD COLUMN user_id TEXT"
                        )
                        cols.append("user_id")
                    except Exception:
                        pass

                if "user_id" in cols:

                    c.execute(
                        f"DELETE FROM {table} "
                        "WHERE CAST(user_id AS TEXT) != ? "
                        "OR user_id IS NULL",
                        (str(user_id),),
                    )

                elif "requester_user_id" in cols:

                    c.execute(
                        f"DELETE FROM {table} "
                        "WHERE CAST(requester_user_id AS TEXT) != ? "
                        "OR requester_user_id IS NULL",
                        (str(user_id),),
                    )

            conn.commit()

            conn.execute("VACUUM")

        dest_conn.close()

        with open(tmp_path, "rb") as f:
            return base64.b64encode(
                f.read()
            ).decode("ascii")

    finally:

        try:
            os.remove(tmp_path)
        except OSError:
            pass


def _clamp_timeout(
    timeout: int | None,
) -> int:

    return max(
        1,
        min(
            int(timeout or SANDBOX_DEFAULT_TIMEOUT),
            SANDBOX_MAX_TIMEOUT,
        ),
    )


async def run_python_sandbox(
    code: str,
    reply_msg: "discord.Message | None" = None,
    timeout: int | None = None,
    session_id: str | None = None,
    user_id: str | None = None,
) -> tuple[str, list[bytes]]:

    if user_id is None:
        try:
            from src.core.state import CURRENT_USER_ID
            user_id = CURRENT_USER_ID.get()
        except ImportError:
            pass

    if reply_msg is not None:

        preview = (
            code
            if len(code) < 1500
            else code[:1500]
            + "\n# ...[truncated for display]"
        )

        try:
            await reply_msg.channel.send(
                content=(
                    " **Running sandbox Python...**\n"
                    "```python\n"
                    f"{preview}\n"
                    "```"
                )
            )
        except Exception:
            pass

    db_base64 = None

    if user_id:
        db_base64 = _get_scrubbed_db_base64(
            user_id
        )

    effective_timeout = _clamp_timeout(
        timeout
    )

    try:

        async with httpx.AsyncClient(
            timeout=effective_timeout + 15
        ) as client:

            resp = await client.post(
                SANDBOX_URL,
                headers=_sandbox_headers(),
                json={
                    "code": code,
                    "timeout": effective_timeout,
                    "session_id": session_id,
                    "user_id": user_id,
                    "db_base64": db_base64,
                },
            )

            resp.raise_for_status()

            data = resp.json()

    except Exception as exc:

        return (
            f" Sandbox request failed: {exc}",
            [],
        )

    images = []

    for encoded in data.get(
        "images",
        [],
    ):

        try:
            images.append(
                base64.b64decode(encoded)
            )
        except Exception:
            continue

    summary_parts = []

    if data.get("timed_out"):
        summary_parts.append(
            f" Execution timed out after "
            f"{effective_timeout}s."
        )

    err_str = str(
        data.get("error") or ""
    )

    if err_str:

        summary_parts.append(
            " Error:\n"
            + (
                err_str[-1000:]
                if len(err_str) > 1000
                else err_str
            )
        )

    stdout = (
        data.get("stdout") or ""
    ).strip()

    if stdout:

        summary_parts.append(
            f"Output:\n{stdout}"
        )

    if images:

        summary_parts.append(
            f"({len(images)} chart image(s) "
            "generated and posted to the channel.)"
        )

    if not summary_parts:

        summary_parts.append(
            "Code ran with no output, "
            "no error, and no charts."
        )

    duration = data.get(
        "duration_seconds"
    )

    if duration is not None:

        summary_parts.append(
            f"(Ran in {duration}s)"
        )

    final_summary = "\n\n".join(
        summary_parts
    )

    if reply_msg is not None:

        try:
            await reply_msg.channel.send(
                content=(
                    " **Sandbox Output:**\n"
                    "```text\n"
                    f"{final_summary[:1900]}\n"
                    "```"
                )
            )
        except Exception:
            pass

    return final_summary, images


async def run_shell(
    command: str,
    reply_msg: "discord.Message | None" = None,
    timeout: int | None = None,
    session_id: str | None = None,
    user_id: str | None = None,
) -> str:

    if user_id is None:
        try:
            from src.core.state import CURRENT_USER_ID
            user_id = CURRENT_USER_ID.get()
        except ImportError:
            pass

    if reply_msg is not None:

        preview = (
            command
            if len(command) < 1500
            else command[:1500]
            + "\n# ...[truncated for display]"
        )

        try:
            await reply_msg.channel.send(
                content=(
                    " **Running sandbox shell...**\n"
                    "```bash\n"
                    f"{preview}\n"
                    "```"
                )
            )
        except Exception:
            pass

    db_base64 = None

    if user_id:
        db_base64 = _get_scrubbed_db_base64(
            user_id
        )

    effective_timeout = _clamp_timeout(
        timeout
    )

    try:

        async with httpx.AsyncClient(
            timeout=effective_timeout + 15
        ) as client:

            resp = await client.post(
                SANDBOX_SHELL_URL,
                headers=_sandbox_headers(),
                json={
                    "command": command,
                    "timeout": effective_timeout,
                    "session_id": session_id,
                    "user_id": user_id,
                    "db_base64": db_base64,
                },
            )

            resp.raise_for_status()

            data = resp.json()

    except Exception as exc:

        return (
            f" Sandbox shell request failed: {exc}"
        )

    parts = []

    if data.get("timed_out"):

        parts.append(
            f" Command timed out after "
            f"{effective_timeout}s."
        )

    err_str = str(
        data.get("error") or ""
    )

    if err_str:

        parts.append(
            " Error:\n"
            + (
                err_str[-1000:]
                if len(err_str) > 1000
                else err_str
            )
        )

    stdout = (
        data.get("stdout") or ""
    ).strip()

    if stdout:

        parts.append(
            f"Output:\n{stdout}"
        )

    if not parts:

        parts.append(
            "Command completed with no output."
        )

    duration = data.get(
        "duration_seconds"
    )

    if duration is not None:

        parts.append(
            f"(Ran in {duration}s)"
        )

    result = "\n\n".join(parts)

    if reply_msg is not None:

        try:
            await reply_msg.channel.send(
                content=(
                    " **Shell Output:**\n"
                    "```text\n"
                    f"{result[:1900]}\n"
                    "```"
                )
            )
        except Exception:
            pass

    return result


async def install_python_package(
    package: str,
    reply_msg: "discord.Message | None" = None,
    timeout: int | None = None,
) -> str:

    if reply_msg is not None:

        try:
            await reply_msg.channel.send(
                content=(
                    f" **Installing package:** "
                    f"`{package}`..."
                )
            )
        except Exception:
            pass

    effective_timeout = min(
        max(
            int(timeout or 60),
            1,
        ),
        180,
    )

    try:

        async with httpx.AsyncClient(
            timeout=effective_timeout + 15
        ) as client:

            resp = await client.post(
                SANDBOX_INSTALL_URL,
                headers=_sandbox_headers(),
                json={
                    "package": package,
                    "timeout": effective_timeout,
                },
            )

            resp.raise_for_status()

            data = resp.json()

    except Exception as exc:

        return (
            f" Install request failed: {exc}"
        )

    status = (
        " Installed"
        if data.get("success")
        else " Install failed"
    )

    output = (
        data.get("stdout") or ""
    ).strip()

    return (
        f"{status}: `{package}`\n"
        "```\n"
        f"{output[-1500:]}\n"
        "```"
    )


async def list_workspace_files(
    user_id: str,
) -> list[dict]:
    """List files in the current user's persistent workspace."""

    try:
        async with httpx.AsyncClient(
            timeout=30,
        ) as client:

            resp = await client.get(
                SANDBOX_WORKSPACE_LIST_URL,
                headers=_sandbox_headers(),
                params={
                    "user_id": str(user_id),
                },
            )

            resp.raise_for_status()

            data = resp.json()

            return data.get(
                "files",
                [],
            )

    except Exception:
        return []


async def save_workspace_file(
    user_id: str,
    filename: str,
    content: bytes,
) -> str | None:
    """Save an uploaded attachment into the current user's workspace."""
    if not content or len(content) > 100 * 1024 * 1024:
        return None
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            resp = await client.post(
                SANDBOX_WORKSPACE_WRITE_URL,
                headers=_sandbox_headers(),
                json={
                    "user_id": str(user_id),
                    "filename": str(filename or "attachment.bin"),
                    "content_base64": base64.b64encode(content).decode("ascii"),
                },
            )
            resp.raise_for_status()
            return str(resp.json().get("path") or "") or None
    except Exception as exc:
        print(f" [WORKSPACE UPLOAD] failed: {type(exc).__name__}: {exc}")
        return None


async def read_workspace_file(
    user_id: str,
    path: str,
) -> tuple[str, bytes] | None:
    """Read one file from the current user's persistent workspace."""

    result = await _read_workspace_file_details(user_id, path)
    if result is None:
        return None
    filename, content, _canonical_path = result
    return filename, content


async def _read_workspace_file_details(
    user_id: str,
    path: str,
) -> tuple[str, bytes, str] | None:
    """Read a workspace file while retaining its server-reported identity."""

    try:

        async with httpx.AsyncClient(
            timeout=45,
        ) as client:

            resp = await client.post(
                SANDBOX_WORKSPACE_READ_URL,
                headers=_sandbox_headers(),
                json={
                    "user_id": str(user_id),
                    "path": path,
                },
            )

            resp.raise_for_status()

            data = resp.json()

    except Exception:
        return None

    try:

        content = base64.b64decode(
            data["content_base64"]
        )

        filename = str(
            data["filename"]
        )

        response_path = data.get("path") or data.get("relative_path")
        if not isinstance(response_path, str) or not response_path.strip():
            return None
        canonical_path = _canonical_owner_relative_path(response_path, user_id)
        if not canonical_path:
            return None
        return filename, content, canonical_path

    except Exception:
        return None


def _canonical_owner_relative_path(path: object, user_id: str) -> str:
    """Normalize a sandbox path into a bounded owner-relative path."""
    value = str(path or "").replace("\\", "/").strip()
    owner_prefix = f"workspace/{str(user_id).strip('/')}/"
    if value.startswith("/"):
        value = value.lstrip("/")
    if value.startswith(owner_prefix):
        value = value[len(owner_prefix):]
    elif value.startswith("workspace/"):
        # A workspace-prefixed response for another account is inconsistent,
        # not a valid relative path in this user's workspace.
        return ""
    normalized = posixpath.normpath(value)
    if normalized in {"", "."} or normalized == ".." or normalized.startswith("../"):
        return ""
    return normalized[:512]


def _reject_nonstandard_json_constant(value: str) -> None:
    """Python's JSON decoder accepts NaN/Infinity unless explicitly rejected."""
    raise ValueError(f"nonstandard JSON constant: {value}")


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate JSON keys instead of silently keeping the last value."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _validate_csv_bytes(content: bytes) -> bool:
    """Check bounded UTF-8 CSV structure and reject JSON documents named .csv."""
    if not content or len(content) > 2_000_000:
        return False
    try:
        text = content.decode("utf-8-sig")
        if not text.strip():
            return False
        try:
            parsed = json.loads(
                content,
                object_pairs_hook=_unique_json_object,
                parse_constant=_reject_nonstandard_json_constant,
            )
        except (UnicodeDecodeError, ValueError, TypeError, RecursionError):
            parsed = None
        else:
            # A JSON scalar (e.g. 123 or "text") is syntactically valid CSV,
            # but cannot prove a requested tabular export format.
            return False
        if text.lstrip().startswith(("{", "[")):
            # Also reject JSON-looking objects that failed strict parsing,
            # including duplicate-key documents.
            return False
        width = None
        row_count = 0
        for row in csv.reader(io.StringIO(text, newline=""), strict=True):
            if not row:
                continue
            if width is None:
                width = len(row)
            elif len(row) != width:
                return False
            row_count += 1
            if row_count > 50_000:
                return False
        return width is not None and width > 0 and row_count > 0
    except (UnicodeDecodeError, csv.Error, ValueError, RecursionError):
        return False


async def send_workspace_file(
    channel,
    user_id: str,
    path: str,
    title: str | None = None,
    return_evidence: bool = False,
) -> bool | dict[str, object]:
    """Retrieve a user's persistent workspace file and attach it to Discord."""

    if not channel:
        return False

    result = await _read_workspace_file_details(
        user_id,
        path,
    )

    if result is None:
        return False

    filename, content, canonical_path = result

    try:
        file = discord.File(
            io.BytesIO(content),
            filename=filename,
        )
    except Exception:
        return False

    message = title or f" **Workspace file:** `{filename}`"
    try:
        sent_message = await channel.send(
            content=message,
            file=file,
        )
    except Exception:
        # Discord may have accepted the upload before the client lost its
        # acknowledgement. Preserve ambiguity for the receipt lifecycle; do
        # not turn it into a known failure that could be retried.
        return {"status": "unknown"} if return_evidence else False

    if return_evidence:
        message_id = getattr(sent_message, "id", None)
        if not canonical_path or message_id is None:
            return {"status": "unknown"}
        evidence = {
            "path": canonical_path,
            "filename": str(filename)[:255],
            "sha256": hashlib.sha256(content).hexdigest(),
            "byte_size": len(content),
            "message_id": str(message_id)[:64],
        }
        if str(filename).casefold().endswith(".json"):
            try:
                if len(content) > 2_000_000:
                    evidence["valid_json"] = False
                else:
                    json.loads(
                        content,
                        object_pairs_hook=_unique_json_object,
                        parse_constant=_reject_nonstandard_json_constant,
                    )
                    evidence["valid_json"] = True
            except (UnicodeDecodeError, ValueError, TypeError, RecursionError):
                evidence["valid_json"] = False
        elif str(filename).casefold().endswith(".csv"):
            evidence["valid_csv"] = _validate_csv_bytes(content)
        return evidence
    return True


async def post_sandbox_images(
    channel,
    images: list[bytes],
    title: str = "Simulation result",
):

    if not channel or not images:
        return

    files = [
        discord.File(
            io.BytesIO(img),
            filename=f"chart_{i + 1}.png",
        )
        for i, img in enumerate(images)
    ]

    await channel.send(
        content=f" {title}",
        files=files,
    )
