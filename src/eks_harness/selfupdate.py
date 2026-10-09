from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from importlib import metadata
from pathlib import Path

from eks_harness._version import __version__, build_info
from eks_harness.paths import Paths

DIST = "eks-harness"
DEFAULT_REPOSITORY = "https://github.com/eksenova/eks-harness.git"
SHA = re.compile(r"[0-9a-f]{40}")
GIT_TIMEOUT = 60
INSTALL_TIMEOUT = 1800
STATE_FILE = "update.json"
LOCK_FILE = "update.lock"


class UpdateError(RuntimeError):
    pass


@dataclass
class Installed:
    version: str
    commit: str | None
    dirty: bool
    source: str
    location: str | None
    extras: list[str]
    receipt: str | None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Status:
    installed: Installed
    repository: str
    ref: str
    latest: str | None
    available: bool
    reason: str
    changes: list[dict] = field(default_factory=list)
    checked_at: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return {"installed": self.installed.as_dict(), "repository": self.repository, "ref": self.ref,
                "latest": self.latest, "available": self.available, "reason": self.reason,
                "changes": self.changes, "checkedAt": self.checked_at}


def _git_env() -> dict[str, str]:
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}


def tool_dir() -> Path:
    return Path(sys.prefix)


def receipt_path() -> Path | None:
    path = tool_dir() / "uv-receipt.toml"
    return path if path.is_file() else None


def _requirement(receipt: Path | None) -> dict:
    if receipt is None:
        return {}
    try:
        data = tomllib.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    for item in data.get("tool", {}).get("requirements", []):
        if isinstance(item, dict) and item.get("name") == DIST:
            return item
    return {}


def _direct_url() -> dict:
    try:
        raw = metadata.distribution(DIST).read_text("direct_url.json")
    except metadata.PackageNotFoundError:
        return {}
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}


def _head(root: Path) -> str | None:
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, timeout=10,
                                check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def installed() -> Installed:
    info = build_info()
    receipt = receipt_path()
    requirement = _requirement(receipt)
    direct = _direct_url()
    vcs = direct.get("vcs_info") or {}
    commit = vcs.get("commit_id") or info.get("gitCommit")
    if not commit and info.get("sourceRoot"):
        commit = _head(Path(info["sourceRoot"]))
    if info.get("editable"):
        source, location = "editable", info.get("sourceRoot")
    elif vcs:
        source, location = "git", direct.get("url")
    elif "dir_info" in direct:
        source, location = "directory", direct.get("url")
    elif direct.get("url"):
        source, location = "archive", direct.get("url")
    else:
        source, location = "index", None
    return Installed(version=info.get("version") or __version__, commit=commit,
                     dirty=bool(info.get("gitDirty")) if source != "git" else False, source=source,
                     location=location, extras=list(requirement.get("extras") or []),
                     receipt=str(receipt) if receipt else None)


def _github(repository: str) -> tuple[str, str] | None:
    match = re.search(r"github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?/?$", repository)
    return (match.group(1), match.group(2)) if match else None


def remote_commit(repository: str, ref: str) -> str:
    if SHA.fullmatch(ref):
        return ref
    git = shutil.which("git")
    errors = []
    if git:
        refs = [ref] if ref.startswith("refs/") else [f"refs/heads/{ref}", f"refs/tags/{ref}^{{}}", f"refs/tags/{ref}"]
        try:
            result = subprocess.run([git, "ls-remote", repository, *refs], capture_output=True, text=True,
                                    timeout=GIT_TIMEOUT, env=_git_env(), check=False)
        except (OSError, subprocess.SubprocessError) as problem:
            errors.append(str(problem))
        else:
            if result.returncode == 0:
                found = dict(reversed(line.split("\t", 1)) for line in result.stdout.splitlines() if "\t" in line)
                for name in refs:
                    if found.get(name):
                        return found[name]
                errors.append(f"no branch or tag {ref}")
            else:
                errors.append(result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "git ls-remote failed")
    repo = _github(repository)
    gh = shutil.which("gh")
    if repo and gh:
        try:
            result = subprocess.run([gh, "api", f"repos/{repo[0]}/{repo[1]}/commits/{ref}", "--jq", ".sha"],
                                    capture_output=True, text=True, timeout=GIT_TIMEOUT, check=False)
        except (OSError, subprocess.SubprocessError) as problem:
            errors.append(str(problem))
        else:
            sha = result.stdout.strip()
            if result.returncode == 0 and SHA.fullmatch(sha):
                return sha
            errors.append(result.stderr.strip() or "gh api failed")
    raise UpdateError(f"could not read {ref} of {repository}: {'; '.join(errors) or 'git is not on PATH'}")


