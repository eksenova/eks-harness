from __future__ import annotations

import json
import socket
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from eks_harness import __version__
from eks_harness._version import compute_source_hash, source_root
from eks_harness.cli import COMMAND_MODULES, main
from eks_harness.cli import mcp_cmd
from eks_harness.cli.client import Gone, HarnessClient
from eks_harness.cli.ui import (
    EXIT_DAEMON_DOWN,
    EXIT_GONE,
    EXIT_OK,
    EXIT_UNAUTHORIZED,
    EXIT_USAGE,
)
from eks_harness.config import Config

from conftest import make_app
from conftest import harness_client

TOOL_COUNT = 18


def run_cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture
def app_client(config: Config) -> Iterator[TestClient]:
    with TestClient(make_app(config)) as test_client:
        yield test_client


def route_client(monkeypatch: pytest.MonkeyPatch, test_client: TestClient, config: Config) -> None:
    def factory(args) -> HarnessClient:
        return harness_client(test_client, config, getattr(args, "api_key", None))
    monkeypatch.setattr(mcp_cmd, "client_from_args", factory)


def test_version_json_reports_version_and_source_hash(capsys) -> None:
    code, out, _ = run_cli(capsys, "version", "--json")
    assert code == EXIT_OK
    info = json.loads(out)
    assert info["version"] == __version__
    root = source_root()
    if root is not None:
        assert info["editable"] is True
        assert info["sourceHash"] == compute_source_hash(root)
    else:
        assert info["sourceHash"]
    assert info["python"] and info["platform"]


def test_global_json_flag_matches_command_flag(capsys) -> None:
    _, before, _ = run_cli(capsys, "--json", "version")
    _, after, _ = run_cli(capsys, "version", "--json")
    first, second = json.loads(before), json.loads(after)
    assert first["sourceHash"] == second["sourceHash"] and first["version"] == second["version"]


def test_version_human_output(capsys) -> None:
    code, out, _ = run_cli(capsys, "version")
    assert code == EXIT_OK
    assert __version__ in out and "Source hash" in out


def test_version_flag(capsys) -> None:
    code, out, _ = run_cli(capsys, "-V")
    assert code == EXIT_OK
    assert out.strip() == f"eks-harness {__version__}"


def test_no_command_prints_help_to_stderr_and_exits_usage(capsys) -> None:
    code, out, err = run_cli(capsys)
    assert code == EXIT_USAGE
    assert out == ""
    assert "usage: eks-harness" in err
    assert "Exit codes:" in err


def test_help_flag_exits_zero(capsys) -> None:
    code, out, _ = run_cli(capsys, "--help")
    assert code == EXIT_OK
    assert "--json" in out and "Exit codes:" in out


def test_unknown_command_exits_usage(capsys) -> None:
    code, _, err = run_cli(capsys, "no-such-command")
    assert code == EXIT_USAGE
    assert "invalid choice" in err


def test_help_command_shows_subcommand_help(capsys) -> None:
    code, out, _ = run_cli(capsys, "help", "mcp")
    assert code == EXIT_OK
    assert out.startswith("usage: eks-harness mcp")
    code, out, _ = run_cli(capsys, "help")
    assert code == EXIT_OK and out.startswith("usage: eks-harness")


def test_help_for_unknown_topic_exits_usage(capsys) -> None:
    code, out, err = run_cli(capsys, "help", "nope")
    assert code == EXIT_USAGE
    assert "has no command 'nope'" in err
    assert out == ""


def test_global_json_on_command_without_json_output_is_usage_error(capsys) -> None:
    code, out, err = run_cli(capsys, "--json", "help")
    assert code == EXIT_USAGE
    assert "has no JSON output" in err
    problem = json.loads(out)
    assert problem == {"error": "usage", "message": "'help' has no JSON output; drop --json", "exitCode": EXIT_USAGE}


def test_every_command_module_registers() -> None:
    from eks_harness.cli import build_parser
    parser = build_parser()
    assert parser.prog == "eks-harness"
    assert "mcp_cmd" in COMMAND_MODULES and "version_cmd" in COMMAND_MODULES


