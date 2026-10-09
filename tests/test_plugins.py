from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from eks_harness.config import Config
from eks_harness.plugins import ManifestError, PluginError, PluginHost, load_manifest, parse_git_spec
from eks_harness.plugins.manifest import parse_manifest
from eks_harness.plugins.trust import content_hash


def write_plugin(root: Path, plugin_id: str, body: str = "", *, module: str | None = None,
                 code: str = "", extra: str = "") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "harness-plugin.toml").write_text(textwrap.dedent(f"""
        [plugin]
        id = "{plugin_id}"
        name = "Test {plugin_id}"
        version = "1.0.0"
        api = "1"
        {extra}
    """) + textwrap.dedent(body), encoding="utf-8")
    if module:
        (root / f"{module}.py").write_text(textwrap.dedent(code), encoding="utf-8")
    return root


SEEDER = """
    [[contributes.seeder]]
    id = "scenario"
    entry = "{module}:Scenario"

    [config]
    count = {{ type = "int", default = 2 }}
    label = "base"
"""

SEEDER_CODE = """
    class Scenario:
        def __init__(self, context):
            self.context = context

        def scenarios(self):
            return [f"{self.context.settings['label']}-{self.context.settings['count']}"]
"""


def make_host(config: Config, tmp_path: Path, **kw) -> PluginHost:
    builtin = tmp_path / "builtin"
    builtin.mkdir(exist_ok=True)
    return PluginHost(config.paths, config, builtin_root=builtin, include_packages=False, **kw)


def test_manifest_parsing_and_errors(tmp_path: Path) -> None:
    root = write_plugin(tmp_path / "p", "acme.backend", SEEDER.format(module="acme_seed") + """
        [[contributes.ui]]
        slot = "backend.inspector"
        module = "ui/dist/panel.js"

        [[contributes.skill]]
        path = "skills/acme"
    """)
    manifest = load_manifest(root)
    assert manifest.id == "acme.backend" and manifest.api == "1"
    assert [c.type for c in manifest.contributions] == ["seeder", "ui", "skill"]
    assert manifest.of_type("ui")[0].id == "backend.inspector:panel"
    assert manifest.defaults() == {"count": 2, "label": "base"}
    for bad, message in [
        ({"plugin": {"id": "Bad Id"}}, "plugin.id"),
        ({"plugin": {"id": "a.b", "api": "2"}}, "not supported"),
        ({"plugin": {"id": "a.b"}, "contributes": {"nope": [{"id": "x"}]}}, "unknown contribution"),
        ({"plugin": {"id": "a.b"}, "contributes": {"backend": [{"id": "x", "entry": "nocolon"}]}}, "entry"),
        ({"plugin": {"id": "a.b"}, "contributes": {"ui": [{"slot": "nowhere", "module": "x.js"}]}}, "slot"),
        ({"plugin": {"id": "a.b"}, "contributes": {"effect": [{"id": "x", "entry": "m:a"},
                                                              {"id": "x", "entry": "m:b"}]}}, "duplicate"),
    ]:
        with pytest.raises(ManifestError, match=message):
            parse_manifest(bad, tmp_path)


def test_builtin_trusted_and_repo_pending_until_approved(config: Config, tmp_path: Path) -> None:
    host = make_host(config, tmp_path)
    write_plugin(tmp_path / "builtin" / "core", "eks.core")
    tree = tmp_path / "repo"
    plugin = write_plugin(tree / ".harness" / "plugins" / "acme", "acme.seed", SEEDER.format(module="acme_seed_a"),
                          module="acme_seed_a", code=SEEDER_CODE)
    states = {r.id: r.state for r in host.records(tree)}
    assert states == {"eks.core": "active", "acme.seed": "pending"}
    assert host.contributions("seeder", tree) == []
    host.trust("acme.seed", tree)
    assert host.get("acme.seed", tree).state == "active"
    seeder = host.load(host.contribution("seeder", "scenario", tree), tree)
    assert seeder.scenarios() == ["base-2"]
    (plugin / "acme_seed_a.py").write_text("raise SystemExit\n", encoding="utf-8")
    host.refresh()
    record = host.get("acme.seed", tree)
    assert record.state == "changed" and record.approved_hash != record.digest
    assert host.records() == [r for r in host.records() if r.id == "eks.core"]


