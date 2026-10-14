from __future__ import annotations

import hashlib
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any

import eks
from eks import t
from eks_harness.plugins.sdk import (
    BackendInfo,
    BackendSpec,
    Health,
    ProcessSpec,
    Secret,
)

ROOT = Path(__file__).resolve().parents[3]
SERVER = ROOT / "backend" / "server.py"
ADMIN_KEY = "reference-admin"


def server_digest() -> str:
    return hashlib.sha256(SERVER.read_bytes()).hexdigest()[:16]


def state_dir(context: Mapping[str, Any], name: str) -> Path:
    path = Path(str(context["stateDir"])) / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def admin(context: Mapping[str, Any], method: str, path: str) -> dict[str, Any]:
    port = int(context["ports"]["api"])
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                     headers={"X-Admin-Key": ADMIN_KEY})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read()
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"{method} {path} answered {error.code}: {error.read().decode()[:300]}") from error
    return json.loads(body) if body else {}


@eks.backend("reference-infra", identity="global", idle_timeout=t("20s"),
             description="Shared audit store the reference API writes to; one instance for every tree")
class ReferenceInfra:
    def describe(self) -> dict[str, Any]:
        return {"description": "reference audit store", "ports": ["store"]}

    def fingerprint(self, context: Mapping[str, Any]) -> str:
        return server_digest()

    def prepare(self, context: Mapping[str, Any]) -> None:
        return None

    def spec(self, context: Mapping[str, Any]) -> BackendSpec:
        port = int(context["ports"]["store"])
        url = f"http://127.0.0.1:{port}"
        return BackendSpec(
            processes=[ProcessSpec(name="store", port="store",
                                   command=[sys.executable, str(SERVER), "infra", "--port", str(port), "--data",
                                            str(state_dir(context, "infra"))],
                                   health=Health(url=f"{url}/health", timeout=30))],
            info=BackendInfo(url=url, urls={"store": url}, version="1"))

    def teardown(self, context: Mapping[str, Any], final: bool = False) -> None:
        return None


@eks.backend("reference-api", requires=["reference-infra"],
             description="Reference JSON API over SQLite with seed snapshots")
class ReferenceApi:
    def describe(self) -> dict[str, Any]:
        return {"description": "reference API", "ports": ["api"]}

    def fingerprint(self, context: Mapping[str, Any]) -> str:
        return server_digest()

    def prepare(self, context: Mapping[str, Any]) -> None:
        if "reference-infra" not in (context.get("dependencies") or {}):
            raise RuntimeError("reference-api starts only with its reference-infra dependency")

    def spec(self, context: Mapping[str, Any]) -> BackendSpec:
        port = int(context["ports"]["api"])
        url = f"http://127.0.0.1:{port}"
        infra = context["dependencies"]["reference-infra"]["url"]
        return BackendSpec(
            processes=[ProcessSpec(name="api", port="api",
                                   command=[sys.executable, str(SERVER), "api", "--port", str(port), "--data",
                                            str(state_dir(context, "api")), "--admin-key", ADMIN_KEY,
                                            "--infra", infra],
                                   health=Health(url=f"{url}/health", timeout=30))],
            info=BackendInfo(url=url, urls={"api": url}, version="1", commit=server_digest(),
                             credentials={"admin": {"headers": {"X-Admin-Key": Secret(ADMIN_KEY)}}},
                             extra={"infra": infra}))

    def teardown(self, context: Mapping[str, Any], final: bool = False) -> None:
        return None

    def migrations(self, context: Mapping[str, Any]) -> str:
        return str(admin(context, "GET", "/admin/migrations")["migrations"])

    def reset(self, context: Mapping[str, Any]) -> dict[str, Any]:
        return admin(context, "POST", "/admin/reset")

    def seed_phase(self, context: Mapping[str, Any], phase: str) -> dict[str, Any]:
        return {"phase": phase}

    def snapshot(self, context: Mapping[str, Any], name: str) -> dict[str, Any]:
        return admin(context, "POST", f"/admin/snapshot?name={name}")

    def restore(self, context: Mapping[str, Any], name: str) -> dict[str, Any]:
        return admin(context, "POST", f"/admin/restore?name={name}")

    def drop_snapshot(self, context: Mapping[str, Any], name: str) -> dict[str, Any]:
        return admin(context, "DELETE", f"/admin/snapshot?name={name}")


@eks.resource("setting", phase="schema", description="A server setting written before any data")
class Setting(eks.Resource):
    value: str

    def create(self, ctx: Any) -> dict[str, Any]:
        ctx.api.put("/api/settings", json={"name": ctx.key, "value": self.value}, as_="admin")
        return {"name": ctx.key, "value": self.value}


@eks.resource("account", description="A company account with a generated tax number")
class Account(eks.Resource):
    name: str

    def create(self, ctx: Any) -> dict[str, Any]:
        parent = ctx.parent or {}
        body = {"name": self.name, "kind": "branch" if parent else "company", "taxNumber": ctx.ids.vkn(),
                "parentId": parent.get("id"), "createdAt": ctx.now.isoformat()}
        return dict(ctx.api.post("/api/accounts", json=body))


@eks.resource("branch", parent="account", description="A branch account under a company")
class Branch(eks.Resource):
    name: str

    def create(self, ctx: Any) -> dict[str, Any]:
        body = {"name": self.name, "kind": "branch", "taxNumber": ctx.ids.vkn(), "parentId": ctx.parent["id"],
                "createdAt": ctx.now.isoformat()}
        return dict(ctx.api.post("/api/accounts", json=body))


@eks.resource("user", parent="account", persona=True, description="A user of an account; a seed persona")
class User(eks.Resource):
    role: str = "admin"

    def create(self, ctx: Any) -> dict[str, Any]:
        token = ctx.ids.uuid()
        email = ctx.ids.email(local=ctx.key)
        created = ctx.api.post("/api/users", json={"accountId": ctx.parent["id"], "email": email,
                                                    "role": self.role, "token": token})
        return {"id": created["id"], "email": email, "role": self.role, "token": token, "persona": ctx.key,
                "account": ctx.parent["id"], "label": f"{ctx.key} ({self.role})"}


@eks.resource("payment", parent="account", keyed=False, positional=("amount",), phase="finalize",
              description="A payment into an account, made by one of its users")
class Payment(eks.Resource):
    amount: int
    payer: Annotated[eks.ResourceRef, eks.refers_to("user")]

    def create(self, ctx: Any) -> dict[str, Any]:
        payer = ctx.resolve(self.payer)
        body = {"accountId": ctx.parent["id"], "amount": self.amount, "iban": ctx.ids.iban(),
                "reference": ctx.ids.ulid(), "paidAt": ctx.now.isoformat()}
        created = ctx.api.post("/api/payments", json=body, as_=payer["persona"])
        return {"id": created["id"], **body}


@eks.resource("note", parent="account", keyed=False, positional=("text",), phase="mongo",
              description="A note kept outside the database, replayed after every restore")
class Note(eks.Resource):
    text: str

    def create(self, ctx: Any) -> dict[str, Any]:
        return dict(ctx.api.post("/api/notes", json={"accountId": ctx.parent["id"], "text": self.text}))
