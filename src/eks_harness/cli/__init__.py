from __future__ import annotations

import argparse
import importlib
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from eks_harness import __version__
from eks_harness.cli.client import ApiClientError
from eks_harness.cli.ui import (
    EXIT_CONFLICT,
    EXIT_DAEMON_DOWN,
    EXIT_ERROR,
    EXIT_FORBIDDEN,
    EXIT_GONE,
    EXIT_INTERRUPTED,
    EXIT_NO_CREDENTIALS,
    EXIT_NOT_FOUND,
    EXIT_NOTHING_SELECTED,
    EXIT_OK,
    EXIT_UNAUTHORIZED,
    EXIT_USAGE,
    CliExit,
    error,
    info,
    print_json,
    warn,
)
from eks_harness.config import ConfigError

COMMAND_MODULES = (
    "version_cmd",
    "setup_cmd",
    "auth_cmds",
    "daemon_cmds",
    "config_cmds",
    "store_cmds",
    "annotate_cmds",
    "lease_cmds",
    "pool_cmds",
    "backend_cmds",
    "mcp_cmd",
    "plugin_cmds",
    "node_cmds",
    "video_cmds",
    "studio_cmds",
    "score_cmds",
    "queue_cmds",
    "flow_cmds",
    "driver_cmds",
    "doctor_cmd",
    "migrate_cmds",
    "update_cmds",
)

DESCRIPTION = """eks-harness: browser and device pools, backends, leases, artifact store, video studio and web UI.

Every command talks to the running daemon over its HTTP API, except setup, auth recover,
daemon install/uninstall/start/stop, update and version, which work on local files.

Output: tables and panels for people; --json (before or after the command) prints plain JSON
on stdout for scripts and agents. In JSON mode a failure is also printed on stdout, as
{"error", "message", "exitCode", ...}, next to the human message on stderr.

No terminal: commands never prompt without one. A missing required value exits with a
message naming the flag to pass, and confirmations need --yes.

Exit codes: 0 ok, 1 error, 2 usage, 3 daemon unreachable, 4 not logged in, 5 forbidden,
6 not found, 7 gone (released sid), 8 conflict, 10 no credentials and no terminal,
11 nothing selected or cancelled, 130 interrupted.

Run 'eks-harness help <command> [<subcommand>]' for the help of one command."""

EXIT_NAMES = {
    EXIT_ERROR: "error",
    EXIT_USAGE: "usage",
    EXIT_DAEMON_DOWN: "daemon_unreachable",
    EXIT_UNAUTHORIZED: "not_authenticated",
    EXIT_FORBIDDEN: "forbidden",
    EXIT_NOT_FOUND: "not_found",
    EXIT_GONE: "gone",
    EXIT_CONFLICT: "conflict",
    EXIT_NO_CREDENTIALS: "no_credentials",
    EXIT_NOTHING_SELECTED: "nothing_selected",
    EXIT_INTERRUPTED: "interrupted",
}

JSON_DEST = "json"
GLOBAL_JSON_DEST = "global_json"


class HelpFormatter(argparse.RawDescriptionHelpFormatter):
    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="eks-harness", description=DESCRIPTION, formatter_class=HelpFormatter)
    parser.add_argument("--url", help="daemon base URL (default: from config, or EKS_HARNESS_URL)")
    parser.add_argument("--api-key", dest="api_key", help=argparse.SUPPRESS)
    parser.add_argument("--json", dest=GLOBAL_JSON_DEST, action="store_true",
                        help="print JSON for the command (same as the command's own --json)")
    parser.add_argument("--debug", action="store_true", help="show tracebacks and debug logging")
    parser.add_argument("-V", "--version", action="version", version=f"eks-harness {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    help_parser = subparsers.add_parser("help", help="show the help of a command",
                                        description="Show the help of a command or subcommand.")
    help_parser.add_argument("topic", nargs="*", help="command path, for example: lease acquire")
    help_parser.set_defaults(func=lambda args: show_help(parser, args.topic))
    for name in COMMAND_MODULES:
        module = importlib.import_module(f"eks_harness.cli.{name}")
        module.register(subparsers)
    register_plugin_commands(subparsers)
    return parser