def test_project_config_pretrust_settings_and_enablement(config: Config, tmp_path: Path) -> None:
    tree = tmp_path / "repo"
    write_plugin(tree / ".harness" / "plugins" / "acme", "acme.seed", SEEDER.format(module="acme_seed_b"),
                 module="acme_seed_b", code=SEEDER_CODE)
    write_plugin(tmp_path / "builtin" / "blender", "eks.blender", extra="enabled_by_default = false")
    (tree / ".harness" / "project.toml").write_text(textwrap.dedent("""
        [project]
        id = "acme/web-app"

        [plugins]
        trust = ["acme"]
        enable = ["eks.blender"]

        [plugins.settings."acme.seed"]
        count = "5"
    """), encoding="utf-8")
    config.set("plugins.settings", '{"acme.seed": {"label": "user", "count": 3}}')
    host = make_host(config, tmp_path)
    assert host.get("eks.blender").state == "disabled"
    assert host.get("eks.blender", tree).state == "active"
    assert host.project(tree).project_id == "acme/web-app"
    assert host.settings("acme.seed", tree) == {"count": 5, "label": "user"}
    seeder = host.load(host.contribution("seeder", "acme.seed:scenario", tree), tree)
    assert seeder.scenarios() == ["user-5"]
    config.set("plugins.disabled", '["acme.seed"]')
    assert host.get("acme.seed", tree).state == "disabled"


def test_path_plugins_override_builtin_by_id(config: Config, tmp_path: Path) -> None:
    write_plugin(tmp_path / "builtin" / "fx", "eks.fx")
    local = write_plugin(tmp_path / "dev" / "fx", "eks.fx")
    config.set("plugins.paths", f'["{tmp_path / "dev"}"]')
    host = make_host(config, tmp_path)
    states = [(r.kind, r.state) for r in host.records()]
    assert states == [("builtin", "shadowed"), ("path", "pending")]
    host.trust("eks.fx")
    assert host.get("eks.fx").root == local.resolve() and host.get("eks.fx").active
    with pytest.raises(PluginError):
        host.contribution("seeder", "x")


def test_missing_binaries_and_bad_manifests_surface_as_errors(config: Config, tmp_path: Path) -> None:
    write_plugin(tmp_path / "builtin" / "needs", "eks.needs", extra="")
    (tmp_path / "builtin" / "needs" / "harness-plugin.toml").write_text(
        '[plugin]\nid = "eks.needs"\n[requires]\nbin = ["definitely-not-a-binary-xyz"]\n', encoding="utf-8")
    (tmp_path / "builtin" / "broken").mkdir(parents=True)
    (tmp_path / "builtin" / "broken" / "harness-plugin.toml").write_text("[plugin\n", encoding="utf-8")
    host = make_host(config, tmp_path)
    by_origin = {r.candidate.origin: r for r in host.records()}
    assert by_origin["builtin:needs"].state == "error" and "definitely" in by_origin["builtin:needs"].reason
    assert by_origin["builtin:broken"].state == "error"


def test_content_hash_ignores_caches(tmp_path: Path) -> None:
    root = write_plugin(tmp_path / "p", "a.b")
    first = content_hash(root)
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "x.pyc").write_bytes(b"1")
    assert content_hash(root) == first
    (root / "code.py").write_text("x = 1\n", encoding="utf-8")
    assert content_hash(root) != first


def test_git_spec_parsing() -> None:
    spec = parse_git_spec("github:acme/plugins#backends/api@v2")
    assert spec.url == "https://github.com/acme/plugins.git" and spec.subpath == "backends/api" and spec.ref == "v2"
    assert parse_git_spec("git@github.com:acme/x.git").url == "git@github.com:acme/x.git"
    with pytest.raises(ValueError):
        parse_git_spec("github:acme/x#../../etc")


