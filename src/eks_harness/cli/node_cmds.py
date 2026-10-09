from __future__ import annotations

import argparse
import json
import sys
import time

from eks_harness import service
from eks_harness.cli.client import client_from_args
from eks_harness.cli.ui import EXIT_ERROR, EXIT_USAGE, console, fail, kv_panel, ok, print_json, table, warn
from eks_harness.paths import resolve_paths


def _json_arg(text: str | None, what: str):
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        fail(f"{what} is not valid JSON: {error}", EXIT_USAGE)


def _slot_text(slots: list[dict]) -> str:
    return ", ".join(f"{s['id']} {s.get('busy', 0)}/{s.get('workers', 1)}" for s in slots)


def cmd_list(args: argparse.Namespace) -> int:
    with client_from_args(args) as client:
        items = client.get("/api/nodes")["items"]
    if args.json:
        print_json({"items": items})
        return 0
    rows = []
    for item in items:
        caps = item.get("capabilities") or {}
        gpus = ", ".join(g.get("name", "?") for g in caps.get("gpus") or []) or "none"
        rows.append((item["id"], item["state"], f"{caps.get('os', '')} {caps.get('arch', '')}".strip(), gpus,
                     _slot_text(item.get("slots") or []), str(len(item.get("running") or []))))
    console.print(table(["node", "state", "system", "gpus", "slots busy/workers", "jobs"], rows))
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    with client_from_args(args) as client:
        item = client.get(f"/api/nodes/{args.id}")
    if args.json:
        print_json(item)
        return 0
    caps = item.get("capabilities") or {}
    console.print(kv_panel(item["id"], [("state", item["state"]), ("label", item.get("label")),
                                        ("version", item.get("version")), ("system", f"{caps.get('os')} {caps.get('arch')}"),
                                        ("cpus", caps.get("cpus")), ("memory", f"{caps.get('memoryMb', 0)} MB"),
                                        ("blender", (caps.get("blender") or {}).get("version")),
                                        ("job kinds", ", ".join(caps.get("jobKinds") or []))]))
    slots = item.get("slots") or []
    if slots:
        console.print(table(["slot", "backend", "workers", "busy", "env"],
                            [(s["id"], s.get("backend", ""), str(s.get("workers", 1)), str(s.get("busy", 0)),
                              " ".join(f"{k}={v}" for k, v in (s.get("env") or {}).items())) for s in slots]))
    jobs = item.get("jobs") or []
    if jobs:
        console.print(table(["job", "kind", "state", "slot"], [(j["id"], j["kind"], j["state"], j.get("slot") or "")
                                                                for j in jobs[:20]]))
    return 0


def cmd_create(args: argparse.Namespace) -> int:
    with client_from_args(args) as client:
        data = client.post("/api/nodes", json={"id": args.id, "label": args.label or "",
                                               "config": {"slots": _json_arg(args.slots, "--slots") or []}})
    if args.json:
        print_json(data)
        return 0
    ok(f"node {args.id} created; on the node run:")
    console.print(f"  eks-harness node configure --hub {data['hubUrl']} --name {args.id} --token-stdin", markup=False)
    console.print(f"token (shown once): {data['token']}", markup=False)
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    from eks_harness.nodes.install import InstallError, RemoteTarget, install_node

    node_id = args.id or args.host.split("@")[-1].split(".")[0].lower()
    slots = _json_arg(args.slots, "--slots")
    env = dict(item.split("=", 1) for item in args.env or [])
    if args.dry_run:
        print_json({"node": node_id, "host": args.host, "wsl": args.wsl, "slots": slots, "env": env})
        return 0
    with client_from_args(args) as client:
        existing = {n["id"] for n in client.get("/api/nodes")["items"]}
        if node_id in existing:
            data = client.post(f"/api/nodes/{node_id}/token")
        else:
            data = client.post("/api/nodes", json={"id": node_id, "label": args.label or args.host,
                                                   "config": {"slots": slots} if slots and args.pin_slots else {}})
        hub = args.hub_url or data["hubUrl"]
        try:
            result = install_node(RemoteTarget(args.host, wsl=args.wsl), hub=hub, token=data["token"], name=node_id,
                                  slots=slots, blender=args.blender or "", env=env,
                                  spec=args.spec, extras=args.extras or "",
                                  log=lambda text: console.print(f"  {text}", markup=False))
        except InstallError as error:
            fail(str(error), EXIT_ERROR)
        deadline = time.time() + args.wait
        online = False
        while time.time() < deadline:
            items = {n["id"]: n for n in client.get("/api/nodes")["items"]}
            if items.get(node_id, {}).get("online"):
                online = True
                break
            time.sleep(2)
    if args.json:
        print_json({**result, "node": node_id, "online": online})
        return 0 if online else EXIT_ERROR
    if online:
        ok(f"node {node_id} is online")
    else:
        warn(f"node {node_id} installed but not connected yet; check its log: ssh {args.host} "
             f"'tail -n 50 ~/.local/state/eks-harness/log/node.log'")
    if result.get("hint"):
        warn(result["hint"])
    return 0 if online else EXIT_ERROR