def changes(repository: str, base: str | None, head: str, limit: int = 50) -> list[dict]:
    repo = _github(repository)
    gh = shutil.which("gh")
    if not repo or not gh or not base or base == head:
        return []
    try:
        query = ("[.commits[] | {sha: .sha, title: (.commit.message | split(\"\\n\")[0]), "
                 "date: .commit.author.date}]")
        result = subprocess.run([gh, "api", f"repos/{repo[0]}/{repo[1]}/compare/{base}...{head}", "--jq", query],
                                capture_output=True, text=True, timeout=GIT_TIMEOUT, check=False)
        items = json.loads(result.stdout) if result.returncode == 0 else []
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return []
    return list(reversed(items))[:limit]


def check(repository: str, ref: str, *, with_changes: bool = True) -> Status:
    current = installed()
    latest = remote_commit(repository, ref)
    if current.commit == latest and not current.dirty and current.source == "git":
        return Status(current, repository, ref, latest, False, "up to date")
    if current.source in ("editable",):
        reason = "editable install from a source tree; update it with git"
        return Status(current, repository, ref, latest, False, reason)
    if current.commit == latest and not current.dirty:
        reason = f"built from {latest[:12]} ({current.source}); installing from GitHub switches the source"
        return Status(current, repository, ref, latest, False, reason)
    found = changes(repository, current.commit, latest) if with_changes else []
    reason = (f"{current.commit[:12]} -> {latest[:12]}" if current.commit else f"unknown -> {latest[:12]}")
    return Status(current, repository, ref, latest, True, reason, found)


def uv_binary() -> str:
    candidates = [shutil.which("uv"), str(Path.home() / ".local" / "bin" / "uv"), "/opt/homebrew/bin/uv",
                  "/usr/local/bin/uv", str(Path.home() / ".cargo" / "bin" / "uv")]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise UpdateError("uv is not installed or not on PATH (https://docs.astral.sh/uv/)")


def requirement(repository: str, commit: str, extras: list[str]) -> str:
    url = repository if repository.startswith("git+") else f"git+{repository}"
    names = f"[{','.join(extras)}]" if extras else ""
    return f"{DIST}{names} @ {url}@{commit}"


def install_command(repository: str, commit: str, extras: list[str]) -> list[str]:
    return [uv_binary(), "tool", "install", "--force", "--reinstall", "--quiet", requirement(repository, commit, extras)]


def installed_commit_on_disk() -> str | None:
    for path in sorted(tool_dir().glob("lib/python*/site-packages/eks_harness-*.dist-info/direct_url.json")):
        try:
            return (json.loads(path.read_text(encoding="utf-8")).get("vcs_info") or {}).get("commit_id")
        except (OSError, json.JSONDecodeError):
            continue
    return None


def install(repository: str, commit: str, extras: list[str], *,
            output: Callable[[str], None] | None = None) -> str:
    command = install_command(repository, commit, extras)
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                               env={**_git_env(), "UV_NO_PROGRESS": "1"})
    tail: list[str] = []
    assert process.stdout is not None
    started = time.time()
    for line in process.stdout:
        line = line.rstrip()
        tail = [*tail[-40:], line]
        if output:
            output(line)
        if time.time() - started > INSTALL_TIMEOUT:
            process.kill()
            raise UpdateError(f"the install took longer than {INSTALL_TIMEOUT // 60} minutes")
    code = process.wait()
    if code != 0:
        raise UpdateError(f"uv tool install failed (exit {code}): " + " | ".join(tail[-8:]))
    landed = installed_commit_on_disk()
    if landed != commit:
        raise UpdateError(f"the install finished but reports commit {landed or 'unknown'}, not {commit}")
    return landed


@contextlib.contextmanager
def update_lock(paths: Paths) -> Iterator[None]:
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    with open(paths.state_dir / LOCK_FILE, "a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UpdateError("another update is running") from None
        yield


def read_state(paths: Paths) -> dict:
    try:
        data = json.loads((paths.state_dir / STATE_FILE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_state(paths: Paths, **values) -> dict:
    data = {**read_state(paths), **values}
    path = paths.state_dir / STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(temp, path)
    return data