def test_plugin_api_lists_trusts_and_serves_ui(client, ctx, tmp_path: Path) -> None:
    tree = tmp_path / "repo"
    plugin = write_plugin(tree / ".harness" / "plugins" / "acme", "acme.ui", """
        [[contributes.ui]]
        slot = "project.tab"
        module = "ui/dist/tab.js"
    """)
    (plugin / "ui" / "dist").mkdir(parents=True)
    (plugin / "ui" / "dist" / "tab.js").write_text("export default {}\n", encoding="utf-8")
    (plugin / "secret.txt").write_text("nope", encoding="utf-8")
    (tree / ".harness" / "project.toml").write_text('[project]\nid = "acme/web"\n', encoding="utf-8")
    listed = client.get("/api/plugins", params={"tree": str(tree)}).json()
    states = {i["id"]: i["state"] for i in listed["items"]}
    assert states["acme.ui"] == "pending"
    assert client.get("/api/plugins/ui", params={"project": "acme/web"}).json()["items"] == []
    trusted = client.post("/api/plugins/acme.ui/trust", json={"project": "acme/web"})
    assert trusted.status_code == 200 and trusted.json()["plugin"]["state"] == "active"
    modules = client.get("/api/plugins/ui", params={"project": "acme/web"}).json()["items"]
    assert [m["slot"] for m in modules] == ["project.tab"]
    served = client.get(modules[0]["url"])
    assert served.status_code == 200 and "export default" in served.text
    assert client.get("/api/plugins/acme.ui/files/secret.txt", params={"project": "acme/web"}).status_code == 404
    assert client.get("/api/plugins/acme.ui/files/../../etc/passwd",
                      params={"project": "acme/web"}).status_code == 404
    assert client.delete("/api/plugins/acme.ui/trust", params={"project": "acme/web"}).json()["removed"] == 1
    assert client.get("/api/plugins/nope.plugin").status_code == 404


@pytest.mark.parametrize("kind", ["api", "backend", "cli", "credentials", "driver", "mcp-tools", "node-capability",
                                  "seeder", "skill", "ui"])
def test_scaffold_templates_produce_valid_plugins(kind: str, tmp_path: Path) -> None:
    from eks_harness.cli.plugin_cmds import validate_path
    from eks_harness.plugins.scaffold import scaffold

    target = tmp_path / kind
    written = scaffold(kind, target, f"acme.{kind.replace('-', '')}")
    assert written and (target / "harness-plugin.toml").is_file()
    results = validate_path(target)
    assert results[0]["id"] == f"acme.{kind.replace('-', '')}"
    if kind != "ui":
        assert results[0]["ok"], results
    for path in written:
        assert "{{" not in path.read_text(encoding="utf-8")
        if path.suffix == ".py":
            compile(path.read_text(encoding="utf-8"), str(path), "exec")


def test_scaffolded_seeder_loads_through_host(config: Config, tmp_path: Path) -> None:
    from eks_harness.plugins.scaffold import scaffold

    scaffold("seeder", tmp_path / "plugins" / "seed", "acme.seedkit")
    config.set("plugins.paths", [str(tmp_path / "plugins")])
    host = make_host(config, tmp_path)
    host.trust("acme.seedkit")
    seeder = host.load(host.contribution("seeder", "seedkit"))
    assert seeder.scenarios() == ["standard"]


def test_plugin_cli_new_and_validate(tmp_path: Path, capsys) -> None:
    from eks_harness.cli import main

    assert main(["plugin", "new", "backend", "acme.api", str(tmp_path / "api"), "--json"]) == 0
    assert main(["plugin", "validate", str(tmp_path / "api"), "--json"]) == 0
    assert '"ok": true' in capsys.readouterr().out
    assert main(["plugin", "validate", str(tmp_path / "missing")]) != 0
