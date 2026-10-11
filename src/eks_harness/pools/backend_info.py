from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from eks_harness.durations import (
    NEVER,
    describe_duration,
    duration_seconds,
    parse_duration,
)

__all__ = ["NEVER", "describe_duration", "duration_seconds", "parse_duration"]

REDACTED = "<redacted>"
SECRET_MARK = "$secret"
INFO_KEYS = ("url", "urls", "ports", "credentials", "env", "secretEnv", "commit", "branch", "version", "worktree",
             "extra")


def params_digest(params: dict[str, Any] | None) -> str:
    if not params:
        return ""
    return hashlib.sha1(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()[:8]


def backend_identity(tree: str | None, key: str | None = None, params: dict[str, Any] | None = None) -> str:
    base = key.strip() if key and key.strip() else (f"tree:{Path(tree).expanduser().resolve()}" if tree else "shared")
    digest = params_digest(params)
    return f"{base}#{digest}" if digest else base


def _secret_value(value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {SECRET_MARK}:
        return value[SECRET_MARK]
    if isinstance(value, dict):
        return {k: _secret_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_secret_value(v) for v in value]
    return value


def _redact_marked(value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {SECRET_MARK}:
        return REDACTED
    if isinstance(value, dict):
        return {k: _redact_marked(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_marked(v) for v in value]
    return value


def _redact_all(value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {SECRET_MARK}:
        return REDACTED
    if isinstance(value, dict):
        return {k: _redact_all(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_all(v) for v in value]
    if value is None or value == "":
        return value
    return REDACTED


def normalize_info(raw: Any) -> dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    ports: dict[str, int] = {}
    for name, port in (data.get("ports") or {}).items():
        try:
            ports[str(name)] = int(port)
        except (TypeError, ValueError):
            continue
    secret_env = data.get("secretEnv", data.get("secret_env")) or []
    return {
        "url": str(data.get("url") or ""),
        "urls": {str(k): str(v) for k, v in (data.get("urls") or {}).items()},
        "ports": ports,
        "credentials": dict(data.get("credentials") or {}),
        "env": {str(k): v if isinstance(v, dict) else str(v) for k, v in (data.get("env") or {}).items()},
        "secretEnv": sorted({str(k) for k in secret_env}),
        "commit": str(data.get("commit") or ""),
        "branch": str(data.get("branch") or ""),
        "version": str(data.get("version") or ""),
        "worktree": str(data.get("worktree") or ""),
        "extra": dict(data.get("extra") or {}),
    }


def merge_info(*layers: Any) -> dict[str, Any]:
    merged = normalize_info({})
    for layer in layers:
        if not layer:
            continue
        info = normalize_info(layer)
        for key in ("url", "commit", "branch", "version", "worktree"):
            if info[key]:
                merged[key] = info[key]
        for key in ("urls", "ports", "credentials", "env", "extra"):
            merged[key] = {**merged[key], **info[key]}
        merged["secretEnv"] = sorted(set(merged["secretEnv"]) | set(info["secretEnv"]))
    if not merged["url"] and merged["urls"]:
        merged["url"] = next(iter(merged["urls"].values()))
    return merged


def reveal_info(info: dict[str, Any]) -> dict[str, Any]:
    full = normalize_info(info)
    full["credentials"] = _secret_value(full["credentials"])
    full["env"] = {k: str(_secret_value(v)) for k, v in full["env"].items()}
    full["extra"] = _secret_value(full["extra"])
    full["redacted"] = False
    return full


def redact_info(info: dict[str, Any]) -> dict[str, Any]:
    out = normalize_info(info)
    out["credentials"] = _redact_all(out["credentials"])
    out["env"] = {k: REDACTED if k in out["secretEnv"] or isinstance(v, dict) else v for k, v in out["env"].items()}
    out["extra"] = _redact_marked(out["extra"])
    out["redacted"] = True
    return out


def secret_strings(info: dict[str, Any]) -> list[str]:
    found: list[str] = []

    def walk(value: Any, secret: bool) -> None:
        if isinstance(value, dict):
            if set(value) == {SECRET_MARK}:
                walk(value[SECRET_MARK], True)
                return
            for item in value.values():
                walk(item, secret)
        elif isinstance(value, list):
            for item in value:
                walk(item, secret)
        elif secret and value not in (None, "") and len(str(value)) >= 4:
            found.append(str(value))

    data = normalize_info(info)
    walk(data["credentials"], True)
    walk(data["extra"], False)
    for key, value in data["env"].items():
        walk(value, key in data["secretEnv"])
    return found


def scrub(text: str, secrets: list[str]) -> str:
    for secret in sorted(set(secrets), key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    return text


def git_facts(tree: str | None) -> dict[str, str]:
    if not tree or not Path(tree).is_dir():
        return {}

    def git(*args: str) -> str:
        try:
            out = subprocess.run(["git", "-C", tree, *args], capture_output=True, text=True, timeout=10,
                                 check=False)
        except (OSError, subprocess.SubprocessError):
            return ""
        return out.stdout.strip() if out.returncode == 0 else ""

    commit = git("rev-parse", "HEAD")
    if not commit:
        return {"worktree": str(tree)}
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    dirty = bool(git("status", "--porcelain", "--untracked-files=no"))
    return {"commit": commit + ("-dirty" if dirty else ""), "branch": "" if branch == "HEAD" else branch,
            "worktree": git("rev-parse", "--show-toplevel") or str(tree)}


def default_info(ports: dict[str, Any], exports: dict[str, Any], tree: str | None) -> dict[str, Any]:
    urls = {name: f"http://127.0.0.1:{port}" for name, port in (ports or {}).items()}
    url = next((str(v) for k, v in (exports or {}).items() if str(k).upper().endswith("URL")
                and str(v).startswith(("http://", "https://"))), "")
    return {"url": url or next(iter(urls.values()), ""), "urls": urls, "ports": dict(ports or {}),
            "env": {str(k): str(v) for k, v in (exports or {}).items()}, **git_facts(tree)}


def resolve_template(value: Any, ports: dict[str, Any], captured: dict[str, Any]) -> Any:
    if isinstance(value, str):
        text = value
        for name, port in (ports or {}).items():
            text = text.replace("{ports." + name + "}", str(port))
        for name, found in (captured or {}).items():
            text = text.replace("{captured." + name + "}", str(found))
        return text
    if isinstance(value, dict):
        return {k: resolve_template(v, ports, captured) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_template(v, ports, captured) for v in value]
    return value
