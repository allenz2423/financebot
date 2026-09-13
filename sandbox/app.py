"""Financebot isolated execution sandbox.

Execution model:
- Python and bash run as the unprivileged `sandbox` user.
- Each execution gets a disposable project directory containing a scrubbed
  copy of finances.db.
- /workspace is a persistent writable volume shared by all executions.
- The production host filesystem is never writable from this container.
- Files under /workspace may be freely created, modified, renamed, and deleted.
"""

import asyncio
import ctypes
import hmac
import base64
import json
import os
import re
import resource
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Financebot Sandbox")

DEFAULT_TIMEOUT = int(os.getenv("SANDBOX_DEFAULT_TIMEOUT", "60"))
MAX_TIMEOUT = int(os.getenv("SANDBOX_MAX_TIMEOUT", "120"))
MEMORY_LIMIT_MB = int(os.getenv("SANDBOX_MEMORY_LIMIT_MB", "512"))
MAX_OUTPUT_CHARS = int(os.getenv("SANDBOX_MAX_OUTPUT_CHARS", "1000000000000000"))

OUTPUT_MARKER = "###SANDBOX_RESULT###"

PACKAGES_DIR = os.getenv("SANDBOX_PACKAGES_DIR", "/opt/packages")
HOST_PROJECT_DIR = os.getenv("SANDBOX_HOST_PROJECT_DIR", "/host/project")

WORKSPACE_DIR = Path("/workspace")
SESSION_ROOT = Path("/tmp/sandbox_sessions")


# Landlock filesystem sandboxing.
#
# The sandbox container itself is shared by all users, so /workspace cannot be
# exposed wholesale to arbitrary Python/bash processes. Each child process gets
# a Landlock allowlist containing only its own persistent workspace plus the
# runtime paths it legitimately needs.
#
# Landlock is intentionally applied in the child via preexec_fn, before exec().
LANDLOCK_CREATE_RULESET = 444
LANDLOCK_ADD_RULE = 445
LANDLOCK_RESTRICT_SELF = 446

LANDLOCK_RULE_TYPE_PATH_BENEATH = 1

LANDLOCK_ACCESS_FS_EXECUTE = 1 << 0
LANDLOCK_ACCESS_FS_WRITE_FILE = 1 << 1
LANDLOCK_ACCESS_FS_READ_FILE = 1 << 2
LANDLOCK_ACCESS_FS_READ_DIR = 1 << 3
LANDLOCK_ACCESS_FS_REMOVE_DIR = 1 << 4
LANDLOCK_ACCESS_FS_REMOVE_FILE = 1 << 5
LANDLOCK_ACCESS_FS_MAKE_CHAR = 1 << 6
LANDLOCK_ACCESS_FS_MAKE_DIR = 1 << 7
LANDLOCK_ACCESS_FS_MAKE_REG = 1 << 8
LANDLOCK_ACCESS_FS_MAKE_SOCK = 1 << 9
LANDLOCK_ACCESS_FS_MAKE_FIFO = 1 << 10
LANDLOCK_ACCESS_FS_MAKE_BLOCK = 1 << 11
LANDLOCK_ACCESS_FS_MAKE_SYM = 1 << 12
LANDLOCK_ACCESS_FS_REFER = 1 << 13
LANDLOCK_ACCESS_FS_TRUNCATE = 1 << 14

LANDLOCK_READ_ONLY = (
    LANDLOCK_ACCESS_FS_EXECUTE
    | LANDLOCK_ACCESS_FS_READ_FILE
    | LANDLOCK_ACCESS_FS_READ_DIR
)

