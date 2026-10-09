from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time


def _parent_alive(parent: int) -> bool:
    if os.getppid() != parent:
        return False
    try:
        os.kill(parent, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eks_harness.live.watchdog",
                                     description="run a recorder that must only ever be stopped with SIGINT")
    parser.add_argument("--parent", type=int, required=True)
    parser.add_argument("--grace", type=float, default=20.0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("no command given")
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    try:
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, restore_signals=False, process_group=0)
    except OSError as error:
        print(f"could not start {command[0]}: {error}", file=sys.stderr, flush=True)
        return 127
    state = {"interrupted_at": None}

    def interrupt(*_: object) -> None:
        if child.poll() is not None:
            return
        now = time.monotonic()
        last = state["interrupted_at"]
        if last is None or now - last >= args.grace:
            state["interrupted_at"] = now
            try:
                os.kill(child.pid, signal.SIGINT)
            except ProcessLookupError:
                pass

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, interrupt)
    while True:
        try:
            code = child.wait(timeout=0.25)
            break
        except subprocess.TimeoutExpired:
            pass
        if not _parent_alive(args.parent):
            interrupt()
        elif state["interrupted_at"] is not None and time.monotonic() - state["interrupted_at"] >= args.grace:
            interrupt()
    return 128 - code if code < 0 else code


if __name__ == "__main__":
    sys.exit(main())
