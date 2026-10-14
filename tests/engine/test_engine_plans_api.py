from __future__ import annotations

import shutil
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import AuthEnv
from fastapi.testclient import TestClient

from eks.workspace import plan_spec
from eks_harness.ir.registry import Registry, default_registry

FIXTURES = Path(__file__).resolve().parent.parent / "eks" / "cli_fixtures"
PROJECT = "acme/cli"

INLINE = """
    import eks


    @eks.flow(app="web-app", seed="standard", as_="harman-admin")
    def inline(s: eks.Session, page: str = "/a"):
        s.ui.nav(page)


    @eks.flow(app="web-app", seed="standard", as_="harman-admin")
    def other(s: eks.Session):
        s.ui.nav("/b")
"""


@pytest.fixture(autouse=True)
def registry() -> Iterator[Registry]:
    chosen = default_registry()
    state = chosen.snapshot()
    try:
        yield chosen
    finally:
        chosen.restore(state)


def copy_tree(target: Path) -> Path:
    shutil.copytree(FIXTURES, target, ignore=shutil.ignore_patterns("__pycache__"))
    return target.resolve()


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    return copy_tree(tmp_path / "tree")


def plan_json(tree: Path, spec: str, **params: str) -> dict[str, Any]:
    plan = plan_spec(spec, params=params, cwd=tree)
    return plan.model_dump(mode="json", by_alias=True, exclude_none=True)


def test_build_from_a_spec_with_params_and_platform(client: TestClient, tree: Path) -> None:
    response = client.post("/api/plans", json={"spec": ".harness/flows/subeler.py", "cwd": str(tree),
                                               "params": {"branch": "Moda", "rows": "3"}})
    assert response.status_code == 200, response.text
    built = response.json()
    assert built["tree"] == str(tree)
    assert built["plan"]["name"] == "subeler" and built["plan"]["params"] == {"branch": "Moda", "rows": 3}
    assert built["validation"] == {"ok": True, "hash": built["plan"]["hash"], "issues": []}
    expected = plan_json(tree, ".harness/flows/subeler.py", branch="Moda", rows="3")
    assert built["plan"]["hash"] == expected["hash"]

    mobile = client.post("/api/plans", json={"spec": "mobile-app/.harness/flows/tapper.py", "tree": str(tree),
                                             "cwd": str(tree), "platform": "android"}).json()
    assert mobile["plan"]["session"]["platform"] == "android"
    named = client.post("/api/plans", json={"spec": ".harness/flows/kasa.py", "entry": "iade",
                                            "cwd": str(tree)}).json()
    assert named["plan"]["name"] == "iade"
    absolute = client.post("/api/plans", json={"spec": str(tree / ".harness/flows/kasa.py::kasa")}).json()
    assert absolute["plan"]["name"] == "kasa" and absolute["tree"] == str(tree)

    broken = client.post("/api/plans", json={"spec": ".harness/flows/broken.py", "cwd": str(tree)}).json()
    assert broken["validation"]["ok"] is False
    assert [issue["code"] for issue in broken["validation"]["issues"]] == ["persona.unknown"]


def test_inline_sources_and_build_errors(client: TestClient, tree: Path) -> None:
    source = textwrap.dedent(INLINE)
    response = client.post("/api/plans", json={"source": source, "entry": "inline", "tree": str(tree),
                                               "params": {"page": "/kasa"}})
    assert response.status_code == 200, response.text
    built = response.json()
    assert built["plan"]["name"] == "inline" and built["validation"]["ok"] is True
    assert built["plan"]["params"] == {"page": "/kasa"}
    assert not list((tree / ".harness" / "flows").glob("_eks_inline_*"))

    nested = client.post("/api/plans", json={"spec": ".harness/flows/raises.py", "cwd": str(tree)})
    assert nested.status_code == 400
    payload = nested.json()
    assert payload["error"] == "plan_build" and "cannot be nested" in payload["message"]
    assert payload["where"].startswith(".harness/flows/raises.py:")
    ambiguous = client.post("/api/plans", json={"spec": "mobile-app/.harness/flows/tapper.py", "cwd": str(tree)})
    assert ambiguous.status_code == 400 and "does not name one platform" in ambiguous.json()["message"]
    neither = client.post("/api/plans", json={"tree": str(tree)})
    assert neither.status_code == 400 and neither.json()["error"] == "plan_build"
    outside = client.post("/api/plans", json={"spec": str(tree / ".harness/flows/kasa.py"),
                                              "tree": str(tree / "web-app")})
    assert outside.status_code == 400 and "is not inside the tree" in outside.json()["message"]