LANDLOCK_WRITABLE = (
    LANDLOCK_READ_ONLY
    | LANDLOCK_ACCESS_FS_WRITE_FILE
    | LANDLOCK_ACCESS_FS_REMOVE_DIR
    | LANDLOCK_ACCESS_FS_REMOVE_FILE
    | LANDLOCK_ACCESS_FS_MAKE_DIR
    | LANDLOCK_ACCESS_FS_MAKE_REG
    | LANDLOCK_ACCESS_FS_MAKE_SOCK
    | LANDLOCK_ACCESS_FS_MAKE_FIFO
    | LANDLOCK_ACCESS_FS_MAKE_SYM
    | LANDLOCK_ACCESS_FS_REFER
    | LANDLOCK_ACCESS_FS_TRUNCATE
)
LANDLOCK_DEV_ACCESS = (
    LANDLOCK_ACCESS_FS_READ_FILE
    | LANDLOCK_ACCESS_FS_WRITE_FILE
    | LANDLOCK_ACCESS_FS_READ_DIR
)

class _LandlockRulesetAttr(ctypes.Structure):
    # ABI v1 filesystem-only ruleset.
    _fields_ = [
        ("handled_access_fs", ctypes.c_uint64),
    ]


class _LandlockPathBeneathAttr(ctypes.Structure):
    # Kernel layout:
    #   __u64 allowed_access;
    #   __s32 parent_fd;
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
    ]


def _landlock_syscall(number: int, *args) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    return int(libc.syscall(number, *args))


def _landlock_add_path_rule(
    ruleset_fd: int,
    path: Path,
    allowed_access: int,
) -> None:
    """Add a Landlock path-beneath rule for an existing directory."""
    path = path.resolve()

    if not path.exists():
        return

    fd = os.open(
        str(path),
        os.O_PATH | os.O_CLOEXEC,
    )

    try:
        rule = _LandlockPathBeneathAttr(
            allowed_access=ctypes.c_uint64(allowed_access),
            parent_fd=ctypes.c_int32(fd),
        )

        rc = _landlock_syscall(
            LANDLOCK_ADD_RULE,
            ruleset_fd,
            LANDLOCK_RULE_TYPE_PATH_BENEATH,
            ctypes.byref(rule),
            0,
        )

        if rc < 0:
            errno = ctypes.get_errno()
            raise OSError(
                errno,
                f"landlock_add_rule failed for {path}",
            )
    finally:
        os.close(fd)


