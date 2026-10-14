from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal
from urllib.parse import quote

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from eks_harness.cli.client import ApiClientError
from eks_harness.mcp_context import harness_instance, required_tree, resolve_tree
from eks_harness.mcp_system import TreeArg

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

    from eks_harness.mcp_server import HarnessTools

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False)
DEFAULT_WAIT = 120.0
MAX_WAIT = 600.0

SESSIONS = "/api/engine/sessions"

_TERMINAL = frozenset({"passed", "failed", "aborted", "error"})
_HTTP_SLACK = 30.0
_ISSUE_LIMIT = 20


def _issue_text(issue: dict[str, Any]) -> str:
    where = f" at {issue['src']}" if issue.get("src") else (f" node {issue['node']}" if issue.get("node") else "")
    return f"{issue.get('severity', 'error')} {issue.get('code', '?')}{where}: {issue.get('message', '')}"


def _engine_error(problem: ApiClientError) -> ToolError:
    lines = [problem.message]
    issues = problem.payload.get("issues")
    if isinstance(issues, list):
        lines.extend(_issue_text(issue) for issue in issues[:_ISSUE_LIMIT] if isinstance(issue, dict))
        if len(issues) > _ISSUE_LIMIT:
            lines.append(f"... and {len(issues) - _ISSUE_LIMIT} more issues")
    where = problem.payload.get("where")
    if isinstance(where, str) and where:
        lines.append(f"at {where}")
    return ToolError("\n".join(lines))


def _clean(body: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in body.items() if value is not None}


def _q(value: str) -> str:
    return quote(value, safe="")


RunIdArg = Annotated[str, Field(description="Run id")]
SessionIdArg = Annotated[str, Field(description="Session id from eks_session_open")]
SpecArg = Annotated[str | None, Field(description="Entry file relative to the tree or absolute, path/to/file.py or "
                                                  "path/to/file.py::name")]
SourceArg = Annotated[str | None, Field(description="Python source of an entry file, planned as if it lived in the "
                                                    "tree's .harness/<folder>/")]
PlanArg = Annotated[dict[str, Any] | None, Field(description="Plan JSON (eks/plan@1)")]
ParamsArg = Annotated[dict[str, str] | None, Field(description="Entry parameters as text, parsed by their annotations")]
PlatformArg = Annotated[Literal["web", "ios", "android"] | None, Field(description="Platform of a flow")]
FakesArg = Annotated[list[str] | None, Field(description="Op families or ops to simulate with fake executors; ['*'] "
                                                         "simulates every op without a real executor")]


