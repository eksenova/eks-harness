from __future__ import annotations

import argparse
import sys
from typing import Any

from eks_harness.cli.client import ApiClientError, HarnessClient, client_from_args
from eks_harness.cli.ui import EXIT_DAEMON_DOWN, EXIT_OK, console, err, info, interactive, kv_panel, print_json, table


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "mcp", help="run the MCP server on stdio (tools over the daemon's HTTP API)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Run a Model Context Protocol server on stdin/stdout. Every tool calls the daemon's HTTP API\n"
                    "with the CLI's credentials (eks-harness login, or EKS_HARNESS_API_KEY).\n\n"
                    "Register it with an MCP client, for example:\n"
                    "  claude mcp add eks-harness -- eks-harness mcp\n\n"
                    "--check verifies the daemon and the credentials and exits; --list-tools prints the tools.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="check that the daemon is reachable and the key works")
    mode.add_argument("--list-tools", action="store_true", help="list the MCP tools and exit")
    parser.add_argument("--json", action="store_true", help="print JSON (with --check or --list-tools)")
    parser.set_defaults(func=run)


def _client_factory(args: argparse.Namespace):
    def factory() -> HarnessClient:
        return client_from_args(args)
    return factory


def run(args: argparse.Namespace) -> int:
    if args.check:
        return _check(args)
    if args.list_tools:
        return _list_tools(args)
    from eks_harness.mcp_server import run_stdio
    if interactive():
        info("eks-harness mcp speaks MCP on stdin/stdout and is meant to be started by an MCP client; "
             "press Ctrl+C to stop.")
    run_stdio(_client_factory(args))
    return EXIT_OK


def _list_tools(args: argparse.Namespace) -> int:
    import anyio

    from eks_harness.mcp_server import create_server

    server, _ = create_server(_client_factory(args))
    listed = anyio.run(server.list_tools)
    rows = [{"name": tool.name, "description": tool.description or "",
             "readOnly": bool(tool.annotations and tool.annotations.read_only_hint),
             "inputSchema": tool.input_schema} for tool in listed]
    if args.json:
        print_json({"tools": rows})
        return EXIT_OK
    console.print(table(["Tool", ("Read only", {"justify": "center"}), "Description"],
                        [(r["name"], "yes" if r["readOnly"] else "no", r["description"]) for r in rows],
                        title="MCP tools"))
    return EXIT_OK


def _check(args: argparse.Namespace) -> int:
    client = client_from_args(args)
    result: dict[str, Any] = {"url": client.base_url, "keySource": client.key_source, "daemon": False,
                              "authenticated": False}
    try:
        if not client.is_up():
            result["error"] = "daemon_unreachable"
            result["message"] = (f"The eks-harness daemon is not reachable at {client.base_url}: "
                                 f"start it with 'eks-harness daemon start'.")
            _report_check(args, result)
            return EXIT_DAEMON_DOWN
        result["daemon"] = True
        try:
            me = client.get("/api/auth/me")
        except ApiClientError as problem:
            result["error"] = problem.error
            result["message"] = problem.message
            _report_check(args, result)
            return problem.exit_code
        user = me.get("user") or {}
        result.update({"authenticated": True, "username": user.get("username"), "role": user.get("role"),
                       "via": me.get("via"), "authEnabled": me.get("authEnabled")})
        _report_check(args, result)
        return EXIT_OK
    finally:
        client.close()


def _report_check(args: argparse.Namespace, result: dict[str, Any]) -> None:
    if args.json:
        print_json(result)
        if result.get("message"):
            sys.stderr.write(result["message"] + "\n")
        return
    rows = [("Daemon", result["url"] + (" (reachable)" if result["daemon"] else " (not reachable)")),
            ("Credentials", {"env": "EKS_HARNESS_API_KEY", "file": "saved login", "explicit": "--api-key",
                             "none": "none"}.get(result["keySource"], result["keySource"]))]
    if result["authenticated"]:
        rows.append(("User", f"{result.get('username')} ({result.get('role')}, via {result.get('via')})"))
        rows.append(("Auth", "enabled" if result.get("authEnabled") else "disabled"))
    target = console if result["authenticated"] else err
    target.print(kv_panel("eks-harness mcp", rows))
    if result.get("message"):
        sys.stderr.write(result["message"] + "\n")