def _landlock_restrict_filesystem(
    workspace: Path,
    cwd: str,
) -> None:
    """Restrict a child process to its own filesystem view.

    This intentionally fails closed. If Landlock cannot be applied, the
    child does not execute.

    The policy permits:
      * read/execute access to the container runtime
      * read access to installed sandbox packages
      * full access to this execution's private temporary tree
      * full access to this user's persistent workspace

    It deliberately does NOT grant access to:
      * other users' workspaces
      * the global /tmp contents
      * /proc
      * /sys
      * /root
      * /home
      * /host
    """

    handled = (
        LANDLOCK_ACCESS_FS_EXECUTE
        | LANDLOCK_ACCESS_FS_WRITE_FILE
        | LANDLOCK_ACCESS_FS_READ_FILE
        | LANDLOCK_ACCESS_FS_READ_DIR
        | LANDLOCK_ACCESS_FS_REMOVE_DIR
        | LANDLOCK_ACCESS_FS_REMOVE_FILE
        | LANDLOCK_ACCESS_FS_MAKE_CHAR
        | LANDLOCK_ACCESS_FS_MAKE_DIR
        | LANDLOCK_ACCESS_FS_MAKE_REG
        | LANDLOCK_ACCESS_FS_MAKE_SOCK
        | LANDLOCK_ACCESS_FS_MAKE_FIFO
        | LANDLOCK_ACCESS_FS_MAKE_BLOCK
        | LANDLOCK_ACCESS_FS_MAKE_SYM
        | LANDLOCK_ACCESS_FS_REFER
        | LANDLOCK_ACCESS_FS_TRUNCATE
    )

    attr = _LandlockRulesetAttr(
        handled_access_fs=ctypes.c_uint64(handled),
    )

    ruleset_fd = _landlock_syscall(
        LANDLOCK_CREATE_RULESET,
        ctypes.byref(attr),
        ctypes.sizeof(attr),
        0,
    )

    if ruleset_fd < 0:
        raise OSError(
            ctypes.get_errno(),
            "landlock_create_ruleset failed",
        )

    try:
        cwd_path = Path(cwd).resolve()

        # _prepare_workspace() creates:
        #
        #   /tmp/sandbox_sessions/<uid>/project
        #   /tmp/sandbox_sessions/<uid>/out
        #
        # Shell executions may use project as cwd, while Python output can
        # live in the sibling "out" directory. Therefore the actual
        # execution root is the parent of "project".
        execution_root = cwd_path.parent if cwd_path.name == "project" else cwd_path
        execution_root = execution_root.resolve()

        session_root = Path("/tmp/sandbox_sessions").resolve()

        try:
            execution_root.relative_to(session_root)
        except ValueError:
            raise OSError(
                13,
                f"Execution directory escaped sandbox session root: {execution_root}",
            )

        workspace = workspace.resolve()
        workspace_root = WORKSPACE_DIR.resolve()

        try:
            workspace.relative_to(workspace_root)
        except ValueError:
            raise OSError(
                13,
                f"Workspace escaped workspace root: {workspace}",
            )

        if workspace == workspace_root:
            raise OSError(
                13,
                "Refusing to sandbox against global /workspace",
            )

        # System runtime: read/execute only.
        #
        # /proc and /sys are intentionally excluded. They can expose
        # information about other processes or the host/container.
        readonly_paths = (
            "/bin",
            "/sbin",
            "/usr",
            "/lib",
            "/lib64",
            "/etc",
            "/dev",
            "/var",
            "/run",
            PACKAGES_DIR,
        )

        for raw_path in readonly_paths:
            _landlock_add_path_rule(
                ruleset_fd,
                Path(raw_path),
                LANDLOCK_READ_ONLY,
            )

        # Allow reading and writing to devices like /dev/null, /dev/zero, /dev/urandom
        _landlock_add_path_rule(
            ruleset_fd,
            Path("/dev"),
            LANDLOCK_DEV_ACCESS,
        )
        # /tmp itself is traversal-only. This means:
        #
        #   cat /tmp/other-user-secret
        #
        # is denied, while a specifically allowed execution subtree remains
        # accessible.
        _landlock_add_path_rule(
            ruleset_fd,
            Path("/tmp"),
            LANDLOCK_ACCESS_FS_EXECUTE,
        )

        # Same for the shared session parent. It cannot be enumerated/read.
        _landlock_add_path_rule(
            ruleset_fd,
            session_root,
            LANDLOCK_ACCESS_FS_EXECUTE,
        )

        # /workspace is traversal-only. The user's own directory below it
        # receives the actual writable rule.
        _landlock_add_path_rule(
            ruleset_fd,
            workspace_root,
            LANDLOCK_ACCESS_FS_EXECUTE,
        )

        # Private disposable execution tree.
        _landlock_add_path_rule(
            ruleset_fd,
            execution_root,
            LANDLOCK_WRITABLE,
        )

        # Persistent tenant workspace.
        _landlock_add_path_rule(
            ruleset_fd,
            workspace,
            LANDLOCK_WRITABLE,
        )

        rc = _landlock_syscall(
            LANDLOCK_RESTRICT_SELF,
            ruleset_fd,
            0,
        )

        if rc < 0:
            raise OSError(
                ctypes.get_errno(),
                "landlock_restrict_self failed",
            )

    finally:
        os.close(ruleset_fd)


def _user_workspace(user_id: str | None) -> Path:
    """Return the persistent workspace belonging exclusively to one user."""

    uid = str(user_id or "").strip()

    if not uid:
        raise HTTPException(
            status_code=400,
            detail="user_id is required for workspace access.",
        )

    # Discord user IDs are numeric. This prevents the tenant identifier
    # itself from becoming a path traversal primitive.
    if not uid.isdigit():
        raise HTTPException(
            status_code=400,
            detail="Invalid user_id.",
        )

    root = WORKSPACE_DIR.resolve()
    user_root = (root / uid).resolve()

    try:
        user_root.relative_to(root)
    except ValueError:
        raise HTTPException(
            status_code=403,
            detail="Invalid workspace boundary.",
        )

    user_root.mkdir(
        parents=True,
        exist_ok=True,
    )

    return user_root

