from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any
from urllib.parse import quote

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

    from eks_harness.mcp_server import HarnessTools

log = logging.getLogger("eks_harness.mcp")

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False)

TreeArg = Annotated[str | None, Field(description="Work tree path whose .harness/ applies (default: the server's cwd)")]


def _tree(tree: str | None) -> str:
    from eks_harness.plugins import find_tree

    base = Path(tree).expanduser() if tree else Path(os.getcwd())
    found = find_tree(base)
    return str(found or base.resolve())


def _score_file(path: str) -> Path:
    file = Path(path).expanduser()
    if not file.is_file():
        raise ToolError(f"no score file {file}")
    return file


def _load_score(path: str) -> tuple[Any, Path]:
    from eks_harness.score import ScoreIR

    file = _score_file(path)
    if file.suffix == ".py":
        from eks_harness.score.loader import load_score_module

        return load_score_module(file), file.parent
    try:
        return ScoreIR.load(file), file.parent
    except Exception as error:
        raise ToolError(f"{file}: {error}") from None


def register_system_tools(server: MCPServer, tools: HarnessTools) -> None:
    @server.tool(description="Plugins visible for a work tree: id, source (builtin, package, path, repo, git), "
                             "state (active, pending approval, changed, disabled, error) and what each contributes.",
                 annotations=READ_ONLY)
    def list_plugins(tree: TreeArg = None) -> dict[str, Any]:
        return tools.call("GET", "/api/plugins", params={"tree": _tree(tree)})

    @server.tool(description="One plugin: contributions (drivers, backends, effects, UI slots...), settings and "
                             "their current values.", annotations=READ_ONLY)
    def get_plugin(plugin_id: Annotated[str, Field(description="Plugin id, for example acme.backend")],
                   tree: TreeArg = None) -> dict[str, Any]:
        return tools.call("GET", f"/api/plugins/{quote(plugin_id)}", params={"tree": _tree(tree)})

    @server.tool(description="Approve a folder or git plugin so it loads (pins its content hash). Ask the user "
                             "before approving code you have not reviewed.", annotations=WRITE)
    def trust_plugin(plugin_id: Annotated[str, Field(description="Plugin id")], tree: TreeArg = None) -> dict[str, Any]:
        return tools.call("POST", f"/api/plugins/{quote(plugin_id)}/trust", json={"tree": _tree(tree)})

    @server.tool(description="Set plugin settings (hub-wide; a repo's .harness/project.toml can override them).",
                 annotations=WRITE)
    def set_plugin_settings(plugin_id: Annotated[str, Field(description="Plugin id")],
                            values: Annotated[dict[str, Any], Field(description="Setting key to value")]) -> dict[str, Any]:
        return tools.call("PUT", f"/api/plugins/{quote(plugin_id)}/settings", json={"values": values})

    @server.tool(description="Nodes (machines that run renders, previews and builds): state, GPUs, slots and "
                             "running jobs.", annotations=READ_ONLY)
    def list_nodes() -> dict[str, Any]:
        return tools.call("GET", "/api/nodes")

    @server.tool(description="Queue a job on the nodes. kind is probe, shell, echo or a plugin job kind (for "
                             "example blender.frames); requirements select nodes and slots, e.g. "
                             "{\"gpu\": true, \"capabilities\": [\"blender\"]}. wait seconds blocks for the result.",
                 annotations=WRITE)
    def submit_job(kind: Annotated[str, Field(description="Job kind")],
                   payload: Annotated[dict[str, Any] | None, Field(description="Job payload")] = None,
                   requirements: Annotated[dict[str, Any] | None, Field(description="Node and slot requirements")] = None,
                   wait: Annotated[float, Field(ge=0, le=600, description="Seconds to wait for the result")] = 0,
                   ) -> dict[str, Any]:
        job = tools.call("POST", "/api/jobs", json={"kind": kind, "payload": payload or {},
                                                    "requirements": requirements or {}})
        if wait:
            job = tools.call("GET", f"/api/jobs/{job['id']}", params={"wait": wait}, timeout=wait + 30)
        return job

    @server.tool(description="Jobs on nodes, newest first, optionally by state (queued,running,done,failed), "
                             "node or kind.", annotations=READ_ONLY)
    def list_jobs(state: str | None = None, node: str | None = None, kind: str | None = None) -> dict[str, Any]:
        return tools.call("GET", "/api/jobs", params={k: v for k, v in {"state": state, "node": node,
                                                                        "kind": kind}.items() if v})

    @server.tool(description="One job with progress, result and output blobs; wait blocks until it ends.",
                 annotations=READ_ONLY)
    def get_job(job_id: str, wait: Annotated[float, Field(ge=0, le=600)] = 0) -> dict[str, Any]:
        return tools.call("GET", f"/api/jobs/{job_id}", params={"wait": wait} if wait else None, timeout=wait + 30)

    @server.tool(description="Cancel a queued or running job.", annotations=DESTRUCTIVE)
    def cancel_job(job_id: str) -> dict[str, Any]:
        return tools.call("POST", f"/api/jobs/{job_id}/cancel")

    @server.tool(description="Which backend a work tree should use (local build, staging, ...) according to the "
                             "repo's backend policy plugins, with the reason.", annotations=READ_ONLY)
    def choose_backend(tree: TreeArg = None, requested: str | None = None) -> dict[str, Any]:
        return tools.call("POST", "/api/backends/choose", json={"tree": _tree(tree), "requested": requested})

    @server.tool(description="Backend definitions available for a work tree (repo scripts and plugin backends).",
                 annotations=READ_ONLY)
    def list_backend_definitions(tree: TreeArg = None) -> dict[str, Any]:
        return tools.call("GET", "/api/backends/definitions", params={"tree": _tree(tree)})

    @server.tool(description="Running and stopped backends with ports, exports and status.", annotations=READ_ONLY)
    def list_backends() -> dict[str, Any]:
        return tools.call("GET", "/api/backends")

    @server.tool(description="Start a backend for an instance (or keep it) and return its record; poll "
                             "list_backends until status is running.", annotations=WRITE)
    def ensure_backend(definition: str, instance: Annotated[str, Field(description="Harness instance key, "
                                                                       "for example session:<id> or tree:<path>")],
                       tree: TreeArg = None) -> dict[str, Any]:
        return tools.call("POST", "/api/backends/ensure", json={"definition": definition, "instance": instance,
                                                                "tree": _tree(tree)}, timeout=300)

    @server.tool(description="Contact sheet of a video artifact: one image with evenly spaced frames labelled by "
                             "time and frame number, highlighted frames at given cues, and the audio waveform with "
                             "beat, downbeat and cue ticks. Stored next to the video; the sheet images are returned "
                             "so you can look at the whole video at once.", annotations=WRITE)
    def video_sheet(artifact_id: Annotated[str, Field(description="Video artifact id")],
                    markers: Annotated[dict[str, Any] | list[Any] | None, Field(
                        description="{beats: [s], downbeats: [s], cues: [{t, label}], markers: [{t, kind, label}], "
                                    "frames: [{t, label}]}; cues and frames become highlighted tiles")] = None,
                    frames: Annotated[int | None, Field(ge=1, le=400, description="Evenly spaced frames "
                                                        "(default 2 per second, 12 to 96)")] = None,
                    images: Annotated[bool, Field(description="Attach the sheet images")] = True) -> list[Any]:
        from mcp.server.mcpserver import Image

        data = tools.call("POST", f"/api/artifacts/{quote(artifact_id.strip().upper(), safe='')}/sheet",
                          json={"markers": markers, "frames": frames}, timeout=900)
        summary = {"video": data["video"].get("url"), "info": data.get("info"),
                   "sheets": [{"id": s["id"], "page": s.get("url"), "part": s["part"], "parts": s["parts"],
                               "range": s["range"], "tiles": s["tiles"]} for s in data["sheets"]]}
        content: list[Any] = [summary]
        if images:
            for sheet in data["sheets"]:
                raw = tools.fetch_bytes(f"/raw/{quote(sheet['id'], safe='')}/{quote(sheet['filename'], safe='')}",
                                        8 * 1024 * 1024)
                if raw is not None:
                    content.append(Image(data=raw, format="jpeg"))
        return content

    @server.tool(description="Check a video artifact against an expected timeline. expectations is "
                             "{tolerance_frames, checks: [{t, kind, params, tolerance_frames, label}]} with kinds "
                             "cut, motion, text (local OCR), visual (template or colour in a box), black, frozen, "
                             "safe_area, loudness, onset, beats and plugin kinds. Returns pass/fail with measured vs "
                             "expected times and the delta in frames, and stores the report next to the video.",
                 annotations=WRITE)
    def video_check(artifact_id: Annotated[str, Field(description="Video artifact id")],
                    expectations: Annotated[dict[str, Any] | list[Any], Field(description="Checks to run")],
                    tree: TreeArg = None) -> dict[str, Any]:
        data = tools.call("POST", f"/api/artifacts/{quote(artifact_id.strip().upper(), safe='')}/checks",
                          json={"expectations": expectations, "tree": _tree(tree)}, timeout=1800)
        return {"ok": data["ok"], "text": data["text"], "report": data["artifact"].get("url"),
                "results": data["report"]["results"], "detected": data["report"].get("detected")}

    @server.tool(description="The render queue: video renders run one at a time on the hub machine (render.concurrency); "
                             "shows what renders now, what waits, in order, and the leases each render took.",
                 annotations=READ_ONLY)
    def render_queue() -> dict[str, Any]:
        return tools.call("GET", "/api/render-queue")

    @server.tool(description="Seed a running backend with a scenario from the seeder plugins.", annotations=WRITE)
    def seed_backend(backend_id: str, scenario: str | None = None, seeder: str | None = None) -> dict[str, Any]:
        return tools.call("POST", f"/api/backends/{quote(backend_id, safe='@:')}/seed",
                          json={"scenario": scenario, "seeder": seeder}, timeout=3600)

    @server.tool(description="Personas (logins) the credentials plugins offer for a target such as local or "
                             "staging. Secrets are never returned.", annotations=READ_ONLY)
    def list_personas(tree: TreeArg = None, target: str = "local") -> dict[str, Any]:
        return tools.call("GET", "/api/backends/personas", params={"tree": _tree(tree), "target": target})

    @server.tool(description="Validate a score (score.json or a Python DSL file that defines `score`) and return "
                             "its tracks, rules and clock.", annotations=READ_ONLY)
    def score_validate(path: Annotated[str, Field(description="Path to score.json or score.py")]) -> dict[str, Any]:
        score, _ = _load_score(path)
        built = score.build() if hasattr(score, "build") else score
        return {"ok": True, "name": built.name, "fps": built.clock.fps,
                "tracks": [{"id": t.id, "kind": t.kind} for t in built.tracks], "rules": len(built.rules)}

    @server.tool(description="Resolve a score's clock (beats from its audio), track spans and every scheduled "
                             "action and event with exact frames. observed adds device or scene events "
                             "({time, source, name, data}) to see the chained actions they trigger.",
                 annotations=READ_ONLY)
    def score_plan(path: Annotated[str, Field(description="Path to score.json or score.py")],
                   observed: Annotated[list[dict[str, Any]] | None, Field(description="Extra events")] = None,
                   ) -> dict[str, Any]:
        from eks_harness.score import Event, plan
        from eks_harness.score.analysis import beat_analyzer

        score, base = _load_score(path)
        built = score.build() if hasattr(score, "build") else score
        events = [Event(float(e["time"]), str(e["source"]), str(e["name"]), dict(e.get("data") or {}))
                  for e in observed or []]
        return plan(built, observed=events, base=base, analyzer=beat_analyzer).as_dict()

    @server.tool(description="Write a score built with the Python DSL (score.py defining `score`) to JSON IR.",
                 annotations=WRITE)
    def score_export(path: str, out: str | None = None) -> dict[str, Any]:
        score, base = _load_score(path)
        built = score.build() if hasattr(score, "build") else score
        target = Path(out).expanduser() if out else Path(path).with_suffix(".json")
        built.save(target)
        return {"path": str(target), "bytes": target.stat().st_size, "tracks": len(built.tracks)}


