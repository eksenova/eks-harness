from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from eks_harness.drivers.client import WorkerClient
from eks_harness.drivers.workers import WorkerHandle
from eks_harness.flows import FlowError, FlowRequest, format_report, run_flow
from eks_harness.plugins import PluginHost

from conftest import harness_client
from fake_worker import FakeWorker


class FakeManager:
    def __init__(self, worker: FakeWorker) -> None:
        self.worker = worker
        self.configs: list[tuple[str, str, dict]] = []
        self.stopped: list[tuple[str, str]] = []

    def ensure(self, kind: str, key: str, config: dict) -> WorkerHandle:
        self.configs.append((kind, key, config))
        port = int(self.worker.url.rsplit(":", 1)[1])
        return WorkerHandle(kind=kind, key=key, pid=1, port=port, state_dir=Path("/tmp"), log_file=Path("/tmp/x"),
                            code="c", config_hash="h", started_at=0.0)

    def stop(self, kind: str, key: str) -> bool:
        self.stopped.append((kind, key))
        return True


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def make_tree(tmp_path: Path) -> Path:
    tree = tmp_path / "repo"
    write(tree / ".harness/project.toml", '[project]\nid = "acme/web-app"\n[plugins]\ntrust = ["acme"]\n')
    write(tree / "web/.harness/app.toml", """
        [app]
        platform = "web"
        [web]
        url = "http://localhost:3000"
    """)
    write(tree / "web/.harness/app.py", """
        from eks_harness.flows import WebApp

        class App(WebApp):
            booted = False

            def boot(self):
                type(self).booted = True

            def open_settings(self):
                return self.click("Settings")
    """)
    write(tree / ".harness/plugins/acme/harness-plugin.toml", """
        [plugin]
        id = "acme.app"

        [[contributes.flow_helpers]]
        id = "menus"
        entry = "acme_menus:Menus"
        platform = "web"

        [[contributes.driver_extension]]
        path = "auth.mjs"
        platform = "web"
    """)
    write(tree / ".harness/plugins/acme/acme_menus.py", """
        class Menus:
            def menu(self, label):
                return self.click({"role": "menuitem", "name": label})
    """)
    write(tree / ".harness/plugins/acme/auth.mjs", "export default () => ({})\n")
    return tree


@pytest.fixture
def daemon(client, config):
    return harness_client(client, config)


def test_flow_runs_against_a_lease_with_app_class_helpers_and_extensions(tmp_path, daemon, config) -> None:
    tree = make_tree(tmp_path)
    flow = write(tree / "web/.harness/flows/settings-check.py", """
        def flow(app):
            app.goto("/")
            app.open_settings()
            app.menu("Profile")
            app.caption("Profile page", step=1)
            app.checkpoint("profile")
            return {"text": app.text("h1"), "params": app.params}
    """)
    answers = {"goto": "http://localhost:3000/", "click": True, "annotate.caption": "caption", "text": "Profile",
               "shot": [{"viewport": "desktop", "id": "a1", "url": "u", "rawUrl": "r", "sessionUrl": "s"}],
               "<script>": {"url": "/profile", "issues": []}}
    with FakeWorker(answers) as fake:
        manager = FakeManager(fake)
        report = run_flow(FlowRequest(flow=flow, params={"who": "ada"}, fetch=False, instance="test:1"),
                          client=daemon, paths=config.paths, config=config,
                          host=PluginHost(config.paths, config), manager=manager)
    assert report["ok"], report
    assert report["value"] == {"text": "Profile", "params": {"who": "ada"}}
    assert report["project"] == "acme/web-app" and report["platform"] == "web"
    assert [c[0] for c in fake.calls[:4]] == ["goto", "click", "click", "annotate.caption"]
    assert fake.calls[2][1] == [{"role": "menuitem", "name": "Profile"}]
    assert report["checkpoints"][0]["label"] == "profile" and report["checkpoints"][0]["shots"] == ["profile-desktop"]
    kind, key, worker_config = manager.configs[0]
    assert kind == "web" and key == report["sid"]
    assert worker_config["baseUrl"] == "http://localhost:3000"
    assert [e["id"] for e in worker_config["extensions"]] == ["acme.app:auth.mjs"]
    text = format_report(report, "settings-check")
    assert "flow settings-check: ok" in text and "checkpoint profile" in text


def test_flow_failures_are_reported_with_checks_and_lease_release(tmp_path, daemon, config) -> None:
    tree = make_tree(tmp_path)
    flow = write(tree / "web/.harness/flows/broken.py", """
        def flow(app):
            app.click("Missing button")
    """)

    def missing(args):
        raise RuntimeError("nothing matches 'Missing button'")

    answers = {"click": missing, "shot": [], "<script>": {"issues": ["page scrolls horizontally"]}}
    with FakeWorker(answers) as fake:
        manager = FakeManager(fake)
        report = run_flow(FlowRequest(flow=flow, fetch=False, release=True, instance="test:2"), client=daemon,
                          paths=config.paths, config=config, host=PluginHost(config.paths, config), manager=manager)
    assert not report["ok"]
    assert report["failure"]["step"] == "click" and "Missing button" in report["failure"]["error"]
    assert report["failure"]["issues"] == ["page scrolls horizontally"]
    assert report["released"] and manager.stopped == [("web", report["sid"])]


def test_inline_code_and_missing_profiles(tmp_path, daemon, config) -> None:
    tree = make_tree(tmp_path)
    with FakeWorker({"url": "http://localhost:3000/x"}) as fake:
        report = run_flow(FlowRequest(code="result = app.url()", cwd=tree / "web", fetch=False, instance="t:3"),
                          client=daemon, paths=config.paths, config=config, host=PluginHost(config.paths, config),
                          manager=FakeManager(fake))
    assert report["value"] == "http://localhost:3000/x"
    lonely = tmp_path / "lonely"
    lonely.mkdir()
    with pytest.raises(FlowError, match="cannot tell which app"):
        run_flow(FlowRequest(code="pass", cwd=lonely, fetch=False, instance="t:4"), client=daemon, paths=config.paths,
                 config=config, host=PluginHost(config.paths, config))
    assert WorkerClient is not None