INSTALL_TIMEOUT_DEFAULT = int(os.getenv("SANDBOX_INSTALL_TIMEOUT", "60"))
INSTALL_TIMEOUT_MAX = int(os.getenv("SANDBOX_INSTALL_TIMEOUT_MAX", "180"))


class ExecuteRequest(BaseModel):
    code: str
    timeout: Optional[int] = None
    session_id: Optional[str] = None
    user_id: Optional[str] = None
    db_base64: Optional[str] = None


class ShellRequest(BaseModel):
    command: str
    timeout: Optional[int] = None
    session_id: Optional[str] = None
    user_id: Optional[str] = None
    db_base64: Optional[str] = None


class ExecuteResponse(BaseModel):
    stdout: str
    error: Optional[str]
    images: list[str]
    timed_out: bool
    duration_seconds: float


class ShellResponse(BaseModel):
    stdout: str
    error: Optional[str]
    timed_out: bool
    duration_seconds: float
    workspace: str


class InstallRequest(BaseModel):
    package: str
    timeout: Optional[int] = None


class InstallResponse(BaseModel):
    success: bool
    stdout: str
    duration_seconds: float


class WorkspaceFileRequest(BaseModel):
    user_id: str
    path: str


class WorkspaceFileResponse(BaseModel):
    path: str
    filename: str
    size: int
    content_base64: str


def _limit_resources(timeout: int):
    cpu_limit = min(max(timeout, 1), MAX_TIMEOUT)

    resource.setrlimit(
        resource.RLIMIT_CPU,
        (cpu_limit, cpu_limit),
    )

    resource.setrlimit(
        resource.RLIMIT_CORE,
        (0, 0),
    )

    resource.setrlimit(
        resource.RLIMIT_NPROC,
        (512, 512),
    )

    memory = MEMORY_LIMIT_MB * 1024 * 1024

    resource.setrlimit(
        resource.RLIMIT_AS,
        (memory, memory),
    )


def _safe_session_id(session_id: str) -> str:
    """Prevent session IDs from escaping /tmp/sandbox_sessions."""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", str(session_id))

    if not cleaned:
        cleaned = "default"

    return cleaned[:128]


def _prepare_workspace(
    run_id: str,
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    db_base64: Optional[str] = None,
) -> str:
    """Create an ephemeral execution directory.

    The persistent /workspace volume is deliberately NOT copied here.
    Executions access it directly.
    """

    if session_id:
        safe_id = _safe_session_id(session_id)

        workdir = SESSION_ROOT / safe_id

        workdir.mkdir(
            parents=True,
            exist_ok=True,
        )
    else:
        SESSION_ROOT.mkdir(parents=True, exist_ok=True)
        workdir = Path(
            tempfile.mkdtemp(
                prefix=f"run_{run_id}_",
                dir=str(SESSION_ROOT),
            )
        )

    project_dir = workdir / "project"
    output_dir = workdir / "out"

    project_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    source = Path(HOST_PROJECT_DIR)

    if source.exists():
        shutil.copytree(
            source,
            project_dir,
            dirs_exist_ok=True,
        )

    target_db = project_dir / "finances.db"

    if db_base64:
        target_db.write_bytes(
            base64.b64decode(db_base64)
        )

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    return str(workdir)