def register_engine_tools(server: MCPServer, tools: HarnessTools) -> list[str]:
    def call(method: str, path: str, **kwargs: Any) -> Any:
        try:
            return tools.client.request(method, path, **kwargs)
        except ApiClientError as problem:
            raise _engine_error(problem) from None

    @server.tool(description="Run a flow, seed, edit, scene or score on the hub from a file (spec), Python source or "
                             "plan JSON and return its report: status, failures with source lines, checkpoints, "
                             "artifacts with links, values, timings and cache use. Waits up to wait seconds; a run "
                             "that is still going or paused comes back with its status (poll eks_run_status).",
                 annotations=WRITE)
    def eks_run(spec: SpecArg = None, source: SourceArg = None, plan: PlanArg = None, params: ParamsArg = None,
                platform: PlatformArg = None, tree: TreeArg = None,
                project: Annotated[str | None, Field(description="Project owner/name (default: the tree's)")] = None,
                session: Annotated[str | None, Field(description="Session (default: the tree's branch)")] = None,
                backend: Annotated[str | None, Field(description="Backend lease id, backend id or definition")] = None,
                breakpoints: Annotated[list[str] | None, Field(description="Breakpoint specs; pausing needs "
                                                                           "debug=true")] = None,
                break_on_error: bool = False, debug: bool = False,
                cache: Literal["use", "refresh", "readonly", "off"] = "use", fakes: FakesArg = None,
                wait: Annotated[float, Field(ge=0, le=MAX_WAIT, description="Seconds to wait")] = DEFAULT_WAIT,
                ) -> dict[str, Any]:
        tree_path = resolve_tree(tree)
        options = _clean({"debug": debug or bool(breakpoints) or break_on_error, "breakpoints": breakpoints or [],
                          "breakOnError": break_on_error, "cache": cache})
        body = _clean({"spec": spec, "source": source, "plan": plan, "params": params or {}, "platform": platform,
                       "tree": tree_path, "cwd": tree_path, "project": project, "session": session,
                       "instance": harness_instance(tree), "options": options,
                       "backend": {"ref": backend} if backend else None,
                       "fakes": {"families": fakes} if fakes else None, "wait": wait})
        if spec is None and source is None and plan is None:
            raise ToolError("Pass spec (an entry file), source (Python source) or plan (plan JSON).")
        created = call("POST", "/api/runs", json=body, timeout=wait + _HTTP_SLACK)
        run = created.get("run", {})
        result: dict[str, Any] = {"issues": created.get("issues", [])}
        if run.get("status") in _TERMINAL:
            result["report"] = call("GET", f"/api/runs/{_q(run['id'])}/report")
            return result
        result["run"] = run
        result["pause"] = run.get("pause")
        return result

    @server.tool(description="Build the plan of an entry (file or Python source) on the hub without running it and "
                             "validate it: the plan JSON and its validation issues.", annotations=READ_ONLY)
    def eks_plan(spec: SpecArg = None, source: SourceArg = None, params: ParamsArg = None,
                 platform: PlatformArg = None, tree: TreeArg = None, plugins: bool = True) -> dict[str, Any]:
        if spec is None and source is None:
            raise ToolError("Pass spec (an entry file) or source (Python source).")
        tree_path = resolve_tree(tree)
        return call("POST", "/api/plans", json=_clean({"spec": spec, "source": source, "params": params or {},
                                                       "platform": platform, "tree": tree_path, "cwd": tree_path,
                                                       "plugins": plugins}))

    @server.tool(description="Validate plan JSON against a tree's registry: ok, hash and issues with codes, nodes "
                             "and source lines.", annotations=READ_ONLY)
    def eks_validate(plan: Annotated[dict[str, Any], Field(description="Plan JSON (eks/plan@1)")],
                     tree: TreeArg = None, plugins: bool = True) -> dict[str, Any]:
        return call("POST", "/api/plans/validate", json=_clean({"plan": plan, "tree": resolve_tree(tree),
                                                                "plugins": plugins}))

    @server.tool(description="The op catalog: every op with its output type, location, flags and executor on the "
                             "hub; with a name, that op's argument JSON Schema.", annotations=READ_ONLY)
    def eks_ops(name: Annotated[str | None, Field(description="Op name such as ui.tap")] = None,
                family: Annotated[str | None, Field(description="Only ops of this family")] = None,
                tree: TreeArg = None) -> dict[str, Any]:
        params = _clean({"tree": resolve_tree(tree), "family": family})
        if name:
            params.pop("family", None)
            return call("GET", f"/api/ops/{_q(name)}", params=params)
        return call("GET", "/api/ops", params=params)

    @server.tool(description="Run one op without a script or session: the hub plans a one-node run, acquires the "
                             "lease and backend it needs, runs it and returns its value and artifacts.",
                 annotations=WRITE)
    def eks_do(op: Annotated[str, Field(description="Op name such as api.get or ui.tap")],
               args: Annotated[dict[str, Any] | None, Field(description="Op arguments; target fields take "
                                                                        "shorthand strings")] = None,
               platform: PlatformArg = None,
               app: Annotated[str | None, Field(description="App profile for ops that use a lease")] = None,
               persona: Annotated[str | None, Field(description="Persona to act as first")] = None,
               backend: Annotated[str | None, Field(description="Backend lease id, backend id or definition")] = None,
               project: str | None = None, session: str | None = None, tree: TreeArg = None, fakes: FakesArg = None,
               wait: Annotated[float, Field(ge=0, le=MAX_WAIT, description="Seconds to wait")] = DEFAULT_WAIT,
               ) -> dict[str, Any]:
        body = _clean({"args": args or {}, "platform": platform, "app": app, "persona": persona,
                       "backend": {"ref": backend} if backend else None, "project": project, "session": session,
                       "tree": resolve_tree(tree), "instance": harness_instance(tree),
                       "fakes": {"families": fakes} if fakes else None, "wait": wait})
        return call("POST", f"/api/ops/{_q(op)}/do", json=body, timeout=wait + _HTTP_SLACK)

    @server.tool(description="Status of a run, with its pause details when paused; report=true adds the full report "
                             "and events_after=<id> the journal events after that id.", annotations=READ_ONLY)
    def eks_run_status(run_id: RunIdArg, report: bool = False,
                       events_after: Annotated[int | None, Field(description="Journal event id to continue "
                                                                             "from")] = None,
                       wait: Annotated[float, Field(ge=0, le=MAX_WAIT, description="Seconds to wait for the run "
                                                                                   "to end or pause")] = 0.0,
                       ) -> dict[str, Any]:
        path = f"/api/runs/{_q(run_id)}"
        result: dict[str, Any] = {"run": call("GET", path, params={"wait": wait} if wait else None,
                                              timeout=wait + _HTTP_SLACK)}
        if report:
            result["report"] = call("GET", f"{path}/report")
        if events_after is not None:
            page = call("GET", f"{path}/journal", params={"events": "true", "after": events_after})
            result["events"] = page.get("events", [])
            result["nextAfter"] = page.get("nextAfter")
        return result

    @server.tool(description="Debugger controls of a run: continue, step, retry, skip, abort, edit (new args for the "
                             "paused node), do (an ad-hoc op in the paused session), pause, break_add, break_remove, "
                             "break_list, status.", annotations=WRITE)
    def eks_run_control(run_id: RunIdArg,
                        action: Annotated[Literal["continue", "step", "retry", "skip", "abort", "edit", "do", "pause",
                                                  "break_add", "break_remove", "break_list", "status"],
                                          Field(description="Action")],
                        node: str | None = None, args: dict[str, Any] | None = None, op: str | None = None,
                        value: Any = None, breakpoint: str | None = None,
                        id: Annotated[str | None, Field(description="Breakpoint id for break_remove")] = None,
                        ) -> dict[str, Any]:
        body = _clean({"action": action, "node": node, "args": args, "op": op, "value": value,
                       "breakpoint": breakpoint, "id": id})
        return call("POST", f"/api/runs/{_q(run_id)}/control", json=body)

    @server.tool(description="Flows, seeds, edits, scenes and scores of a tree: files with their entries, "
                             "parameters, apps and platforms.", annotations=READ_ONLY)
    def eks_flows(tree: TreeArg = None,
                  kind: Annotated[Literal["flow", "seed", "edit", "scene", "score"] | None,
                                  Field(description="Only entries of this kind")] = None) -> dict[str, Any]:
        return call("GET", "/api/flows", params=_clean({"tree": required_tree(tree), "kind": kind}))

    @server.tool(description="Read the source of an entry file of a tree.", annotations=READ_ONLY)
    def eks_flow_get(path: Annotated[str, Field(description="File relative to the tree")],
                     tree: TreeArg = None) -> dict[str, Any]:
        return call("GET", "/api/flows/source", params={"tree": required_tree(tree), "path": path})

    @server.tool(description="Create or replace an entry file under .harness/{flows,seeds,edits,scenes,scores} or "
                             "<app>/.harness/flows, then plan and validate its entries.", annotations=WRITE)
    def eks_flow_save(path: Annotated[str, Field(description="File relative to the tree")],
                      source: Annotated[str, Field(description="New content")], tree: TreeArg = None,
                      expect_sha256: Annotated[str | None, Field(description="Refuse unless the file still has this "
                                                                             "sha256")] = None) -> dict[str, Any]:
        return call("PUT", "/api/flows/source", json=_clean({"tree": required_tree(tree), "path": path,
                                                             "source": source, "expectSha256": expect_sha256}))

    @server.tool(description="Delete an entry file of a tree. Ask the user first.", annotations=DESTRUCTIVE)
    def eks_flow_delete(path: Annotated[str, Field(description="File relative to the tree")],
                        tree: TreeArg = None) -> dict[str, Any]:
        return call("DELETE", "/api/flows/source", params={"tree": required_tree(tree), "path": path})

    @server.tool(description="Open an interactive session: the hub starts a debug run of a flow for the app that "
                             "acquires its lease (and backend with seed, as_ or backend) and pauses at once. Drive it "
                             "with eks_session_do, end it with eks_session_close and turn what you did into a flow "
                             "with eks_session_export. An idle session ends after engine.sessionTimeoutSeconds.",
                 annotations=WRITE)
    def eks_session_open(app: Annotated[str, Field(description="App profile, for example web-app")],
                         platform: PlatformArg = None,
                         seed: Annotated[str | None, Field(description="Seed the backend is restored to")] = None,
                         as_: Annotated[str | None, Field(description="Persona to act as")] = None,
                         backend: Annotated[str | None, Field(description="Backend lease id, backend id or "
                                                                          "definition")] = None,
                         viewport: Annotated[str | None, Field(description="Viewport preset")] = None,
                         tree: TreeArg = None, fakes: FakesArg = None,
                         wait: Annotated[float, Field(ge=0, le=MAX_WAIT, description="Seconds to wait for the "
                                                                                     "session")] = DEFAULT_WAIT,
                         ) -> dict[str, Any]:
        body = _clean({"app": app, "platform": platform, "seed": seed, "persona": as_,
                       "backend": {"ref": backend} if backend else None, "viewport": viewport,
                       "tree": resolve_tree(tree), "instance": harness_instance(tree),
                       "fakes": {"families": fakes} if fakes else None, "wait": wait})
        opened = call("POST", SESSIONS, json=body, timeout=wait + _HTTP_SLACK)
        run = opened.get("run", {})
        if run.get("status") in _TERMINAL:
            report = call("GET", f"/api/runs/{_q(run['id'])}/report")
            lines = [f"the session ended {run.get('status')} before it was ready: {run.get('reason') or ''}".strip()]
            lines += [f"{item.get('code')}: {item.get('message')}" for item in report.get("failures", [])]
            raise ToolError("\n".join(lines))
        return {"session": opened["session"], "status": run.get("status"), "pause": opened.get("pause"),
                "issues": opened.get("issues", []), "links": run.get("links")}

    @server.tool(description="Run one op in an open session, as the paused run's ad-hoc op: returns ok, its value, "
                             "the artifacts it made and its failure. Each call renews the session's idle timeout.",
                 annotations=WRITE)
    def eks_session_do(session: SessionIdArg,
                       op: Annotated[str, Field(description="Op name such as ui.tap or api.get")],
                       args: Annotated[dict[str, Any] | None, Field(description="Op arguments; target fields take "
                                                                                "shorthand strings")] = None,
                       ) -> dict[str, Any]:
        return call("POST", f"{SESSIONS}/{_q(session)}/do", json={"op": op, "args": args or {}},
                    timeout=MAX_WAIT + _HTTP_SLACK)

    @server.tool(description="Close a session: its run continues to the end, passes and releases its lease and "
                             "backend.", annotations=WRITE)
    def eks_session_close(session: SessionIdArg) -> dict[str, Any]:
        return call("DELETE", f"{SESSIONS}/{_q(session)}", timeout=MAX_WAIT + _HTTP_SLACK)

    @server.tool(description="The ops that passed in a session, in order, as a Python flow (each op as its "
                             "authoring call, s.op(...) where there is none), planned and validated. Save it with "
                             "eks_flow_save.", annotations=READ_ONLY)
    def eks_session_export(session: SessionIdArg,
                           name: Annotated[str | None, Field(description="Flow name (default session)")] = None,
                           ) -> dict[str, Any]:
        return call("GET", f"{SESSIONS}/{_q(session)}/export", params=_clean({"name": name}))

    @server.tool(description="Seed snapshots on the hub, newest first: seed, backend definition, digest, resources, "
                             "personas, build time and determinism.", annotations=READ_ONLY)
    def eks_seeds(definition: Annotated[str | None, Field(description="Only snapshots of this backend "
                                                                      "definition")] = None) -> dict[str, Any]:
        return call("GET", "/api/seeds", params=_clean({"definition": definition}))

    @server.tool(description="One seed snapshot with its outputs and personas (logins, secrets redacted).",
                 annotations=READ_ONLY)
    def eks_seed_show(key: Annotated[str, Field(description="Snapshot key from eks_seeds")]) -> dict[str, Any]:
        return call("GET", f"/api/seeds/{_q(key)}")

    return ["eks_run", "eks_plan", "eks_validate", "eks_ops", "eks_do", "eks_run_status", "eks_run_control",
            "eks_flows", "eks_flow_get", "eks_flow_save", "eks_flow_delete", "eks_session_open", "eks_session_do",
            "eks_session_close", "eks_session_export", "eks_seeds", "eks_seed_show"]


__all__ = ["DEFAULT_WAIT", "DESTRUCTIVE", "MAX_WAIT", "READ_ONLY", "SESSIONS", "WRITE", "register_engine_tools"]
