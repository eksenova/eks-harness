from __future__ import annotations

import os
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

BUSY_TIMEOUT_MS = 10_000


def _configure(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA temp_store = MEMORY")
    return conn


def private_files(path: Path) -> None:
    if os.name == "nt":
        return
    if not path.exists():
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
    for candidate in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        try:
            if candidate.stat().st_mode & 0o077:
                candidate.chmod(0o600)
        except FileNotFoundError:
            continue


def open_connection(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        private_files(Path(path))
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False, timeout=BUSY_TIMEOUT_MS / 1000)
    return _configure(conn)


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path) if str(path) != ":memory:" else path
        self._local = threading.local()
        self._all: list[tuple[threading.Thread, sqlite3.Connection]] = []
        self._all_lock = threading.Lock()
        self._closed = False

    def conn(self) -> sqlite3.Connection:
        if self._closed:
            raise RuntimeError("the database is closed")
        conn = getattr(self._local, "conn", None)
        if conn is None:
            self.reap()
            conn = open_connection(self.path)
            self._local.conn = conn
            self._local.depth = 0
            with self._all_lock:
                self._all.append((threading.current_thread(), conn))
        return conn

    def reap(self) -> int:
        with self._all_lock:
            dead = [conn for thread, conn in self._all if not thread.is_alive()]
            self._all = [(thread, conn) for thread, conn in self._all if thread.is_alive()]
        for conn in dead:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        return len(dead)

    def open_connections(self) -> int:
        with self._all_lock:
            return len(self._all)

    @contextmanager
    def transaction(self, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        conn = self.conn()
        depth = getattr(self._local, "depth", 0)
        if depth == 0:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        else:
            conn.execute(f"SAVEPOINT sp_{depth}")
        self._local.depth = depth + 1
        try:
            yield conn
        except BaseException:
            self._local.depth = depth
            if depth == 0:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
            else:
                conn.execute(f"ROLLBACK TO SAVEPOINT sp_{depth}")
                conn.execute(f"RELEASE SAVEPOINT sp_{depth}")
            raise
        else:
            self._local.depth = depth
            if depth == 0:
                conn.execute("COMMIT")
            else:
                conn.execute(f"RELEASE SAVEPOINT sp_{depth}")

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        return self.conn().execute(sql, params)

    def close_thread(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            self._local.conn = None
            with self._all_lock:
                self._all = [(thread, other) for thread, other in self._all if other is not conn]
            conn.close()

    def close(self) -> None:
        self._closed = True
        with self._all_lock:
            connections, self._all = [conn for _, conn in self._all], []
        for conn in connections:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    def checkpoint(self) -> None:
        self.conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")


def now() -> float:
    return time.time()
