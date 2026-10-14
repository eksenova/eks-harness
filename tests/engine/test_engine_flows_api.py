from __future__ import annotations

import hashlib
import shutil
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import AuthEnv
from fastapi.testclient import TestClient

from eks_harness.api.routes_flows import MAX_SOURCE_BYTES
from eks_harness.ir.registry import Registry, default_registry

FIXTURES = Path(__file__).resolve().parent.parent / "eks" / "cli_fixtures"
PROJECT = "acme/cli"

FRESH = """
    import eks


    @eks.flow(app="web-app", seed="standard", as_="harman-admin", tags=["yeni"], description="A fresh flow")
    def fresh(s: eks.Session, page: str = "/a"):
        s.ui.nav(page)


    @eks.flow(app="web-app", seed="standard", as_="harman-admin")
    def needs(s: eks.Session, page: str):
        s.ui.nav(page)
"""

TAPPER = """
    import eks
    from eks import by


    @eks.flow(app="mobile-app")
    def tapper(s: eks.Session):
        s.ui.tap(by.test_id("kaydet"))
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
    (target / ".harness" / "flows" / "syntax.py").write_text("import eks\n\n\ndef (:\n", encoding="utf-8")
    return target.resolve()


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    return copy_tree(tmp_path / "tree")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def save(client: TestClient, tree: Path, path: str, source: str, status: int = 200,
         **extra: Any) -> dict[str, Any]:
    response = client.put("/api/flows/source", json={"tree": str(tree), "path": path, "source": source, **extra})
    assert response.status_code == status, response.text
    return response.json()


def test_listing_shows_files_entries_and_load_errors(client: TestClient, tree: Path) -> None:
    response = client.get("/api/flows", params={"tree": str(tree)})
    assert response.status_code == 200, response.text
    listing = response.json()
    assert listing["tree"] == str(tree)
    files = {item["path"]: item for item in listing["files"]}
    assert ".harness/flows/subeler.py" in files and "mobile-app/.harness/flows/tapper.py" in files
    subeler = files[".harness/flows/subeler.py"]
    (entry,) = subeler["entries"]
    assert subeler["kind"] == "flow" and subeler["error"] is None
    assert entry["name"] == "subeler" and entry["app"] == "web-app" and entry["platforms"] == ["web"]
    assert entry["tags"] == ["subeler"]
    assert entry["params"] == [{"name": "branch", "annotation": "str", "default": "Kadıköy"},
                               {"name": "rows", "annotation": "int", "default": 1}]
    assert sorted(item["name"] for item in files[".harness/flows/kasa.py"]["entries"]) == ["iade", "kasa"]
    assert files["mobile-app/.harness/flows/tapper.py"]["entries"][0]["platforms"] == ["ios", "android"]
    broken = files[".harness/flows/syntax.py"]
    assert broken["entries"] == [] and "invalid syntax" in broken["error"]
    edits = client.get("/api/flows", params={"tree": str(tree), "kind": "edit"}).json()
    assert [item["path"] for item in edits["files"]] == [".harness/edits/demo.py"]
    unknown = client.get("/api/flows", params={"tree": str(tree), "kind": "movie"})
    assert unknown.status_code == 400
    missing = client.get("/api/flows", params={"tree": str(tree / "nowhere")})
    assert missing.status_code == 400 and missing.json()["error"] == "bad_tree"


def test_read_save_and_delete_with_sha_checks(client: TestClient, tree: Path) -> None:
    path = ".harness/flows/fresh.py"
    assert client.get("/api/flows/source", params={"tree": str(tree), "path": path}).status_code == 404
    source = textwrap.dedent(FRESH)
    saved = save(client, tree, path, source)
    assert saved["path"] == path and saved["sha256"] == sha(source) and saved["error"] is None
    assert sorted(entry["name"] for entry in saved["entries"]) == ["fresh", "needs"]
    first = next(entry for entry in saved["entries"] if entry["name"] == "fresh")
    assert first["description"] == "A fresh flow" and first["tags"] == ["yeni"]
    validations = saved["validations"]
    assert set(validations) == {"flow:fresh", "flow:needs"}
    assert validations["flow:fresh"]["ok"] is True and validations["flow:fresh"]["hash"].startswith("sha256:")
    assert validations["flow:needs"]["ok"] is False
    (issue,) = validations["flow:needs"]["issues"]
    assert issue["code"] == "plan_build" and "page" in issue["message"]

    read = client.get("/api/flows/source", params={"tree": str(tree), "path": path}).json()
    assert read == {"tree": str(tree), "path": path, "source": source, "sha256": sha(source)}

    changed = source.replace('page: str = "/a"', 'page: str = "/b"')
    stale = save(client, tree, path, changed, status=409, expectSha256=sha("something else"))
    assert stale["error"] == "sha_mismatch" and stale["current"] == sha(source)
    assert (tree / path).read_text(encoding="utf-8") == source
    created_twice = save(client, tree, ".harness/flows/other.py", changed, status=409, expectSha256=sha(source))
    assert created_twice["current"] is None
    updated = save(client, tree, path, changed, expectSha256="sha256:" + sha(source), validate=False)
    assert updated["validations"] == {} and sorted(entry["name"] for entry in updated["entries"]) == ["fresh", "needs"]
    planned = client.post("/api/plans", json={"spec": f"{path}::fresh", "cwd": str(tree)}).json()
    assert planned["plan"]["params"] == {"page": "/b"}

    broken = save(client, tree, path, "import eks\n\nraise RuntimeError('boom')\n")
    assert broken["entries"] == [] and "RuntimeError: boom" in broken["error"]

    removed = client.delete("/api/flows/source", params={"tree": str(tree), "path": path})
    assert removed.status_code == 200
    assert removed.json() == {"path": path, "sha256": sha("import eks\n\nraise RuntimeError('boom')\n"),
                              "entries": [], "validations": {}, "error": None}
    assert not (tree / path).exists()
    assert client.delete("/api/flows/source", params={"tree": str(tree), "path": path}).status_code == 404
    missing_plan = client.post("/api/plans", json={"spec": f"{path}::fresh", "cwd": str(tree)})
    assert missing_plan.status_code == 400


def test_matrix_flows_validate_per_platform(client: TestClient, tree: Path) -> None:
    saved = save(client, tree, "mobile-app/.harness/flows/tapper.py", textwrap.dedent(TAPPER))
    assert set(saved["validations"]) == {"flow:tapper@ios", "flow:tapper@android"}
    assert all(item["ok"] for item in saved["validations"].values())
    hashes = {item["hash"] for item in saved["validations"].values()}
    assert len(hashes) == 2
    seed = client.get("/api/flows/source", params={"tree": str(tree), "path": ".harness/seeds/standard.py"}).json()
    resaved = save(client, tree, ".harness/seeds/standard.py", seed["source"], expectSha256=seed["sha256"])
    assert set(resaved["validations"]) == {"seed:standard"} and resaved["validations"]["seed:standard"]["ok"]


@pytest.mark.parametrize("path", [
    "../outside.py",
    ".harness/flows/../plugins/x.py",
    "/etc/passwd.py",
    "C:/flows/x.py",
    ".harness\\flows\\x.py",
    "src/x.py",
    ".harness/plugins/fixtures/fixture_backends.py",
    ".harness/flows/notes.txt",
    ".harness/flows/.py",
    ".harness/flows/sub/x.py",
    "web-app/.harness/seeds/x.py",
    ".harness/flows/_eks_inline_0123456789abcdef.py",
    ".harness/project.py",
    "",
])
def test_path_rules(client: TestClient, tree: Path, path: str) -> None:
    for response in (
        client.get("/api/flows/source", params={"tree": str(tree), "path": path}),
        client.put("/api/flows/source", json={"tree": str(tree), "path": path, "source": "x = 1\n"}),
        client.delete("/api/flows/source", params={"tree": str(tree), "path": path}),
    ):
        assert response.status_code == 400, (path, response.text)
        assert response.json()["error"] == "bad_path"


def test_size_limit_and_symlinks(client: TestClient, tree: Path, tmp_path: Path) -> None:
    too_big = client.put("/api/flows/source", json={"tree": str(tree), "path": ".harness/flows/big.py",
                                                    "source": "#" * (MAX_SOURCE_BYTES + 1)})
    assert too_big.status_code == 413
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "flow.py").write_text("x = 1\n", encoding="utf-8")
    (tree / ".harness" / "scores").mkdir(parents=True, exist_ok=True)
    (tree / ".harness" / "scores" / "linked.py").symlink_to(outside / "flow.py")
    linked = client.get("/api/flows/source", params={"tree": str(tree), "path": ".harness/scores/linked.py"})
    assert linked.status_code == 400 and "leaves the tree" in linked.json()["message"]


def test_flow_routes_need_access(auth_env: AuthEnv, tmp_path: Path) -> None:
    tree = copy_tree(tmp_path / "tree")
    client = auth_env.client
    viewer, viewer_key = auth_env.make_user("viewer")
    auth_env.grant(viewer, PROJECT, "viewer")
    watching = auth_env.headers_for(viewer_key)
    assert client.get("/api/flows", params={"tree": str(tree)}, headers=watching).status_code == 200
    assert client.get("/api/flows/source", params={"tree": str(tree), "path": ".harness/flows/kasa.py"},
                      headers=watching).status_code == 200
    denied = client.put("/api/flows/source", json={"tree": str(tree), "path": ".harness/flows/x.py",
                                                   "source": "x = 1\n"}, headers=watching)
    assert denied.status_code == 403
    assert client.delete("/api/flows/source", params={"tree": str(tree), "path": ".harness/flows/kasa.py"},
                         headers=watching).status_code == 403
    editor, editor_key = auth_env.make_user("editor")
    auth_env.grant(editor, PROJECT, "editor")
    allowed = client.put("/api/flows/source", json={"tree": str(tree), "path": ".harness/flows/x.py",
                                                    "source": "x = 1\n"}, headers=auth_env.headers_for(editor_key))
    assert allowed.status_code == 200 and allowed.json()["entries"] == []
    _stranger, stranger_key = auth_env.make_user("stranger")
    assert client.get("/api/flows", params={"tree": str(tree)},
                      headers=auth_env.headers_for(stranger_key)).status_code == 404
