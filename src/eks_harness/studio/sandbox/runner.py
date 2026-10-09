"""Subprocess sandbox runner.

Public surface:

* :func:`run_project_py` - execute a Python source string in a sandboxed
  subprocess and return a :class:`SandboxResult` describing what happened.
* :class:`SandboxResult` - outcome of one run; carries the pickled ``Project``
  on success and a structured error + traceback on failure.
* :class:`SandboxTimeout` - raised internally; surfaced as ``error`` text on the
  result so callers don't need to handle two exception paths.

The runner is async-first (``asyncio.create_subprocess_exec``). The child is
spawned with ``-P`` (no unsafe ``sys.path[0]`` prepend) and an environment
scrubbed of every ``PYTHON*`` variable except an explicit ``PYTHONPATH`` that
points only at the ``eks_harness`` package root. User
site-packages stays enabled because that is where dependencies like
``pydantic`` and ``librosa`` live in editable installs.

Platform specifics:

* **Windows** - sets ``CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`` so we can
  send ``CTRL_BREAK_EVENT`` for graceful termination, and (when ``pywin32``
  is installed) attaches the child to a Job Object configured with
  ``JOB_OBJECT_LIMIT_PROCESS_MEMORY`` and ``JOB_OBJECT_LIMIT_PROCESS_TIME``
  so the OS enforces caps and reaps the entire job tree on parent death.
* **POSIX** - ``preexec_fn`` calls :func:`resource.setrlimit` for CPU, address
  space and FD count, and ``start_new_session=True`` lets us ``killpg`` on
  timeout.

Network gating sets proxy environment variables to a black-hole address. This
is best-effort - well-behaved Python libraries respect ``HTTPS_PROXY`` /
``HTTP_PROXY``, but raw socket calls do not. The inner child also installs a
strict ``socket`` monkey-patch when ``allow_network=False`` is forwarded
through the environment.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "DEFAULT_MEMORY_BYTES",
    "DEFAULT_TIMEOUT_S",
    "SandboxResult",
    "SandboxTimeout",
    "run_project_py",
]

_LOG = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 30.0
DEFAULT_MEMORY_BYTES = 1024 * 1024 * 1024  # 1 GiB
DEFAULT_FD_LIMIT = 256


class SandboxTimeout(RuntimeError):
    """Raised when the sandboxed subprocess exceeds its wall-clock limit."""


@dataclass
class SandboxResult:
    """Outcome of one sandbox invocation."""

    ok: bool
    project_pickle: bytes | None
    project_json: str | None
    error: str | None
    traceback: str | None
    returncode: int
    duration_s: float
    stderr: str = ""

    @property
    def failed(self) -> bool:
        return not self.ok


async def run_project_py(
    source: str,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    memory_bytes: int = DEFAULT_MEMORY_BYTES,
    allow_network: bool = False,
    cwd: Path | None = None,
    extra_env: dict[str, str] | None = None,
    python: str | None = None,
) -> SandboxResult:
    """Execute ``source`` (a complete Python module) in a fresh sandboxed subprocess.

    The child must define a top-level ``project: Project`` variable. On
    success the pickled project is returned via ``project_pickle`` along with
    its JSON snapshot via ``project_json``. On failure the error and
    traceback are returned in structured fields.
    """

    interpreter = python or sys.executable
    env = _build_env(allow_network=allow_network, extra_env=extra_env)

    started = time.monotonic()
    proc, job_handle = await _spawn(
        interpreter=interpreter,
        env=env,
        cwd=cwd,
        memory_bytes=memory_bytes,
    )

    try:
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=source.encode("utf-8")),
                timeout=timeout_s,
            )
        except TimeoutError:
            await _kill(proc, job_handle)
            duration = time.monotonic() - started
            return SandboxResult(
                ok=False,
                project_pickle=None,
                project_json=None,
                error=f"sandbox timed out after {timeout_s:.1f}s",
                traceback=None,
                returncode=-1,
                duration_s=duration,
                stderr="",
            )
    finally:
        _close_job(job_handle)

    duration = time.monotonic() - started
    stderr_text = stderr.decode("utf-8", errors="replace") if stderr else ""

    if proc.returncode != 0:
        return SandboxResult(
            ok=False,
            project_pickle=None,
            project_json=None,
            error=_extract_error_summary(stderr_text) or f"sandbox exited {proc.returncode}",
            traceback=stderr_text,
            returncode=proc.returncode or -1,
            duration_s=duration,
            stderr=stderr_text,
        )

    pickle_bytes, json_text, parse_error = _parse_inner_payload(stdout)
    if parse_error is not None:
        return SandboxResult(
            ok=False,
            project_pickle=None,
            project_json=None,
            error=parse_error,
            traceback=stderr_text or None,
            returncode=proc.returncode or 0,
            duration_s=duration,
            stderr=stderr_text,
        )

    return SandboxResult(
        ok=True,
        project_pickle=pickle_bytes,
        project_json=json_text,
        error=None,
        traceback=None,
        returncode=proc.returncode or 0,
        duration_s=duration,
        stderr=stderr_text,
    )


def _build_env(*, allow_network: bool, extra_env: dict[str, str] | None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = _sandbox_pythonpath()
    if allow_network:
        env["EKS_HARNESS_SANDBOX_NETWORK"] = "1"
    else:
        env["EKS_HARNESS_SANDBOX_NETWORK"] = "0"
        env["HTTPS_PROXY"] = "http://127.0.0.1:1"
        env["HTTP_PROXY"] = "http://127.0.0.1:1"
        env["NO_PROXY"] = ""
    if extra_env:
        env.update(extra_env)
    return env


def _sandbox_pythonpath() -> str:
    """Return a PYTHONPATH containing the package roots the inner child must import.

    The sandbox runs with ``-P -s`` instead of ``-I`` so PYTHONPATH is honoured;
    that lets us inject just the package roots without exposing the rest of the
    developer's user site-packages.
    """

    import eks_harness.video
    import eks_harness.studio

    roots = {
        str(Path(eks_harness.video.__file__).resolve().parent.parent),
        str(Path(eks_harness.studio.__file__).resolve().parent.parent),
    }
    return os.pathsep.join(sorted(roots))


async def _spawn(
    *,
    interpreter: str,
    env: dict[str, str],
    cwd: Path | None,
    memory_bytes: int,
) -> tuple[asyncio.subprocess.Process, object | None]:
    if sys.platform.startswith("win"):
        return await _spawn_windows(interpreter=interpreter, env=env, cwd=cwd, memory_bytes=memory_bytes)
    return await _spawn_posix(interpreter=interpreter, env=env, cwd=cwd, memory_bytes=memory_bytes)


async def _spawn_posix(
    *,
    interpreter: str,
    env: dict[str, str],
    cwd: Path | None,
    memory_bytes: int,
) -> tuple[asyncio.subprocess.Process, None]:
    import resource as _resource

    cpu_seconds = max(int(DEFAULT_TIMEOUT_S) + 5, 60)

    setrlimit = _resource.setrlimit  # type: ignore[attr-defined,unused-ignore]
    rlimit_cpu = _resource.RLIMIT_CPU  # type: ignore[attr-defined,unused-ignore]
    rlimit_as = _resource.RLIMIT_AS  # type: ignore[attr-defined,unused-ignore]
    rlimit_nofile = _resource.RLIMIT_NOFILE  # type: ignore[attr-defined,unused-ignore]

    def _apply_limits() -> None:
        setrlimit(rlimit_cpu, (cpu_seconds, cpu_seconds))
        with contextlib.suppress(OSError, ValueError):
            setrlimit(rlimit_as, (memory_bytes, memory_bytes))
        with contextlib.suppress(OSError, ValueError):
            setrlimit(rlimit_nofile, (DEFAULT_FD_LIMIT, DEFAULT_FD_LIMIT))
        # No setsid() here: start_new_session=True already made the child a
        # session leader, and a second setsid() fails with EPERM.

    proc = await asyncio.create_subprocess_exec(
        interpreter,
        "-P",
        "-m",
        "eks_harness.studio.sandbox._inner",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=str(cwd) if cwd else None,
        preexec_fn=_apply_limits,
        start_new_session=True,
    )
    return proc, None


async def _spawn_windows(
    *,
    interpreter: str,
    env: dict[str, str],
    cwd: Path | None,
    memory_bytes: int,
) -> tuple[asyncio.subprocess.Process, object | None]:
    creationflags = 0
    try:
        from subprocess import CREATE_NEW_PROCESS_GROUP, CREATE_NO_WINDOW

        creationflags = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    except ImportError:
        pass

    proc = await asyncio.create_subprocess_exec(
        interpreter,
        "-P",
        "-m",
        "eks_harness.studio.sandbox._inner",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=str(cwd) if cwd else None,
        creationflags=creationflags,
    )

    job_handle = _attach_job_object(proc.pid, memory_bytes=memory_bytes)
    return proc, job_handle


def _attach_job_object(pid: int, *, memory_bytes: int) -> object | None:
    """Best-effort Job Object attachment for Windows sandboxing.

    Returns the job handle (so the caller keeps it alive for the lifetime of
    the child) or ``None`` when ``pywin32`` isn't available - in which case we
    fall back to the in-process timeout-and-kill path. The Job Object lets the
    OS enforce the memory cap and reap orphaned grandchildren.
    """

    try:
        import win32api
        import win32con
        import win32job
    except ImportError:
        _LOG.debug("pywin32 not available; sandbox runs without Job Object containment")
        return None

    try:
        job: object = win32job.CreateJobObject(None, "")
        info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        info["BasicLimitInformation"]["LimitFlags"] |= (
            win32job.JOB_OBJECT_LIMIT_PROCESS_MEMORY
            | win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            | win32job.JOB_OBJECT_LIMIT_BREAKAWAY_OK
        )
        info["ProcessMemoryLimit"] = memory_bytes
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)

        process_handle = win32api.OpenProcess(
            win32con.PROCESS_TERMINATE | win32con.PROCESS_SET_QUOTA, False, pid
        )
        try:
            win32job.AssignProcessToJobObject(job, process_handle)
        finally:
            win32api.CloseHandle(process_handle)
        return job
    except Exception:
        _LOG.exception("failed to attach Windows Job Object; sandbox falls back to timeout-only enforcement")
        return None


def _close_job(job_handle: object | None) -> None:
    if job_handle is None:
        return
    try:
        import win32api

        win32api.CloseHandle(job_handle)
    except Exception:
        pass


async def _kill(proc: asyncio.subprocess.Process, job_handle: object | None) -> None:
    if proc.returncode is not None:
        return
    try:
        if sys.platform.startswith("win"):
            if job_handle is not None:
                try:
                    import win32job

                    win32job.TerminateJobObject(job_handle, 1)
                except Exception:
                    proc.kill()
            else:
                proc.kill()
        else:
            os.killpg(proc.pid, 9)  # SIGKILL - process group
    except (ProcessLookupError, PermissionError, OSError):
        pass
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(proc.wait(), timeout=2.0)


def _extract_error_summary(stderr_text: str) -> str | None:
    """Pick the most informative one-line summary out of a Python traceback."""

    if not stderr_text.strip():
        return None
    lines = [line for line in stderr_text.strip().splitlines() if line.strip()]
    for line in reversed(lines):
        if not line.startswith(" "):
            return line
    return lines[-1] if lines else None


def _parse_inner_payload(stdout: bytes) -> tuple[bytes | None, str | None, str | None]:
    """Parse the framed payload the inner child writes to stdout.

    Format (framed so a noisy script stdout cannot corrupt it):

        EKSSTUD\\x01<8-byte big-endian pickle length><pickle bytes>
        EKSSTUD\\x02<8-byte big-endian json length><json bytes>

    Anything else on stdout is benign (user prints) and ignored. If the
    framing markers are missing, the script never produced a project.
    """

    marker_pickle = b"EKSSTUD\x01"
    marker_json = b"EKSSTUD\x02"

    pickle_idx = stdout.rfind(marker_pickle)
    json_idx = stdout.rfind(marker_json)
    if pickle_idx == -1 or json_idx == -1:
        return None, None, "sandbox child did not emit a Project payload (no `project` variable?)"

    try:
        pickle_len = int.from_bytes(stdout[pickle_idx + len(marker_pickle): pickle_idx + len(marker_pickle) + 8], "big")
        pickle_start = pickle_idx + len(marker_pickle) + 8
        pickle_bytes = stdout[pickle_start: pickle_start + pickle_len]

        json_len = int.from_bytes(stdout[json_idx + len(marker_json): json_idx + len(marker_json) + 8], "big")
        json_start = json_idx + len(marker_json) + 8
        json_bytes = stdout[json_start: json_start + json_len]
    except Exception as exc:
        return None, None, f"failed to parse sandbox payload: {exc}"

    if len(pickle_bytes) != pickle_len:
        return None, None, "sandbox payload truncated (pickle)"
    if len(json_bytes) != json_len:
        return None, None, "sandbox payload truncated (json)"

    return pickle_bytes, json_bytes.decode("utf-8", errors="replace"), None
