from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from eks.discovery import tree_registry
from eks_harness.cli import main
from eks_harness.cli.ui import EXIT_NOT_FOUND, EXIT_USAGE
from eks_harness.ir.registry import Registry

SUMMARY_KEYS = {
    "name", "family", "version", "out", "description", "location", "executor", "pure", "idempotent",
    "deviceDependent", "ordering",
    "platforms", "resources", "container", "branches", "exportsBody", "pinnable", "recordingOnly", "bounded", "plugin",
    "pluginVersion",
}

MANIFEST = """
    [plugin]
    id = "acme"
    name = "Acme"
    version = "1.2.0"
    api = "1"
    modules = ["acme_ops"]
"""

OPS = """
    from typing import Literal

    import eks


    @eks.op("acme", out="json", resources=("lease",), platforms=("web",), idempotent=True,
            description="Ping the acme widget [beta]")
    class Ping(eks.Args):
        target: str = "self"
        mode: Literal["fast", "slow"] = "fast"
        tags: list[str] = []
"""


def write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return path


@pytest.fixture
def shop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    tree = tmp_path / "shop"
    write(tree / ".harness" / "project.toml", '[project]\nid = "acme/shop"\n\n[plugins]\ntrust = ["acme"]\n')
    write(tree / ".harness" / "plugins" / "acme" / "harness-plugin.toml", MANIFEST)
    write(tree / ".harness" / "plugins" / "acme" / "acme_ops.py", OPS)
    monkeypatch.chdir(tree)
    monkeypatch.setenv("EKS_HARNESS_NO_PLUGIN_CLI", "1")
    return tree


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(["ops", *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def run_json(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict]:
    code, out, _err = run(capsys, *argv, "--json")
    return code, json.loads(out)


def test_list_shows_builtin_and_plugin_ops(
    shop: Path, capsys: pytest.CaptureFixture[str], eks_registry: Registry
) -> None:
    code, out, err = run(capsys)
    assert code == 0, err
    for name in ("ui.tap", "ui.nav", "api.get", "ctl.when", "edit.render", "acme.ping"):
        assert name in out
    ping = next(line for line in out.splitlines() if "acme.ping" in line)
    assert "acme 1.2.0" in ping
    assert "web" in ping
    assert "idempotent" in ping
    assert "Ping the acme widget [beta]" in ping
    tap = next(line for line in out.splitlines() if line.split() and line.split()[0] == "ui.tap")
    assert "android, ios, web" in tap
    assert "pinnable" in tap
    assert f"{len(tree_registry(shop).ops())} ops" in out


def test_list_json_has_one_summary_per_op(
    shop: Path, capsys: pytest.CaptureFixture[str], eks_registry: Registry
) -> None:
    code, data = run_json(capsys)
    assert code == 0
    assert set(data) == {"ops"}
    names = [item["name"] for item in data["ops"]]
    assert names == sorted(names)
    assert names == [spec.name for spec in tree_registry(shop).ops()]
    for item in data["ops"]:
        assert set(item) == SUMMARY_KEYS
    ping = next(item for item in data["ops"] if item["name"] == "acme.ping")
    assert (ping["plugin"], ping["pluginVersion"]) == ("acme", "1.2.0")
    assert ping["family"] == "acme"
    assert ping["out"] == "json"
    assert ping["platforms"] == ["web"]
    assert ping["resources"] == ["lease"]
    assert ping["idempotent"] is True
    tap = next(item for item in data["ops"] if item["name"] == "ui.tap")
    assert tap["plugin"] == "eks"
    assert tap["pinnable"] is True
    when = next(item for item in data["ops"] if item["name"] == "ctl.when")
    assert when["container"] is True
    assert set(when["branches"]) >= {"then", "else"}


def test_no_plugins_lists_only_the_builtins(
    shop: Path, capsys: pytest.CaptureFixture[str], eks_registry: Registry
) -> None:
    code, data = run_json(capsys, "--no-plugins")
    assert code == 0
    names = {item["name"] for item in data["ops"]}
    assert "acme.ping" not in names
    assert "ui.tap" in names
    assert {item["plugin"] for item in data["ops"]} == {"eks"}


def test_family_filters_the_list(shop: Path, capsys: pytest.CaptureFixture[str], eks_registry: Registry) -> None:
    code, data = run_json(capsys, "--family", "ui")
    assert code == 0
    names = [item["name"] for item in data["ops"]]
    assert names
    assert all(name.startswith("ui.") for name in names)
    assert names == [spec.name for spec in eks_registry.ops("ui")]
    code, out, _err = run(capsys, "--family", "acme")
    assert code == 0
    assert "acme.ping" in out
    assert "ui.tap" not in out
    assert "1 op" in out


def test_unknown_family_exits_6(shop: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = run(capsys, "--family", "uii")
    assert code == EXIT_NOT_FOUND
    assert out == ""
    assert "'uii'" in err
    assert "'ui'" in err


def test_detail_json_has_the_summary_and_the_args_schema(shop: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, data = run_json(capsys, "ui.tap")
    assert code == 0
    assert set(data) == SUMMARY_KEYS | {"schema"}
    assert data["name"] == "ui.tap"
    assert data["family"] == "ui"
    schema = data["schema"]
    assert schema["title"] == "ui.tap"
    assert schema["description"] == data["description"] == "Tap or click a target"
    assert "target" in schema["properties"]
    assert schema["required"] == ["target"]
    assert schema["x-eks"] == {key: data[key] for key in SUMMARY_KEYS}


def test_detail_text_shows_the_summary_and_the_arguments(shop: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = run(capsys, "acme.ping")
    assert code == 0, err
    assert "Ping the acme widget [beta]" in out
    assert "acme 1.2.0" in out
    assert "idempotent" in out
    rows = {line.split()[0]: line for line in out.splitlines() if line.split()}
    assert "'fast' | 'slow'" in rows["mode"]
    assert '"fast"' in rows["mode"]
    assert "list[string]" in rows["tags"]
    assert "string" in rows["target"]
    code, out, _err = run(capsys, "ui.tap")
    assert code == 0
    rows = {line.split()[0]: line for line in out.splitlines() if line.split()}
    assert "Target" in rows["target"]
    assert "yes" in rows["target"]
    assert "string (eks-time) | null" in rows["timeout"]


def test_unknown_op_exits_6_with_suggestions(shop: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = run(capsys, "ui.tapp")
    assert code == EXIT_NOT_FOUND
    assert out == ""
    assert "unknown op 'ui.tapp'" in err
    assert "'ui.tap'" in err


def test_unknown_op_in_json_mode_reports_not_found(shop: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _err = run(capsys, "ui.tapp", "--json")
    assert code == EXIT_NOT_FOUND
    data = json.loads(out)
    assert data["error"] == "not_found"
    assert data["exitCode"] == EXIT_NOT_FOUND
    assert "'ui.tap'" in data["message"]


def test_op_of_another_family_is_not_found(shop: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, _out, err = run(capsys, "ui.tap", "--family", "web")
    assert code == EXIT_NOT_FOUND
    assert "family 'ui'" in err


def test_a_broken_plugin_is_a_usage_error(shop: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write(shop / ".harness" / "plugins" / "acme" / "acme_ops.py", "raise RuntimeError('acme is broken')\n")
    code, _out, err = run(capsys)
    assert code == EXIT_USAGE
    assert "acme is broken" in err
    code, data = run_json(capsys, "--no-plugins")
    assert code == 0
    assert "acme.ping" not in {item["name"] for item in data["ops"]}


def test_listing_is_deterministic(shop: Path, capsys: pytest.CaptureFixture[str]) -> None:
    first = run(capsys, "--json")
    second = run(capsys, "--json")
    assert first == second
