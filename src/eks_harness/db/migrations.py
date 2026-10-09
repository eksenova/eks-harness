from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass
from importlib import resources

MIGRATION_FILE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def discover() -> list[Migration]:
    package = resources.files("eks_harness.db")
    found = [Migration(1, "schema", (package / "schema.sql").read_text(encoding="utf-8"))]
    folder = package / "migrations"
    if folder.is_dir():
        for entry in folder.iterdir():
            match = MIGRATION_FILE.match(entry.name)
            if not match:
                continue
            version = int(match.group(1))
            if version <= 1:
                raise RuntimeError(f"migration {entry.name}: version 1 is schema.sql; numbered migrations start at 0002")
            found.append(Migration(version, match.group(2), entry.read_text(encoding="utf-8")))
    found.sort(key=lambda m: m.version)
    versions = [m.version for m in found]
    if len(versions) != len(set(versions)):
        raise RuntimeError(f"duplicate migration versions: {versions}")
    return found


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at REAL NOT NULL)")


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    _ensure_table(conn)
    return {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}


def current_version(conn: sqlite3.Connection) -> int:
    versions = applied_versions(conn)
    return max(versions) if versions else 0


def migrate(conn: sqlite3.Connection) -> list[int]:
    applied_now: list[int] = []
    _ensure_table(conn)
    for migration in discover():
        if migration.version in applied_versions(conn):
            continue
        script = (
            "BEGIN IMMEDIATE;\n"
            f"{migration.sql}\n;\n"
            f"INSERT INTO schema_migrations (version, name, applied_at) "
            f"VALUES ({migration.version}, '{migration.name}', {time.time()!r});\n"
            f"PRAGMA user_version = {migration.version};\n"
        )
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("PRAGMA legacy_alter_table = ON")
        try:
            conn.executescript(script)
            broken = conn.execute("PRAGMA foreign_key_check").fetchall()
            if broken:
                raise sqlite3.IntegrityError(f"{len(broken)} rows break foreign keys, first: {tuple(broken[0])}")
            conn.execute("COMMIT")
        except sqlite3.Error as error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise RuntimeError(f"database migration {migration.version:04d}_{migration.name} failed: {error}") from error
        finally:
            conn.execute("PRAGMA legacy_alter_table = OFF")
            conn.execute("PRAGMA foreign_keys = ON")
        applied_now.append(migration.version)
    conn.execute("PRAGMA foreign_keys = ON")
    return applied_now
