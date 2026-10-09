"""In-sandbox executor.

Run as ``python -m eks_harness.studio.sandbox._inner``. Reads a complete Python
module from stdin, executes it in a fresh namespace, expects a top-level
``project`` variable that is a :class:`eks_harness.video.ir.Project` instance, and
writes a framed ``(pickle, json)`` payload to stdout.

The framing is deliberate - the user script is allowed to ``print()``;
without framing those prints would corrupt the payload. The format is
documented in :func:`eks_harness.studio.sandbox.runner._parse_inner_payload`.
"""

from __future__ import annotations

import os
import pickle
import sys
import traceback
from typing import Any


def _install_network_block() -> None:
    """Patch ``socket.socket`` so the script cannot open network connections.

    Best-effort companion to the runner's proxy env-var trick; covers libraries
    that bypass proxies (raw sockets, urllib3 with proxy override). Ignored
    when ``EKS_HARNESS_SANDBOX_NETWORK=1``.
    """

    if os.environ.get("EKS_HARNESS_SANDBOX_NETWORK", "0") == "1":
        return

    import socket

    blocked_kinds = {socket.AF_INET, socket.AF_INET6}
    real_socket = socket.socket

    class _BlockedSocket(real_socket):  # type: ignore[misc, valid-type]
        def __init__(self, family: int = socket.AF_INET, *args: Any, **kwargs: Any) -> None:
            if family in blocked_kinds:
                raise PermissionError("network access is disabled in the studio sandbox")
            super().__init__(family, *args, **kwargs)

    socket.socket = _BlockedSocket  # type: ignore[misc]


def _emit(stream: Any, marker: bytes, payload: bytes) -> None:
    stream.write(marker)
    stream.write(len(payload).to_bytes(8, "big"))
    stream.write(payload)
    stream.flush()


def main() -> int:
    _install_network_block()

    source = sys.stdin.read()

    namespace: dict[str, Any] = {"__name__": "__eks_studio_sandbox__"}
    try:
        compiled = compile(source, "<sandbox>", "exec")
        exec(compiled, namespace)
    except SystemExit as exc:
        sys.stderr.write(f"project.py called sys.exit({exc.code!r})\n")
        return 2
    except BaseException:
        traceback.print_exc(file=sys.stderr)
        return 3

    project = namespace.get("project")
    if project is None:
        sys.stderr.write("project.py did not define a top-level `project` variable\n")
        return 4

    try:
        from eks_harness.video.ir import Project
    except ImportError as exc:
        sys.stderr.write(f"the video engine is not importable inside the sandbox: {exc}\n")
        return 5

    if not isinstance(project, Project):
        sys.stderr.write(
            f"`project` must be a eks_harness.video.ir.Project; got {type(project).__name__}\n"
        )
        return 6

    try:
        json_snapshot = project.model_dump_json(by_alias=True)
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 7

    try:
        pickled = pickle.dumps(project)
    except Exception:
        # Pydantic v2 RootModel parametrizations (e.g. Animated[float]) are
        # not picklable by name; the JSON snapshot is the load-bearing payload
        # for the caller, so we fall back to encoding the JSON as the pickle
        # slot - callers detect this via a sentinel marker.
        pickled = b"EKSSTUD_NOPICKLE"

    out: Any
    try:
        out = sys.stdout.buffer
    except AttributeError:
        out = sys.stdout
    _emit(out, b"EKSSTUD\x01", pickled)
    _emit(out, b"EKSSTUD\x02", json_snapshot.encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
