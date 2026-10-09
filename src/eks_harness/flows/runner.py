from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eks_harness.drivers.profile import (
    LEASE_KINDS,
    WORKER_KINDS,
    AppProfile,
    load_profile,
    mobile_worker_config,
    node_modules_dir,
    pace_from_config,
    web_worker_config,
)
from eks_harness.drivers.registry import driver_extensions, flow_helpers
from eks_harness.drivers.workers import WorkerManager
from eks_harness.flows.client import StepError
from eks_harness.flows.core import App
from eks_harness.flows.mobile import MobileApp
from eks_harness.flows.web import WebApp
from eks_harness.plugins import PluginHost, find_tree

HARNESS_DIR = ".harness"


class FlowError(RuntimeError):
    pass


@dataclass
class FlowRequest:
    flow: Path | None = None
    code: str | None = None
    platform: str | None = None
    params: dict[str, str] = field(default_factory=dict)
    fetch: bool = True
    out: Path | None = None
    verbose: bool = False
    wait: float = 600.0
    release: bool = False
    cwd: Path | None = None
    instance: str | None = None
    session: str | None = None


def is_harness_dir(path: Path) -> bool:
    return path.is_dir() and ((path / "app.py").is_file() or (path / "app.toml").is_file())


def find_harness_dir(flow: Path | None, tree: Path | None, start: Path) -> Path:
    origin = flow.parent if flow else start
    for parent in [origin, *origin.parents]:
        if parent.name == HARNESS_DIR and is_harness_dir(parent):
            return parent
        if is_harness_dir(parent / HARNESS_DIR):
            return parent / HARNESS_DIR
        if tree is not None and parent == tree:
            break
    if tree is not None and (tree / HARNESS_DIR).is_dir():
        return tree / HARNESS_DIR
    raise FlowError("cannot tell which app this flow drives: put it under <app>/.harness/flows/ next to an "
                    "app.toml (or app.py)")


