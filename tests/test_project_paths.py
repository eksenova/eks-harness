from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

from eks_harness.config import Config
from eks_harness.plugins import PluginHost, load_project_config
from eks_harness.plugins.project import (
    ProjectConfigError,
    expand_placeholders,
    main_tree,
)

GIT = ["git", "-c", "user.email=a@b", "-c", "user.name=a", "-c", "commit.gpgsign=false"]


def git(repo: Path, *args: str) -> None:
    subprocess.run([*GIT, "-C", str(repo), *args], check=True, capture_output=True)


def backend_repo(root: Path, label: str) -> Path:
    plugin = root / ".harness" / "plugins" / "be"
    plugin.mkdir(parents=True)
    (plugin / "harness-plugin.toml").write_text(textwrap.dedent(f"""
        [plugin]
        id = "acme.be"
        name = "Backend {label}"
        version = "1.0.0"
        api = "1"

        [[contributes.seeder]]
        id = "standard"
        entry = "acme_be_seed:Scenario"
    """), encoding="utf-8")
    (plugin / "acme_be_seed.py").write_text(textwrap.dedent(f"""
        class Scenario:
            def scenarios(self):
                return [{label!r}]
    """), encoding="utf-8")
    return plugin


def frontend_repo(root: Path, paths: str, local: str = "") -> Path:
    (root / ".harness").mkdir(parents=True)
    (root / ".harness" / "project.toml").write_text(textwrap.dedent(f"""
        [project]
        id = "acme/frontend"

        [plugins]
        paths = [{paths}]
        trust = ["acme.be"]
    """), encoding="utf-8")
    if local:
        (root / ".harness" / "project.local.toml").write_text(textwrap.dedent(local), encoding="utf-8")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", ".")
    git(root, "commit", "-qm", "base")
    return root


@pytest.fixture
def host(config: Config, tmp_path: Path) -> PluginHost:
    builtin = tmp_path / "builtin"
    builtin.mkdir()
    return PluginHost(config.paths, config, builtin_root=builtin, include_packages=False)


def test_sibling_repo_plugin_resolves_from_every_worktree(host: PluginHost, tmp_path: Path) -> None:
    plugin = backend_repo(tmp_path / "backend", "main-backend")
    front = frontend_repo(tmp_path / "frontend",
                          '"${ACME_BACKEND_TREE:-${main_tree}/../backend}/.harness/plugins/be"')
    worktree = front / ".claude" / "worktrees" / "feature"
    git(front, "worktree", "add", "-q", "-b", "feature/x", str(worktree))
    assert main_tree(worktree) == front.resolve()
    for tree in (front, worktree):
        record = host.get("acme.be", tree)
        assert record.state == "active" and record.root == plugin.resolve(), tree
        assert host.load(host.contribution("seeder", "standard", tree), tree).scenarios() == ["main-backend"]


def test_environment_and_local_vars_pick_another_backend_checkout(host: PluginHost, tmp_path: Path,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    backend_repo(tmp_path / "backend", "main")
    other = backend_repo(tmp_path / "backend-feature", "feature")
    pinned = backend_repo(tmp_path / "backend-pinned", "pinned")
    front = frontend_repo(tmp_path / "frontend", '"${ACME_BACKEND_TREE:-${main_tree}/../backend}/.harness/plugins/be"')
    monkeypatch.setenv("ACME_BACKEND_TREE", str(tmp_path / "backend-feature"))
    assert load_project_config(front).paths == (other.resolve(),)
    (front / ".harness" / "project.local.toml").write_text(
        f'[vars]\nACME_BACKEND_TREE = "{tmp_path / "backend-pinned"}"\n', encoding="utf-8")
    assert load_project_config(front).paths == (pinned.resolve(),)


def test_project_vars_are_defaults_the_environment_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backend_repo(tmp_path / "backend", "main")
    env_plugin = backend_repo(tmp_path / "from-env", "env")
    front = tmp_path / "frontend"
    (front / ".harness").mkdir(parents=True)
    (front / ".harness" / "project.toml").write_text(textwrap.dedent("""
        [vars]
        BACKEND = "${tree}/../backend"

        [plugins]
        paths = ["${BACKEND}/.harness/plugins/be"]
    """), encoding="utf-8")
    assert load_project_config(front).paths == ((tmp_path / "backend" / ".harness" / "plugins" / "be").resolve(),)
    monkeypatch.setenv("BACKEND", str(tmp_path / "from-env"))
    assert load_project_config(front).paths == (env_plugin.resolve(),)


def test_unset_placeholder_surfaces_as_a_plugin_error(host: PluginHost, tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ACME_MISSING_TREE", raising=False)
    front = frontend_repo(tmp_path / "frontend", '"${ACME_MISSING_TREE}/.harness/plugins/be"')
    errors = [r for r in host.records(front) if r.state == "error"]
    assert errors and "${ACME_MISSING_TREE}" in errors[0].candidate.origin


def test_paths_relative_to_harness_dir_keep_working(tmp_path: Path) -> None:
    front = tmp_path / "frontend"
    legacy = backend_repo(front / ".harness" / "vendor", "legacy")
    (front / ".harness" / "project.toml").write_text('[plugins]\npaths = ["vendor/.harness/plugins/be"]\n',
                                                     encoding="utf-8")
    assert load_project_config(front).paths == (legacy.resolve(),)


def test_self_referencing_variables_are_rejected() -> None:
    with pytest.raises(ProjectConfigError, match="nest too deeply"):
        expand_placeholders("${A}", {"A": "${A}"})