def test_mcp_list_tools_json(capsys) -> None:
    code, out, _ = run_cli(capsys, "mcp", "--list-tools", "--json")
    assert code == EXIT_OK
    tools = json.loads(out)["tools"]
    assert len(tools) >= TOOL_COUNT
    names = {tool["name"] for tool in tools}
    assert {"list_projects", "upload_artifact", "lease_status"} <= names
    assert all(tool["inputSchema"]["type"] == "object" for tool in tools)


def test_mcp_list_tools_human(capsys) -> None:
    code, out, _ = run_cli(capsys, "mcp", "--list-tools")
    assert code == EXIT_OK
    assert "upload_artifact" in out and "session_timeline" in out


def test_mcp_check_daemon_down(capsys) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    code, out, err = run_cli(capsys, "--url", f"http://127.0.0.1:{port}", "mcp", "--check", "--json")
    assert code == EXIT_DAEMON_DOWN
    result = json.loads(out)
    assert result["daemon"] is False and result["error"] == "daemon_unreachable"
    assert "not reachable" in err


def test_mcp_check_against_test_app_without_auth(capsys, monkeypatch, app_client, config) -> None:
    route_client(monkeypatch, app_client, config)
    code, out, _ = run_cli(capsys, "mcp", "--check", "--json")
    assert code == EXIT_OK
    result = json.loads(out)
    assert result["daemon"] is True and result["authenticated"] is True
    assert result["username"] == "local" and result["authEnabled"] is False


def test_mcp_check_with_auth(capsys, monkeypatch, auth_env, auth_config) -> None:
    route_client(monkeypatch, auth_env.client, auth_config)
    code, out, err = run_cli(capsys, "mcp", "--check", "--json")
    assert code == EXIT_UNAUTHORIZED
    assert json.loads(out)["authenticated"] is False
    code, out, _ = run_cli(capsys, "--api-key", auth_env.admin_key, "mcp", "--check", "--json")
    assert code == EXIT_OK
    result = json.loads(out)
    assert result["username"] == "admin" and result["role"] == "admin" and result["via"] == "key"
    code, out, _ = run_cli(capsys, "--api-key", auth_env.admin_key, "mcp", "--check")
    assert code == EXIT_OK and "admin" in out


def test_api_errors_map_to_exit_codes_and_json(capsys, monkeypatch) -> None:
    def gone(args) -> int:
        raise Gone(410, "lease_released", "Lease k7m2qx was released.",
                   {"error": "lease_released", "message": "Lease k7m2qx was released.",
                    "reacquire": "eks-harness lease acquire --project a/b --session s --kind browser"})
    monkeypatch.setattr(mcp_cmd, "_check", gone)
    code, out, err = run_cli(capsys, "--json", "mcp", "--check")
    assert code == EXIT_GONE
    problem = json.loads(out)
    assert problem["error"] == "lease_released" and problem["status"] == 410 and problem["exitCode"] == EXIT_GONE
    assert problem["reacquire"].startswith("eks-harness lease acquire")
    assert "released" in err
    code, out, _ = run_cli(capsys, "mcp", "--check")
    assert code == EXIT_GONE and out == ""


def test_unexpected_errors_exit_one(capsys, monkeypatch) -> None:
    def boom(args) -> int:
        raise RuntimeError("kaput")
    monkeypatch.setattr(mcp_cmd, "_list_tools", boom)
    code, _, err = run_cli(capsys, "mcp", "--list-tools")
    assert code == 1 and "RuntimeError: kaput" in err
    with pytest.raises(RuntimeError):
        main(["--debug", "mcp", "--list-tools"])


def test_interrupt_exits_130(capsys, monkeypatch) -> None:
    def interrupted(args) -> int:
        raise KeyboardInterrupt
    monkeypatch.setattr(mcp_cmd, "_list_tools", interrupted)
    code, _, _ = run_cli(capsys, "mcp", "--list-tools")
    assert code == 130


def test_module_entry_point_without_terminal(harness_home: Path) -> None:
    completed = subprocess.run([sys.executable, "-m", "eks_harness.cli", "version", "--json"],
                               capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
    assert completed.returncode == EXIT_OK, completed.stderr
    assert json.loads(completed.stdout)["version"] == __version__
    bare = subprocess.run([sys.executable, "-m", "eks_harness.cli"], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=60)
    assert bare.returncode == EXIT_USAGE and bare.stdout == ""
