from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path
from typing import Any

from eks_harness import __version__, service
from eks_harness.cli.client import client_from_args
from eks_harness.cli.ui import EXIT_ERROR, console, print_json, table
from eks_harness.config import load as load_config
from eks_harness.paths import resolve_paths

WORDS = {"ok": "[green]ok[/]", "warn": "[yellow]warn[/]", "fail": "[bold red]fail[/]", "info": "info"}


def _version(argv: list[str]) -> str:
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return ""
    text = (out.stdout or out.stderr).strip().splitlines()
    return text[0][:80] if text else ""


def tool_checks() -> list[dict[str, Any]]:
    rows = []
    for name, argv, need in (("ffmpeg", ["ffmpeg", "-version"], "videos and recordings"),
                             ("ffprobe", ["ffprobe", "-version"], "videos and recordings"),
                             ("node", ["node", "--version"], "driver workers"),
                             ("pnpm", ["pnpm", "--version"], "building the UI and SDKs"),
                             ("uv", ["uv", "--version"], "plugins with requirements, node installs"),
                             ("git", ["git", "--version"], "git plugins, trees"),
                             ("adb", ["adb", "version"], "Android devices"),
                             ("xcrun", ["xcrun", "--version"], "iOS simulators")):
        found = shutil.which(name)
        rows.append({"check": f"tool {name}", "state": "ok" if found else "warn",
                     "detail": _version(argv) if found else f"missing (needed for {need})"})
    from eks_harness.nodes.capabilities import blender_info

    blender = blender_info()
    rows.append({"check": "tool blender", "state": "ok" if blender else "info",
                 "detail": f"{blender['version']} at {blender['path']}" if blender else
                 "not found (only needed for Blender scenes; EKS_HARNESS_BLENDER)"})
    try:
        from eks_harness.video.plugins.builtin.media.web_scene import runner

        runner.require_playwright()
        version = runner.browser_version()
        rows.append({"check": "web scenes", "state": "ok" if version != "unknown" else "warn",
                     "detail": f"Chromium {version}" if version != "unknown" else
                     "Playwright Chromium missing: eks-harness driver install web"})
    except Exception as error:
        rows.append({"check": "web scenes", "state": "info", "detail": f"unavailable ({error})"})
    return rows


def hub_checks(args: argparse.Namespace) -> list[dict[str, Any]]:
    rows = []
    try:
        with client_from_args(args) as client:
            health = client.get("/api/health")
            rows.append({"check": "hub", "state": "ok", "detail": f"{client.base_url} {health.get('version', '')}"})
            if health.get("version") and health["version"] != __version__:
                rows.append({"check": "hub version", "state": "warn",
                             "detail": f"hub {health['version']}, CLI {__version__}: restart the hub after upgrades"})
            try:
                plugins = client.get("/api/plugins")["items"]
                pending = [p["id"] for p in plugins if p["state"] in ("pending", "changed")]
                broken = [f"{p['id']}: {p.get('reason') or p.get('error')}" for p in plugins if p["state"] == "error"]
                rows.append({"check": "plugins", "state": "warn" if pending or broken else "ok",
                             "detail": f"{sum(1 for p in plugins if p['state'] == 'active')} active"
                                       + (f"; waiting for approval: {', '.join(pending)}" if pending else "")
                                       + (f"; errors: {'; '.join(broken)}" if broken else "")})
            except Exception as error:
                rows.append({"check": "plugins", "state": "warn", "detail": str(error)[:200]})
            try:
                nodes = client.get("/api/nodes")["items"]
                offline = [n["id"] for n in nodes if n["state"] == "offline"]
                rows.append({"check": "nodes", "state": "warn" if offline else "ok",
                             "detail": f"{sum(1 for n in nodes if n['online'])} of {len(nodes)} online"
                                       + (f"; offline: {', '.join(offline)}" if offline else "")})
            except Exception as error:
                rows.append({"check": "nodes", "state": "warn", "detail": str(error)[:200]})
    except Exception as error:
        rows.append({"check": "hub", "state": "fail", "detail": f"not reachable: {str(error)[:200]} "
                                                                "(eks-harness daemon start)"})
    return rows


def local_checks() -> list[dict[str, Any]]:
    paths = resolve_paths()
    rows = []
    config = load_config(paths)
    errors = config.errors() if hasattr(config, "errors") else {}
    rows.append({"check": "config", "state": "warn" if errors else "ok",
                 "detail": "; ".join(f"{k}: {v}" for k, v in errors.items()) or str(paths.config_file)})
    for label, path in (("data dir", paths.data_dir), ("cache dir", paths.cache_dir)):
        try:
            free = shutil.disk_usage(path if path.exists() else Path.home()).free / 2**30
        except OSError:
            free = 0
        rows.append({"check": label, "state": "warn" if free < 10 else "ok", "detail": f"{path} ({free:.0f} GB free)"})
    for role in (service.DAEMON, service.NODE):
        record = service.installed(paths, role)
        rows.append({"check": f"service {role.name}", "state": "ok" if record else "info",
                     "detail": record.get("where") if record else "not installed"})
    return rows


def cmd_doctor(args: argparse.Namespace) -> int:
    rows = [{"check": "version", "state": "info", "detail": __version__}, *local_checks(), *tool_checks()]
    if not args.offline:
        rows += hub_checks(args)
    failed = any(r["state"] == "fail" for r in rows)
    if args.json:
        print_json({"ok": not failed, "checks": rows})
    else:
        console.print(table(["check", "state", "detail"], [(r["check"], WORDS[r["state"]], r["detail"]) for r in rows]))
    return EXIT_ERROR if failed else 0


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("doctor", help="check the hub, paths, services, tools, plugins and nodes")
    p.add_argument("--offline", action="store_true", help="skip the checks that need the hub")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_doctor)