def cmd_configure(args: argparse.Namespace) -> int:
    from eks_harness.nodes.agent import NodeSettings, save_settings

    token = sys.stdin.readline().strip() if args.token_stdin else args.token
    if not token:
        fail("pass --token or --token-stdin", EXIT_USAGE)
    env = dict(item.split("=", 1) for item in args.env or [])
    settings = NodeSettings(hub=args.hub, token=token, name=args.name or "", slots=_json_arg(args.slots, "--slots") or [],
                            cache_dir=args.cache_dir or "", blender=args.blender or "", env=env)
    path = save_settings(resolve_paths().ensure(), settings)
    if args.json:
        print_json({"settings": str(path)})
    else:
        ok(f"node settings written to {path}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import logging

    from eks_harness.nodes.agent import load_settings, serve

    logging.getLogger("eks_harness").setLevel(logging.INFO)
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    paths = resolve_paths().ensure()
    try:
        settings = load_settings(paths)
    except (OSError, ValueError, KeyError) as error:
        fail(f"no node settings ({error}); run: eks-harness node configure --hub URL --token-stdin", EXIT_USAGE)
    serve(paths, settings)
    return 0


def cmd_service(args: argparse.Namespace) -> int:
    paths = resolve_paths().ensure()
    if args.action == "install":
        info = service.install(paths, role=service.NODE)
    elif args.action == "uninstall":
        info = service.uninstall(paths, role=service.NODE) or {}
    else:
        info = {"started": service.start(paths, role=service.NODE)}
    if args.json:
        print_json(info)
    else:
        ok(f"node service {args.action}: {info.get('where') or info}")
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    body: dict = {}
    if args.slots is not None:
        body["config"] = {"slots": _json_arg(args.slots, "--slots") or []}
    if args.label is not None:
        body["label"] = args.label
    if args.disable or args.enable:
        body["disabled"] = bool(args.disable)
    with client_from_args(args) as client:
        item = client.patch(f"/api/nodes/{args.id}", json=body)
    print_json(item) if args.json else ok(f"node {args.id}: {item['state']}")
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    with client_from_args(args) as client:
        client.delete(f"/api/nodes/{args.id}")
    ok(f"node {args.id} removed")
    return 0


def cmd_token(args: argparse.Namespace) -> int:
    with client_from_args(args) as client:
        data = client.post(f"/api/nodes/{args.id}/token")
    print_json(data) if args.json else console.print(data["token"], markup=False)
    return 0


def cmd_job_submit(args: argparse.Namespace) -> int:
    body = {"kind": args.kind, "payload": _json_arg(args.payload, "--payload") or {},
            "requirements": _json_arg(args.requirements, "--requirements") or {}, "priority": args.priority}
    with client_from_args(args) as client:
        job = client.post("/api/jobs", json=body)
        if args.wait:
            job = client.get(f"/api/jobs/{job['id']}", params={"wait": args.wait}, timeout=args.wait + 30)
    if args.json:
        print_json(job)
    else:
        ok(f"job {job['id']} {job['state']}" + (f" on {job['node']}" if job.get("node") else ""))
    return 0 if job["state"] not in ("failed", "cancelled") else EXIT_ERROR


def cmd_job_list(args: argparse.Namespace) -> int:
    params = {k: v for k, v in {"state": args.state, "node": args.node, "kind": args.kind}.items() if v}
    with client_from_args(args) as client:
        items = client.get("/api/jobs", params=params)["items"]
    if args.json:
        print_json({"items": items})
        return 0
    rows = [(j["id"], j["kind"], j["state"], j.get("node") or "", j.get("slot") or "",
             f"{round((j.get('progress') or 0) * 100)}%", (j.get("error") or j.get("message") or "")[:60]) for j in items]
    console.print(table(["job", "kind", "state", "node", "slot", "progress", "note"], rows))
    return 0


def cmd_job_show(args: argparse.Namespace) -> int:
    with client_from_args(args) as client:
        job = client.get(f"/api/jobs/{args.id}", params={"wait": args.wait} if args.wait else None,
                         timeout=(args.wait or 0) + 30)
    print_json(job)
    return 0


def cmd_job_cancel(args: argparse.Namespace) -> int:
    with client_from_args(args) as client:
        job = client.post(f"/api/jobs/{args.id}/cancel")
    ok(f"job {job['id']} {job['state']}")
    return 0


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("node", help="machines that run hub jobs (renders, previews, builds)",
                                   description="Nodes dial out to the hub over WebSocket and run jobs in GPU and CPU "
                                               "slots. 'node add' installs and registers one over SSH.")
    sub = parser.add_subparsers(dest="node_command", metavar="<subcommand>", required=True)
    p = sub.add_parser("list", help="nodes, their state, GPUs and slots")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list)
    p = sub.add_parser("show", help="one node: capabilities, slots, recent jobs")
    p.add_argument("id")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_show)
    p = sub.add_parser("create", help="register a node and print its token (manual install)")
    p.add_argument("id")
    p.add_argument("--label")
    p.add_argument("--slots", help="JSON list of slots pinned on the hub")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_create)
    p = sub.add_parser("add", help="install, configure and start a node over SSH")
    p.add_argument("host", help="ssh destination, for example user@gpu-box")
    p.add_argument("--id", help="node id (default: the host name)")
    p.add_argument("--label")
    p.add_argument("--hub-url", help="URL the node dials (default: nodes.hubUrl, the public URL or the listen address)")
    p.add_argument("--wsl", action="store_true", help="the SSH host is Windows; install inside WSL")
    p.add_argument("--slots", help='JSON slots, for example [{"id":"gpu0","workers":2,"env":{"CUDA_VISIBLE_DEVICES":"0"},'
                                   '"tags":["gpu","optix"],"backend":"OPTIX"}]')
    p.add_argument("--pin-slots", action="store_true", help="also pin the slots on the hub")
    p.add_argument("--blender", help="Blender executable on the node")
    p.add_argument("--env", action="append", help="KEY=VALUE for every job on the node (repeatable)")
    p.add_argument("--spec", help=f"install from this requirement instead of a wheel built here (e.g. {'git+https://...'})")
    p.add_argument("--extras", help="eks-harness extras to install on the node (e.g. video)")
    p.add_argument("--wait", type=float, default=60, help="seconds to wait for the node to connect")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_add)
    p = sub.add_parser("configure", help="write this machine's node settings (run on the node)")
    p.add_argument("--hub", required=True)
    p.add_argument("--token")
    p.add_argument("--token-stdin", action="store_true")
    p.add_argument("--name")
    p.add_argument("--slots")
    p.add_argument("--cache-dir")
    p.add_argument("--blender")
    p.add_argument("--env", action="append")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_configure)
    p = sub.add_parser("serve", help="run the node agent in the foreground (run on the node)")
    p.set_defaults(func=cmd_serve)
    p = sub.add_parser("service", help="install, uninstall or start the node agent service (run on the node)")
    p.add_argument("action", choices=["install", "uninstall", "start"])
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_service)
    p = sub.add_parser("set", help="change a node's slots, label or enabled state")
    p.add_argument("id")
    p.add_argument("--slots")
    p.add_argument("--label")
    p.add_argument("--disable", action="store_true")
    p.add_argument("--enable", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_set)
    p = sub.add_parser("remove", help="forget a node (its queued jobs go back to the queue)")
    p.add_argument("id")
    p.set_defaults(func=cmd_remove)
    p = sub.add_parser("token", help="issue a new token for a node (the old one stops working)")
    p.add_argument("id")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_token)

    parser = subparsers.add_parser("job", help="jobs that run on nodes")
    sub = parser.add_subparsers(dest="job_command", metavar="<subcommand>", required=True)
    p = sub.add_parser("submit", help="queue a job")
    p.add_argument("kind", help="job kind: probe, shell, echo or a plugin kind")
    p.add_argument("--payload")
    p.add_argument("--requirements", help='JSON, for example {"gpu": true, "capabilities": ["blender"]}')
    p.add_argument("--priority", type=int, default=0)
    p.add_argument("--wait", type=float, default=0, help="seconds to wait for the result")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_job_submit)
    p = sub.add_parser("list", help="recent jobs")
    p.add_argument("--state")
    p.add_argument("--node")
    p.add_argument("--kind")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_job_list)
    p = sub.add_parser("show", help="one job as JSON")
    p.add_argument("id")
    p.add_argument("--wait", type=float, default=0)
    p.set_defaults(func=cmd_job_show)
    p = sub.add_parser("cancel", help="cancel a job")
    p.add_argument("id")
    p.set_defaults(func=cmd_job_cancel)