def load_module(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if not spec or not spec.loader:
        raise FlowError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def app_class(harness_dir: Path, platform: str, mixins: list[type], app_file: Path | None = None) -> type[App]:
    base: type[App] = WebApp if platform == "web" else MobileApp
    custom = app_file or harness_dir / "app.py"
    if custom.is_file():
        folder = str(custom.parent)
        if folder not in sys.path:
            sys.path.insert(0, folder)
        module = load_module(custom, f"ehx_app_{abs(hash(str(custom)))}")
        candidate = getattr(module, "App", None)
        if candidate is not None:
            if not (isinstance(candidate, type) and issubclass(candidate, App)):
                raise FlowError(f"{custom}: App must subclass eks_harness.flows.WebApp or MobileApp")
            if platform == "web" and not issubclass(candidate, WebApp) and issubclass(candidate, MobileApp):
                raise FlowError(f"{custom}: App is a MobileApp but the flow runs on web")
            if platform != "web" and issubclass(candidate, WebApp):
                raise FlowError(f"{custom}: App is a WebApp but the flow runs on {platform}")
            base = candidate
    if not mixins:
        return base
    return type(base.__name__, (*mixins, base), {})


def git_branch(tree: Path) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(tree), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True,
                             text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    branch = out.stdout.strip()
    return branch if out.returncode == 0 and branch and branch != "HEAD" else None


def project_and_session(profile: AppProfile, host: PluginHost, tree: Path | None,
                        request: FlowRequest) -> tuple[str, str]:
    project = profile.project or (host.project(tree).project_id if tree else None)
    if not project:
        raise FlowError(f"set [app] project in {profile.root}/.harness/app.toml or [project] id in "
                        f".harness/project.toml (for example acme/web-app)")
    session = request.session or profile.session or (git_branch(tree) if tree else None) or "main"
    return project, session


def acquire(client: Any, *, kind: str, project: str, session: str, instance: str, tree: Path | None,
            wait: float) -> dict[str, Any]:
    from eks_harness.cli.lease_cmds import wait_for_lease

    body = {"kind": kind, "project": project, "session": session, "instance": instance,
            "tree": str(tree) if tree else None, "label": "flow"}
    return wait_for_lease(client, "/api/leases/acquire", body, wait, kind)


@dataclass
class PreparedApp:
    app: App
    handle: Any
    manager: WorkerManager
    project: str
    session: str
    sid: str
    platform: str
    flow: Path | None
    tree: Path | None


def prepare_app(request: FlowRequest, *, client: Any, paths: Any, config: Any, host: PluginHost | None = None,
                manager: WorkerManager | None = None) -> PreparedApp:
    start = (request.cwd or Path.cwd()).resolve()
    flow = request.flow.resolve() if request.flow else None
    tree = find_tree(flow.parent if flow else start)
    harness_dir = find_harness_dir(flow, tree, start)
    profile = load_profile(harness_dir)
    platform = request.platform or profile.default_platform
    if platform not in profile.platforms and (harness_dir / "app.toml").is_file():
        raise FlowError(f"{profile.name} runs on {', '.join(profile.platforms)}, not {platform}")
    host = host or PluginHost(paths, config)
    project, session = project_and_session(profile, host, tree, request)
    if request.instance:
        instance = request.instance
    else:
        from eks_harness.cli.lease_cmds import instance_key

        instance = instance_key(None, tree)
    lease = acquire(client, kind=LEASE_KINDS[platform], project=project, session=session, instance=instance,
                    tree=tree, wait=request.wait)
    sid = lease["sid"]
    cls = app_class(harness_dir, platform, flow_helpers(host, platform, tree), profile.app_file)
    kwargs: dict[str, Any] = {"sid": sid, "harness": client, "verbose": request.verbose, "out": request.out,
                              "params": request.params}
    if issubclass(cls, MobileApp):
        kwargs["platform"] = platform
    app = cls(profile, **kwargs)
    app.lease = lease
    app.prepare()
    extensions = driver_extensions(host, platform, tree)
    pace = pace_from_config(config)
    manager = manager or WorkerManager(paths)
    if platform == "web":
        worker_config = web_worker_config(profile, sid=sid, extensions=extensions, pace=pace,
                                          hide=list(config["capture.hideSelectors"] or []),
                                          playwright_dirs=[node_modules_dir(paths)])
    else:
        worker_config = mobile_worker_config(profile, sids={platform: sid}, extensions=extensions, pace=pace)
    worker_config.update(app.worker_options())
    handle = manager.ensure(WORKER_KINDS[platform], sid, worker_config)
    app.worker = handle.client()
    app.worker_handle = handle
    app.boot()
    return PreparedApp(app=app, handle=handle, manager=manager, project=project, session=session, sid=sid,
                       platform=platform, flow=flow, tree=tree)


def run_flow(request: FlowRequest, *, client: Any, paths: Any, config: Any, host: PluginHost | None = None,
             manager: WorkerManager | None = None) -> dict[str, Any]:
    prepared = prepare_app(request, client=client, paths=paths, config=config, host=host, manager=manager)
    app, handle, manager = prepared.app, prepared.handle, prepared.manager
    project, session, sid, platform, flow = (prepared.project, prepared.session, prepared.sid, prepared.platform,
                                             prepared.flow)
    failure = None
    value = None
    try:
        if request.code:
            scope: dict[str, Any] = {"app": app}
            exec(compile(request.code, "<inline flow>", "exec"), scope)
            value = scope.get("result")
        elif flow:
            module = load_module(flow, f"ehx_flow_{flow.stem.replace('-', '_')}")
            if not hasattr(module, "flow"):
                raise FlowError(f"{flow} has no flow(app) function")
            value = module.flow(app)
    except StepError as error:
        failure = app.failure(error)
    except FlowError:
        raise
    except Exception as error:
        failure = {"step": "python", "error": f"{type(error).__name__}: {error}",
                   "trace": traceback.format_exc(limit=4)[-1200:]}
        if app.recording:
            try:
                app.stop_recording(app.recording)
            except Exception:
                pass
    app.collect(fetch=request.fetch)
    report = app.report(failure=failure, value=value)
    report["project"] = project
    report["session"] = session
    report["worker"] = handle.as_dict()
    if request.release:
        try:
            client.post(f"/api/leases/{sid}/release", json={"reason": "flow finished"})
            manager.stop(WORKER_KINDS[platform], sid)
            report["released"] = True
        except Exception as error:
            report["releaseError"] = str(error)[:300]
    return report


def format_report(report: dict, flow_name: str) -> str:
    lines: list[str] = []
    head = "ok" if report["ok"] else "FAILED"
    lines.append(f"flow {flow_name}: {head} in {report['elapsedMs'] / 1000:.1f}s, {report['steps']} steps")
    if report.get("failure"):
        failure = report["failure"]
        lines.append(f"  failed at {failure.get('step')}: {failure.get('error')}")
        for key in ("url", "issues", "errors", "consoleErrors", "network", "alerts", "route"):
            if failure.get(key):
                lines.append(f"  {key}: {failure[key]}")
    for cp in report["checkpoints"]:
        bits = [f"{k}={cp[k]}" for k in ("url", "route", "issues", "errors", "consoleErrors", "network", "alerts")
                if cp.get(k)]
        lines.append(f"checkpoint {cp['label']}: " + (" ".join(bits) if bits else "clean"))
    for art in report["artifacts"]:
        label = f"{art.get('kind')} {art.get('name')}"
        if art.get("durationSeconds"):
            label += f" ({art['durationSeconds']}s)"
        lines.append(label)
        for key, title in (("url", "page"), ("raw", "direct"), ("local", "local"), ("sheet", "contact sheet"),
                           ("sheetPage", "contact sheet page")):
            if art.get(key):
                lines.append(f"  {title}: {art[key]}")
        for key in ("warnings", "failedInteractions"):
            if art.get(key):
                lines.append(f"  {key}: {art[key]}")
    session = next((a.get("session") for a in report["artifacts"] if a.get("session")), None)
    if session:
        lines.append(f"session: {session}")
    if report.get("notes"):
        lines.append("notes: " + "; ".join(report["notes"]))
    if report.get("value") is not None:
        lines.append(f"value: {json.dumps(report['value'], ensure_ascii=False)[:1500]}")
    if report.get("slowest"):
        lines.append("slowest: " + ", ".join(report["slowest"]))
    return "\n".join(lines)
