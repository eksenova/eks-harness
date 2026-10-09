from __future__ import annotations

import logging
import os

log = logging.getLogger("eks_harness.fds")

FILE_LIMIT = 8192
PRESSURE_RATIO = 0.8
FD_DIRS = ("/dev/fd", "/proc/self/fd")


def file_limits() -> tuple[int, int] | None:
    try:
        import resource
    except ImportError:
        return None
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    return soft, hard


def raise_file_limit(target: int = FILE_LIMIT) -> tuple[int, int] | None:
    try:
        import resource
    except ImportError:
        return None
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    wanted = target if hard == resource.RLIM_INFINITY else min(target, hard)
    if soft != resource.RLIM_INFINITY and soft < wanted:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (wanted, hard))
            log.info("raised the open file limit from %d to %d", soft, wanted)
            soft = wanted
        except (ValueError, OSError) as error:
            log.warning("could not raise the open file limit from %d to %d: %s", soft, wanted, error)
    return soft, hard


def open_fds() -> int | None:
    for folder in FD_DIRS:
        try:
            return len(os.listdir(folder))
        except OSError:
            continue
    return None


def usage(db_connections: int | None = None) -> dict | None:
    count = open_fds()
    limits = file_limits()
    if count is None or limits is None:
        return None
    soft = limits[0]
    limit = None if soft < 0 or soft >= 2 ** 62 else soft
    return {"open": count, "limit": limit, "dbConnections": db_connections,
            "pressure": bool(limit) and count >= limit * PRESSURE_RATIO}
