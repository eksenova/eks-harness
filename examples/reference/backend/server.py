from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import threading
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  kind TEXT NOT NULL,
  tax_number TEXT,
  parent_id INTEGER REFERENCES accounts(id),
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id INTEGER NOT NULL REFERENCES accounts(id),
  email TEXT NOT NULL UNIQUE,
  role TEXT NOT NULL,
  token TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS payments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id INTEGER NOT NULL REFERENCES accounts(id),
  amount INTEGER NOT NULL,
  iban TEXT NOT NULL,
  reference TEXT NOT NULL,
  paid_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
  name TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""
MIGRATIONS = hashlib.sha256(SCHEMA.encode()).hexdigest()[:16]
TABLES = ("accounts", "users", "payments", "settings")


class Api:
    def __init__(self, data: Path, admin_key: str, infra: str | None) -> None:
        self.data = data
        self.db_path = data / "reference.sqlite"
        self.notes_path = data / "notes.json"
        self.snapshots = data / "snapshots"
        self.admin_key = admin_key
        self.infra = infra.rstrip("/") if infra else None
        self.lock = threading.RLock()
        data.mkdir(parents=True, exist_ok=True)
        self.snapshots.mkdir(exist_ok=True)
        self.db = self.connect()

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.executescript(SCHEMA)
        return db

    def notes(self) -> list[dict[str, Any]]:
        try:
            return json.loads(self.notes_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []

    def save_notes(self, notes: list[dict[str, Any]]) -> None:
        self.notes_path.write_text(json.dumps(notes, ensure_ascii=False, sort_keys=True), encoding="utf-8")

    def dump(self) -> dict[str, list[dict[str, Any]]]:
        return {table: [dict(row) for row in self.db.execute(f"SELECT * FROM {table} ORDER BY 1")]
                for table in TABLES}

    def digest(self) -> str:
        text = json.dumps(self.dump(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(text.encode()).hexdigest()

    def reset(self) -> None:
        with self.lock:
            self.db.close()
            self.db_path.unlink(missing_ok=True)
            self.save_notes([])
            self.db = self.connect()

    def snapshot(self, name: str) -> dict[str, Any]:
        with self.lock:
            target = self.snapshots / f"{name}.sqlite"
            backup = sqlite3.connect(target)
            try:
                self.db.backup(backup)
            finally:
                backup.close()
            return {"digest": self.digest(), "name": name}

    def restore(self, name: str) -> None:
        with self.lock:
            source = self.snapshots / f"{name}.sqlite"
            if not source.is_file():
                raise LookupError(f"no snapshot {name}")
            self.db.close()
            shutil.copyfile(source, self.db_path)
            self.save_notes([])
            self.db = self.connect()

    def drop(self, name: str) -> None:
        (self.snapshots / f"{name}.sqlite").unlink(missing_ok=True)

    def audit(self, line: str) -> None:
        if not self.infra:
            return
        request = urllib.request.Request(f"{self.infra}/audit", data=line.encode(), method="POST")
        urllib.request.urlopen(request, timeout=5).read()


class Infra:
    def __init__(self, data: Path) -> None:
        data.mkdir(parents=True, exist_ok=True)
        self.log = data / "audit.log"
        self.lock = threading.Lock()

    def append(self, line: str) -> int:
        with self.lock, open(self.log, "a", encoding="utf-8") as handle:
            handle.write(line.rstrip("\n") + "\n")
        return sum(1 for _ in open(self.log, encoding="utf-8"))


class Handler(BaseHTTPRequestHandler):
    api: Api | None = None
    infra: Infra | None = None

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.command} {self.path} {args[1] if len(args) > 1 else ''}", flush=True)

    def reply(self, status: int, body: Any = None) -> None:
        data = b"" if body is None else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        return json.loads(raw) if raw else None

    def route(self) -> None:
        url = urlparse(self.path)
        query = {key: values[-1] for key, values in parse_qs(url.query).items()}
        try:
            if url.path == "/health":
                self.reply(200, {"ok": True})
            elif self.infra is not None:
                self.infra_route(url.path)
            else:
                self.api_route(url.path, query)
        except LookupError as error:
            self.reply(404, {"error": str(error)})
        except (ValueError, KeyError, sqlite3.IntegrityError) as error:
            self.reply(400, {"error": f"{type(error).__name__}: {error}"})

    def infra_route(self, path: str) -> None:
        assert self.infra is not None
        if self.command == "POST" and path == "/audit":
            length = int(self.headers.get("Content-Length") or 0)
            self.reply(200, {"lines": self.infra.append(self.rfile.read(length).decode())})
        else:
            raise LookupError(path)

    def admin(self) -> bool:
        assert self.api is not None
        if self.headers.get("X-Admin-Key") != self.api.admin_key:
            self.reply(HTTPStatus.FORBIDDEN, {"error": "admin key required"})
            return False
        return True

    def user(self) -> dict[str, Any] | None:
        assert self.api is not None
        token = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        if not token:
            return None
        row = self.api.db.execute("SELECT * FROM users WHERE token = ?", (token,)).fetchone()
        return dict(row) if row else None

    def api_route(self, path: str, query: dict[str, str]) -> None:
        api = self.api
        assert api is not None
        if path.startswith("/admin/"):
            if not self.admin():
                return
            action = path.removeprefix("/admin/")
            if action == "reset" and self.command == "POST":
                api.reset()
                self.reply(200, {"ok": True})
            elif action == "snapshot" and self.command == "POST":
                self.reply(200, api.snapshot(query["name"]))
            elif action == "snapshot" and self.command == "DELETE":
                api.drop(query["name"])
                self.reply(200, {"ok": True})
            elif action == "restore" and self.command == "POST":
                api.restore(query["name"])
                self.reply(200, {"ok": True})
            elif action == "dump" and self.command == "GET":
                self.reply(200, {"tables": api.dump(), "notes": api.notes(), "digest": api.digest()})
            elif action == "migrations":
                self.reply(200, {"migrations": MIGRATIONS})
            else:
                raise LookupError(path)
            return
        if path == "/api/accounts" and self.command == "POST":
            body = self.body()
            with api.lock:
                cursor = api.db.execute(
                    "INSERT INTO accounts (name, kind, tax_number, parent_id, created_at) VALUES (?, ?, ?, ?, ?)",
                    (body["name"], body["kind"], body.get("taxNumber"), body.get("parentId"), body["createdAt"]))
                row = api.db.execute("SELECT * FROM accounts WHERE id = ?", (cursor.lastrowid,)).fetchone()
            api.audit(f"account {row['id']} {row['name']}")
            self.reply(201, dict(row))
        elif path == "/api/accounts" and self.command == "GET":
            user = self.user()
            if user is None:
                self.reply(401, {"error": "sign in"})
                return
            rows = api.db.execute("SELECT * FROM accounts WHERE id = ? OR parent_id = ? ORDER BY id",
                                  (user["account_id"], user["account_id"])).fetchall()
            self.reply(200, {"items": [dict(row) for row in rows]})
        elif path == "/api/users" and self.command == "POST":
            body = self.body()
            with api.lock:
                cursor = api.db.execute("INSERT INTO users (account_id, email, role, token) VALUES (?, ?, ?, ?)",
                                        (body["accountId"], body["email"], body["role"], body["token"]))
            self.reply(201, {"id": cursor.lastrowid, "email": body["email"]})
        elif path == "/api/me" and self.command == "GET":
            user = self.user()
            if user is None:
                self.reply(401, {"error": "sign in"})
                return
            account = api.db.execute("SELECT * FROM accounts WHERE id = ?", (user["account_id"],)).fetchone()
            self.reply(200, {"email": user["email"], "role": user["role"], "account": dict(account)})
        elif path == "/api/payments" and self.command == "POST":
            user = self.user()
            if user is None:
                self.reply(401, {"error": "sign in"})
                return
            body = self.body()
            with api.lock:
                cursor = api.db.execute(
                    "INSERT INTO payments (account_id, amount, iban, reference, paid_at) VALUES (?, ?, ?, ?, ?)",
                    (body["accountId"], body["amount"], body["iban"], body["reference"], body["paidAt"]))
            self.reply(201, {"id": cursor.lastrowid})
        elif path == "/api/payments" and self.command == "GET":
            user = self.user()
            if user is None:
                self.reply(401, {"error": "sign in"})
                return
            rows = api.db.execute("SELECT * FROM payments WHERE account_id = ? ORDER BY id",
                                  (user["account_id"],)).fetchall()
            self.reply(200, {"items": [dict(row) for row in rows]})
        elif path == "/api/settings" and self.command == "PUT":
            body = self.body()
            with api.lock:
                api.db.execute("INSERT OR REPLACE INTO settings (name, value) VALUES (?, ?)",
                               (body["name"], json.dumps(body["value"])))
            self.reply(200, {"name": body["name"]})
        elif path == "/api/notes" and self.command == "POST":
            body = self.body()
            with api.lock:
                notes = api.notes()
                note = {"id": len(notes) + 1, "account": body["accountId"], "text": body["text"]}
                api.save_notes([*notes, note])
            self.reply(201, note)
        elif path == "/api/notes" and self.command == "GET":
            self.reply(200, {"items": api.notes()})
        else:
            raise LookupError(path)

    def do_GET(self) -> None:
        self.route()

    def do_POST(self) -> None:
        self.route()

    def do_PUT(self) -> None:
        self.route()

    def do_DELETE(self) -> None:
        self.route()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["api", "infra"])
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--admin-key", default="")
    parser.add_argument("--infra")
    args = parser.parse_args()
    if args.mode == "api":
        Handler.api = Api(args.data, args.admin_key, args.infra)
    else:
        Handler.infra = Infra(args.data)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"reference {args.mode} listening on {args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