def _wrapper_python(
    user_code_path: str,
    output_dir: str,
    chdir_target: str,
) -> str:
    """Generate the Python wrapper used to execute user code."""

    return f'''\
import base64
import json
import os
import sys
import traceback

sys.path.append({PACKAGES_DIR!r})

os.chdir({chdir_target!r})

# Persistent Delilah workspace.
WORKSPACE = os.environ.get(
    "FINANCEBOT_WORKSPACE",
    {str(WORKSPACE_DIR)!r},
)
os.environ["FINANCEBOT_WORKSPACE"] = WORKSPACE

import matplotlib
matplotlib.use("Agg")

import matplotlib.pyplot as plt

_saved = []


def _save_current_figure(*args, **kwargs):
    fig = plt.gcf()

    if fig.get_axes():
        path = os.path.join(
            {output_dir!r},
            f"fig_{{len(_saved)}}.png",
        )

        fig.savefig(
            path,
            dpi=110,
            bbox_inches="tight",
        )

        _saved.append(path)

    plt.close(fig)


plt.show = _save_current_figure

error = None

try:
    with open(
        {user_code_path!r},
        encoding="utf-8",
    ) as f:
        source = f.read()

    exec(
        compile(
            source,
            "<sandbox-python>",
            "exec",
        ),
        {{
            "__name__": "__main__",
            "__file__": {user_code_path!r},
            "plt": plt,
            "WORKSPACE": WORKSPACE,
        }},
    )

except Exception:
    error = traceback.format_exc()


for num in plt.get_fignums():
    fig = plt.figure(num)

    path = os.path.join(
        {output_dir!r},
        f"fig_leftover_{{num}}.png",
    )

    fig.savefig(
        path,
        dpi=110,
        bbox_inches="tight",
    )

    _saved.append(path)


images = []

for path in _saved:
    try:
        with open(path, "rb") as f:
            images.append(
                base64.b64encode(
                    f.read()
                ).decode("ascii")
            )
    except OSError:
        pass


print({OUTPUT_MARKER!r})
print(
    json.dumps(
        {{
            "error": error,
            "images": images,
        }}
    )
)
'''


async def _run_process(
    argv: list[str],
    cwd: str,
    timeout: int,
    workspace: Path | None = None,
) -> tuple[str, bool, float]:

    start = time.monotonic()

    try:
        # Each execution gets its own private temporary tree. Shell runs
        # from <workdir>/project while Python runs from <workdir>, so derive
        # the common execution root without ever falling back to global /tmp.
        execution_root = Path(cwd).resolve()
        if execution_root.name == "project":
            execution_root = execution_root.parent

        session_root = SESSION_ROOT.resolve()
        try:
            execution_root.relative_to(session_root)
        except ValueError:
            raise RuntimeError(
                f"Execution root escaped sandbox session root: {execution_root}"
            )

        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd,
            preexec_fn=lambda: (
                _limit_resources(timeout),
                _landlock_restrict_filesystem(
                    workspace or WORKSPACE_DIR,
                    cwd,
                ),
            ),
            env={
                "PATH": os.getenv(
                    "PATH",
                    "/usr/local/bin:/usr/bin:/bin",
                ),
                "HOME": str(execution_root),
                "TMPDIR": str(execution_root),
                "PYTHONUNBUFFERED": "1",
                "OPENBLAS_NUM_THREADS": "1",
                "MKL_NUM_THREADS": "1",
                "OMP_NUM_THREADS": "1",
                "NUMEXPR_NUM_THREADS": "1",
                "VECLIB_MAXIMUM_THREADS": "1",
                "PYTHONPATH": os.pathsep.join(
                    p
                    for p in [
                        PACKAGES_DIR,
                        os.getenv("PYTHONPATH", ""),
                    ]
                    if p
                ),
                "FINANCEBOT_SANDBOX": "1",
                "FINANCEBOT_WORKSPACE": str(
                    workspace or WORKSPACE_DIR
                ),
            },
        )

        try:
            raw, _ = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )

            timed_out = False

        except asyncio.TimeoutError:
            timed_out = True

            proc.kill()

            await proc.wait()

            raw = b""

    except Exception as exc:
        return (
            f"{type(exc).__name__}: {exc}",
            False,
            round(time.monotonic() - start, 2),
        )

    return (
        raw.decode(errors="replace"),
        timed_out,
        round(time.monotonic() - start, 2),
    )


SANDBOX_INTERNAL_TOKEN = os.getenv("SANDBOX_INTERNAL_TOKEN", "").strip()

if not SANDBOX_INTERNAL_TOKEN:
    raise RuntimeError(
        "SANDBOX_INTERNAL_TOKEN must be configured for sandbox API authentication."
    )