def register_plugin_commands(subparsers: argparse._SubParsersAction) -> list[str]:
    if os.environ.get("EKS_HARNESS_NO_PLUGIN_CLI"):
        return []
    try:
        from eks_harness.config import load as load_config
        from eks_harness.paths import resolve_paths
        from eks_harness.plugins import PluginHost, find_tree

        paths = resolve_paths()
        host = PluginHost(paths, load_config(paths))
        tree = find_tree(Path.cwd())
        contributions = host.contributions("cli", tree)
    except Exception as problem:
        logging.getLogger("eks_harness.cli").debug("plugin commands unavailable: %s", problem)
        return []
    loaded = []
    for contribution in contributions:
        if contribution.id in subparsers.choices:
            continue
        try:
            host.load(contribution, tree).register(subparsers)
            loaded.append(contribution.key)
        except Exception as problem:
            logging.getLogger("eks_harness.cli").debug("plugin command %s failed: %s", contribution.key, problem)
    return loaded


def _subcommands(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def show_help(parser: argparse.ArgumentParser, topic: Sequence[str]) -> int:
    current = parser
    walked: list[str] = []
    for word in topic:
        children = _subcommands(current)
        if word not in children:
            where = " ".join(["eks-harness", *walked])
            known = ", ".join(sorted(children)) or "none"
            raise CliExit(EXIT_USAGE, f"'{where}' has no command '{word}'. Commands: {known}")
        current = children[word]
        walked.append(word)
    sys.stdout.write(current.format_help())
    sys.stdout.flush()
    return EXIT_OK


def wants_json(args: argparse.Namespace | None) -> bool:
    if args is None:
        return False
    return bool(getattr(args, GLOBAL_JSON_DEST, False) or getattr(args, JSON_DEST, False))


def apply_global_json(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if not getattr(args, GLOBAL_JSON_DEST, False):
        return
    if hasattr(args, JSON_DEST):
        setattr(args, JSON_DEST, True)
        return
    command = getattr(args, "command", None) or "this command"
    raise CliExit(EXIT_USAGE, f"'{command}' has no JSON output; drop --json")


def report_failure(args: argparse.Namespace | None, code: int, message: str, error_name: str,
                   extra: dict[str, Any] | None = None) -> int:
    if message:
        error(message)
    if wants_json(args):
        payload: dict[str, Any] = dict(extra or {})
        payload.update({"error": error_name, "message": message, "exitCode": code})
        print_json(payload)
    return code


def _parse(parser: argparse.ArgumentParser, argv: Sequence[str] | None) -> argparse.Namespace | int:
    try:
        return parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exit_:
        code = exit_.code
        if code is None:
            return EXIT_OK
        return code if isinstance(code, int) else EXIT_USAGE


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    parsed = _parse(parser, argv)
    if isinstance(parsed, int):
        return parsed
    args = parsed
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    handler = getattr(args, "func", None)
    if handler is None:
        parser.print_help(sys.stderr)
        return EXIT_USAGE
    try:
        apply_global_json(parser, args)
        result = handler(args)
        return int(result or 0)
    except KeyboardInterrupt:
        warn("interrupted")
        return EXIT_INTERRUPTED
    except BrokenPipeError:
        _silence_stdout()
        return EXIT_OK
    except CliExit as exit_:
        if exit_.code == EXIT_OK:
            if exit_.message:
                info(exit_.message)
            return EXIT_OK
        return report_failure(args, exit_.code, exit_.message, EXIT_NAMES.get(exit_.code, "error"))
    except ApiClientError as api_error:
        if args.debug:
            raise
        extra = {k: v for k, v in api_error.payload.items() if k not in ("error", "message")}
        extra["status"] = api_error.status
        return report_failure(args, api_error.exit_code, api_error.message, api_error.error, extra)
    except (ConfigError, ValueError) as problem:
        if args.debug:
            raise
        return report_failure(args, EXIT_USAGE, str(problem), "usage")
    except Exception as unexpected:
        if args.debug:
            raise
        return report_failure(args, EXIT_ERROR, f"{type(unexpected).__name__}: {unexpected}", "unexpected")


def _silence_stdout() -> None:
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except (OSError, ValueError, AttributeError):
        pass


if __name__ == "__main__":
    sys.exit(main())