def test_validate_reports_issues_and_unparsable_plans(client: TestClient, tree: Path) -> None:
    good = plan_json(tree, ".harness/flows/subeler.py")
    response = client.post("/api/plans/validate", json={"plan": good, "tree": str(tree)})
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "hash": good["hash"], "issues": []}

    broken = plan_json(tree, ".harness/flows/broken.py")
    checked = client.post("/api/plans/validate", json={"plan": broken, "tree": str(tree)}).json()
    assert checked["ok"] is False and checked["hash"] == broken["hash"]
    assert [issue["code"] for issue in checked["issues"]] == ["persona.unknown"]

    tampered = plan_json(tree, ".harness/flows/subeler.py")
    tampered["nodes"][0]["args"]["path"] = "/api/other"
    edited = client.post("/api/plans/validate", json={"plan": tampered, "tree": str(tree)}).json()
    assert edited["ok"] is False and "plan.hash" in [issue["code"] for issue in edited["issues"]]

    unparsable = client.post("/api/plans/validate", json={"plan": {"schema": "eks/plan@1", "hash": "sha256:x",
                                                                   "nodes": "many"}}).json()
    assert unparsable["ok"] is False and unparsable["hash"] == "sha256:x"
    (issue,) = unparsable["issues"]
    assert issue["code"] == "plan.json" and issue["severity"] == "error"
    assert "does not parse as an eks/plan@1 plan" in issue["message"]

    missing = client.post("/api/plans/validate", json={"plan": good, "tree": str(tree / "nowhere")})
    assert missing.status_code == 400 and missing.json()["error"] == "bad_tree"
    relative = client.post("/api/plans/validate", json={"plan": good, "tree": "relative/tree"})
    assert relative.status_code == 400


def test_tree_authorization(auth_env: AuthEnv, tmp_path: Path) -> None:
    tree = copy_tree(tmp_path / "tree")
    bare = tmp_path / "bare"
    shutil.copytree(FIXTURES, bare, ignore=shutil.ignore_patterns("__pycache__", "project.toml"))
    client = auth_env.client
    spec = {"spec": ".harness/flows/subeler.py", "cwd": str(tree)}
    assert client.post("/api/plans", json=spec, headers=auth_env.admin_headers).status_code == 200

    _stranger, stranger_key = auth_env.make_user("stranger")
    stranger = auth_env.headers_for(stranger_key)
    assert client.post("/api/plans", json=spec, headers=stranger).status_code == 404
    good = plan_json(tree, ".harness/flows/subeler.py")
    assert client.post("/api/plans/validate", json={"plan": good, "tree": str(tree)},
                       headers=stranger).status_code == 404
    untreed = client.post("/api/plans/validate", json={"plan": good}, headers=stranger)
    assert untreed.status_code == 200

    viewer, viewer_key = auth_env.make_user("viewer")
    auth_env.grant(viewer, PROJECT, "viewer")
    watching = auth_env.headers_for(viewer_key)
    assert client.post("/api/plans", json=spec, headers=watching).status_code == 200
    assert client.post("/api/plans/validate", json={"plan": good, "tree": str(tree)},
                       headers=watching).json()["ok"] is True
    inline = client.post("/api/plans", json={"source": textwrap.dedent(INLINE), "entry": "inline",
                                             "tree": str(tree)}, headers=watching)
    assert inline.status_code == 403 and inline.json()["error"] == "admin_required"
    unowned = client.post("/api/plans", json={"spec": ".harness/flows/subeler.py", "cwd": str(bare)},
                          headers=watching)
    assert unowned.status_code == 403 and unowned.json()["error"] == "admin_required"
    loose = client.post("/api/plans", json={"spec": "nowhere/flow.py", "cwd": str(tmp_path)}, headers=watching)
    assert loose.status_code == 403
    admin_inline = client.post("/api/plans", json={"source": textwrap.dedent(INLINE), "entry": "other",
                                                   "tree": str(tree)}, headers=auth_env.admin_headers)
    assert admin_inline.status_code == 200 and admin_inline.json()["plan"]["name"] == "other"