@app.middleware("http")
async def _authenticate_internal_api(request, call_next):
    if request.url.path == "/health":
        return await call_next(request)

    supplied = request.headers.get("X-Sandbox-Token", "")
    if not supplied or not hmac.compare_digest(
        supplied,
        SANDBOX_INTERNAL_TOKEN,
    ):
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=401,
            content={"detail": "Unauthorized"},
        )

    return await call_next(request)


@app.post(
    "/execute",
    response_model=ExecuteResponse,
)
async def execute(req: ExecuteRequest) -> ExecuteResponse:

    timeout = min(
        max(req.timeout or DEFAULT_TIMEOUT, 1),
        MAX_TIMEOUT,
    )

    run_id = uuid.uuid4().hex

    workdir = _prepare_workspace(
        run_id,
        req.session_id,
        req.user_id,
        req.db_base64,
    )

    persistent_workspace = _user_workspace(req.user_id)

    project_dir = os.path.join(
        workdir,
        "project",
    )

    output_dir = os.path.join(
        workdir,
        "out",
    )

    user_code_path = os.path.join(
        workdir,
        "user_code.py",
    )

    wrapper_path = os.path.join(
        workdir,
        "_wrapper.py",
    )

    try:
        with open(
            user_code_path,
            "w",
            encoding="utf-8",
        ) as f:
            f.write(req.code)

        with open(
            wrapper_path,
            "w",
            encoding="utf-8",
        ) as f:
            f.write(
                _wrapper_python(
                    user_code_path,
                    output_dir,
                    project_dir,
                )
            )

        full_output, timed_out, duration = await _run_process(
            ["python3", wrapper_path],
            workdir,
            timeout,
            workspace=persistent_workspace,
        )

        stdout_text = full_output
        error_text = None
        images: list[str] = []

        if OUTPUT_MARKER in full_output:

            stdout_text, _, result_json = full_output.partition(
                OUTPUT_MARKER
            )

            try:
                parsed = json.loads(
                    result_json.strip()
                )

                error_text = parsed.get("error")
                images = parsed.get("images", [])

            except json.JSONDecodeError:
                pass

        elif timed_out:
            error_text = (
                f"Execution exceeded {timeout}s "
                "timeout and was killed."
            )

        if len(stdout_text) > MAX_OUTPUT_CHARS:
            stdout_text = (
                stdout_text[:MAX_OUTPUT_CHARS]
                + "\n... [truncated]"
            )

        return ExecuteResponse(
            stdout=stdout_text.strip(),
            error=error_text,
            images=images,
            timed_out=timed_out,
            duration_seconds=duration,
        )

    finally:
        if not req.session_id:
            shutil.rmtree(
                workdir,
                ignore_errors=True,
            )


@app.post(
    "/shell",
    response_model=ShellResponse,
)
async def shell(req: ShellRequest) -> ShellResponse:

    timeout = min(
        max(req.timeout or DEFAULT_TIMEOUT, 1),
        MAX_TIMEOUT,
    )

    run_id = uuid.uuid4().hex

    workdir = _prepare_workspace(
        run_id,
        req.session_id,
        req.user_id,
        req.db_base64,
    )

    persistent_workspace = _user_workspace(req.user_id)

    project_dir = os.path.join(
        workdir,
        "project",
    )

    try:
        output, timed_out, duration = await _run_process(
            [
                "/bin/bash",
                "-lc",
                req.command,
            ],
            project_dir,
            timeout,
            workspace=persistent_workspace,
        )

        error = None

        if timed_out:
            error = (
                f"Shell command exceeded {timeout}s "
                "timeout and was killed."
            )

        if len(output) > MAX_OUTPUT_CHARS:
            output = (
                output[:MAX_OUTPUT_CHARS]
                + "\n... [truncated]"
            )

        return ShellResponse(
            stdout=output.strip(),
            error=error,
            timed_out=timed_out,
            duration_seconds=duration,
            workspace=str(persistent_workspace),
        )

    finally:
        if not req.session_id:
            shutil.rmtree(
                workdir,
                ignore_errors=True,
            )


