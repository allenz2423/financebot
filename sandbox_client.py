"""Client for the isolated financebot sandbox."""

import base64
import io
import os

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


async def read_workspace_file(
    user_id: str,
    path: str,
) -> tuple[str, bytes] | None:
    """Read one file from the current user's persistent workspace."""

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

        return filename, content

    except Exception:
        return None


async def send_workspace_file(
    channel,
    user_id: str,
    path: str,
    title: str | None = None,
) -> bool:
    """Retrieve a user's persistent workspace file and attach it to Discord."""

    if not channel:
        return False

    result = await read_workspace_file(
        user_id,
        path,
    )

    if result is None:
        return False

    filename, content = result

    try:

        file = discord.File(
            io.BytesIO(content),
            filename=filename,
        )

        message = (
            title
            or f" **Workspace file:** `{filename}`"
        )

        await channel.send(
            content=message,
            file=file,
        )

        return True

    except Exception:
        return False


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