def _worker_session(sid: str) -> Any:
    from eks_harness.drivers.sessions import WorkerSession
    from eks_harness.drivers.workers import WorkerManager
    from eks_harness.paths import resolve_paths

    manager = WorkerManager(resolve_paths())
    for kind, platform in (("web", "web"), ("mobile", None)):
        handle = manager.get(kind, sid)
        if handle is not None:
            if platform is None:
                platform = str((handle.extra or {}).get("platform") or "ios")
            return WorkerSession(handle.client(), platform=platform, sid=sid)
    raise ToolError(f"no driver worker runs for lease {sid}; start one with flow_run (or eks-harness flow run) first")


def register_driver_tools(server: MCPServer, tools: HarnessTools) -> None:
    import subprocess
    import sys

    @server.tool(description="Driver workers running on this machine (one per leased browser profile or device), "
                             "with their lease sid, kind and port.", annotations=READ_ONLY)
    def driver_workers() -> dict[str, Any]:
        from eks_harness.drivers.workers import WorkerManager
        from eks_harness.paths import resolve_paths

        return {"items": [h.as_dict() for h in WorkerManager(resolve_paths()).list()]}

    @server.tool(description="Act on a leased browser or device through its driver worker: press/click/tap, fill, "
                             "type, key, scroll, navigate, back, reload, evaluate, dispatch (app store action), "
                             "patch/unpatch/inject (React Native live UI), arm (fakes such as camera or NFC), emit. "
                             "Targets use the driver grammar: #testId, text=..., role=button:Save, label=..., css=....",
                 annotations=WRITE)
    def driver_act(sid: Annotated[str, Field(description="Lease sid")],
                   action: Annotated[str, Field(description="Action name")],
                   params: Annotated[dict[str, Any] | None, Field(description="Action parameters, e.g. "
                                                                  "{\"target\": \"#save\"}")] = None) -> dict[str, Any]:
        try:
            return _worker_session(sid).act(action, **(params or {}))
        except ToolError:
            raise
        except Exception as error:
            raise ToolError(f"{action} failed: {error}") from None

    @server.tool(description="Read state cheaply before paying for images: tree (accessibility or element tree), "
                             "text, exists, visible, count, value, url or route, state (app store), logs.",
                 annotations=READ_ONLY)
    def driver_observe(sid: Annotated[str, Field(description="Lease sid")],
                       query: Annotated[str, Field(description="tree, text, exists, url, route, state, logs, ...")],
                       target: str | None = None) -> dict[str, Any]:
        try:
            return _worker_session(sid).observe(query, **({"target": target} if target else {}))
        except ToolError:
            raise
        except Exception as error:
            raise ToolError(f"{query} failed: {error}") from None

    @server.tool(description="Capture evidence into the artifact store: screenshot, video.start, video.stop, dom, "
                             "mhtml, a11y, har.start, har.stop. Returns artifact links; open what you capture.",
                 annotations=WRITE)
    def driver_capture(sid: Annotated[str, Field(description="Lease sid")],
                       kind: Annotated[str, Field(description="Capture kind")],
                       params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            return _worker_session(sid).capture(kind, **(params or {}))
        except ToolError:
            raise
        except Exception as error:
            raise ToolError(f"{kind} failed: {error}") from None

    @server.tool(description="Run a Python flow (flow(app) in <app>/.harness/flows/*.py) or inline code against a "
                             "leased browser or device and return its report: checkpoints, failures, screenshots, "
                             "videos and contact sheets with local paths to Read.", annotations=WRITE)
    def flow_run(flow: Annotated[str | None, Field(description="Path to a flow file")] = None,
                 code: Annotated[str | None, Field(description="Inline Python with `app` in scope")] = None,
                 platform: Annotated[str | None, Field(description="web, ios or android")] = None,
                 params: Annotated[dict[str, str] | None, Field(description="Values for app.params")] = None,
                 release: bool = False, tree: TreeArg = None) -> dict[str, Any]:
        if not flow and not code:
            raise ToolError("pass a flow path or inline code")
        argv = [sys.executable, "-m", "eks_harness.cli", "flow", "run", "--json"]
        if flow:
            argv.append(flow)
        if code:
            argv += ["-e", code]
        if platform:
            argv += ["--platform", platform]
        for key, value in (params or {}).items():
            argv += ["--set", f"{key}={value}"]
        if release:
            argv.append("--release")
        out = subprocess.run(argv, cwd=_tree(tree), capture_output=True, text=True, timeout=3600)
        try:
            return json.loads(out.stdout)
        except ValueError:
            raise ToolError((out.stderr or out.stdout).strip()[-3000:] or f"flow exited {out.returncode}") from None


def register_plugin_tools(server: MCPServer, tools: HarnessTools) -> list[str]:
    try:
        from eks_harness.config import load as load_config
        from eks_harness.paths import resolve_paths
        from eks_harness.plugins import PluginHost, find_tree

        paths = resolve_paths()
        host = PluginHost(paths, load_config(paths))
        tree = find_tree(Path(os.getcwd()))
        loaded = []
        for contribution, target in host.load_all("mcp_tools", tree):
            try:
                target.register(server, tools)
                loaded.append(contribution.key)
            except Exception as error:
                log.warning("mcp tools %s failed: %s", contribution.key, error)
        return loaded
    except Exception as error:
        log.warning("plugin MCP tools unavailable: %s", error)
        return []


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