@app.post(
    "/install",
    response_model=InstallResponse,
)
async def install(req: InstallRequest) -> InstallResponse:

    timeout = min(
        max(
            req.timeout or INSTALL_TIMEOUT_DEFAULT,
            1,
        ),
        INSTALL_TIMEOUT_MAX,
    )

    os.makedirs(
        PACKAGES_DIR,
        exist_ok=True,
    )

    start = time.monotonic()

    try:
        proc = await asyncio.create_subprocess_exec(
            "pip",
            "install",
            "--no-cache-dir",
            "--target",
            PACKAGES_DIR,
            req.package,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        try:
            raw, _ = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )

            success = proc.returncode == 0

        except asyncio.TimeoutError:

            proc.kill()

            await proc.wait()

            return InstallResponse(
                success=False,
                stdout=(
                    f"Install of '{req.package}' "
                    f"exceeded {timeout}s and was killed."
                ),
                duration_seconds=round(
                    time.monotonic() - start,
                    2,
                ),
            )

        output = raw.decode(
            errors="replace"
        )

    except Exception as exc:

        return InstallResponse(
            success=False,
            stdout=(
                f"{type(exc).__name__}: {exc}"
            ),
            duration_seconds=round(
                time.monotonic() - start,
                2,
            ),
        )

    if len(output) > 8000:
        output = (
            output[:8000]
            + "\n... [truncated]"
        )

    return InstallResponse(
        success=success,
        stdout=output.strip(),
        duration_seconds=round(
            time.monotonic() - start,
            2,
        ),
    )


def _resolve_workspace_file(
    user_id: str,
    path: str,
) -> Path:
    """Resolve a path strictly inside the current user's workspace."""

    root = _user_workspace(user_id)

    raw = str(path or "").strip()

    if not raw:
        raise HTTPException(
            status_code=400,
            detail="Workspace path is required.",
        )

    candidate = Path(raw)

    if candidate.is_absolute():
        raise HTTPException(
            status_code=400,
            detail="Workspace path must be relative.",
        )

    try:
        resolved = (root / candidate).resolve()
        resolved.relative_to(root)
    except ValueError:
        raise HTTPException(
            status_code=403,
            detail="Path must remain inside the current user's workspace.",
        )

    return resolved


@app.get("/workspace/list")
async def workspace_list(user_id: str):
    """List only the current user's persistent workspace."""

    root = _user_workspace(user_id)

    files = []

    for path in sorted(root.rglob("*")):
        try:
            relative = path.relative_to(root)
            stat = path.stat()
        except (OSError, ValueError):
            continue

        files.append(
            {
                "path": str(relative),
                "name": path.name,
                "size": stat.st_size if path.is_file() else 0,
                "is_dir": path.is_dir(),
            }
        )

    return {
        "files": files,
    }


@app.post(
    "/workspace/read",
    response_model=WorkspaceFileResponse,
)
async def workspace_read(
    req: WorkspaceFileRequest,
) -> WorkspaceFileResponse:

    path = _resolve_workspace_file(
        req.user_id,
        req.path,
    )

    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail="Workspace file does not exist.",
        )

    if not path.is_file():
        raise HTTPException(
            status_code=400,
            detail="Workspace path is not a regular file.",
        )

    max_file_size = 25 * 1024 * 1024

    try:
        size = path.stat().st_size

        if size > max_file_size:
            raise HTTPException(
                status_code=413,
                detail=(
                    "Workspace file is too large to send through Discord "
                    f"({size} bytes > {max_file_size} bytes)."
                ),
            )

        data = path.read_bytes()

    except HTTPException:
        raise

    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Unable to read workspace file: {exc}",
        )

    user_root = _user_workspace(req.user_id)

    return WorkspaceFileResponse(
        path=str(path.relative_to(user_root)),
        filename=path.name,
        size=len(data),
        content_base64=base64.b64encode(data).decode("ascii"),
    )


@app.get("/health")
async def health():
    WORKSPACE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    return {
        "status": "ok",
        "workspace": str(WORKSPACE_DIR),
    }
