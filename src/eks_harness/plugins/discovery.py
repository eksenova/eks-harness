from __future__ import annotations

import hashlib
import importlib
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path

from eks_harness.plugins.manifest import MANIFEST_NAME, Manifest, ManifestError, load_manifest

ENTRY_POINT_GROUP = "eks_harness.plugins"
BUILTIN_ROOT = Path(__file__).resolve().parent.parent / "builtin_plugins"
TRUSTED_KINDS = frozenset({"builtin", "package"})
_LOG = logging.getLogger(__name__)
_GITHUB = re.compile(r"github:(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)")


@dataclass(frozen=True)
class Candidate:
    kind: str
    origin: str
    root: Path
    manifest: Manifest | None = None
    error: str | None = None
    tree: Path | None = None

    @property
    def trusted_by_kind(self) -> bool:
        return self.kind in TRUSTED_KINDS


@dataclass(frozen=True)
class GitSpec:
    url: str
    subpath: str = ""
    ref: str = ""

    @property
    def cache_key(self) -> str:
        return hashlib.sha256(f"{self.url}@{self.ref}".encode()).hexdigest()[:16]

    def __str__(self) -> str:
        text = self.url
        if self.subpath or self.ref:
            text += "#" + self.subpath + (f"@{self.ref}" if self.ref else "")
        return text


def parse_git_spec(spec: str) -> GitSpec:
    url, _, fragment = spec.partition("#")
    subpath, _, ref = fragment.partition("@")
    match = _GITHUB.fullmatch(url)
    if match:
        url = f"https://github.com/{match['repo']}.git"
    if not url:
        raise ValueError(f"'{spec}' is not a git plugin spec (url[#subpath][@ref])")
    if ".." in Path(subpath).parts:
        raise ValueError(f"'{spec}': subpath may not leave the repository")
    return GitSpec(url=url, subpath=subpath.strip("/"), ref=ref)


def git_checkout_dir(cache_dir: Path, spec: GitSpec) -> Path:
    return cache_dir / "plugins" / "git" / spec.cache_key


def fetch_git(cache_dir: Path, spec: GitSpec, *, update: bool = False) -> Path:
    target = git_checkout_dir(cache_dir, spec)
    if target.exists() and not update:
        return target / spec.subpath if spec.subpath else target
    staging = target.with_name(target.name + ".tmp")
    if staging.exists():
        shutil.rmtree(staging)
    args = ["git", "clone", "--depth", "1"]
    if spec.ref:
        args += ["--branch", spec.ref]
    args += [spec.url, str(staging)]
    out = subprocess.run(args, capture_output=True, text=True, timeout=600)
    if out.returncode != 0 and spec.ref:
        shutil.rmtree(staging, ignore_errors=True)
        out = subprocess.run(["git", "clone", spec.url, str(staging)], capture_output=True, text=True, timeout=600)
        if out.returncode == 0:
            out = subprocess.run(["git", "-C", str(staging), "checkout", spec.ref], capture_output=True, text=True,
                                 timeout=120)
    if out.returncode != 0:
        shutil.rmtree(staging, ignore_errors=True)
        raise RuntimeError(f"git fetch of {spec} failed: {(out.stderr or out.stdout).strip()}")
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging.rename(target)
    return target / spec.subpath if spec.subpath else target


def _candidate(kind: str, origin: str, root: Path, tree: Path | None = None) -> Candidate:
    try:
        manifest = load_manifest(root)
    except ManifestError as error:
        return Candidate(kind=kind, origin=origin, root=root.resolve(), error=str(error), tree=tree)
    return Candidate(kind=kind, origin=origin, root=manifest.root, manifest=manifest, tree=tree)


def plugin_dirs(path: Path) -> list[Path]:
    if (path / MANIFEST_NAME).is_file():
        return [path]
    if not path.is_dir():
        return []
    return sorted(p for p in path.iterdir() if (p / MANIFEST_NAME).is_file())


def builtin_candidates(root: Path = BUILTIN_ROOT) -> list[Candidate]:
    return [_candidate("builtin", f"builtin:{p.name}", p) for p in plugin_dirs(root)]


def package_candidates() -> list[Candidate]:
    found: list[Candidate] = []
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        origin = f"package:{ep.value}"
        try:
            target = ep.load()
        except Exception as error:
            found.append(Candidate(kind="package", origin=origin, root=Path("."), error=f"{ep.name}: {error}"))
            continue
        if callable(target) and not isinstance(target, type):
            target = target()
        if isinstance(target, str | Path):
            root = Path(target)
        else:
            module = target if hasattr(target, "__file__") else importlib.import_module(ep.module)
            root = Path(module.__file__).resolve().parent
        found.append(_candidate("package", origin, root))
    return found


def path_candidates(paths: list[Path], kind: str = "path", tree: Path | None = None) -> list[Candidate]:
    found: list[Candidate] = []
    for path in paths:
        path = path.expanduser()
        dirs = plugin_dirs(path)
        if not dirs and kind == "path":
            found.append(Candidate(kind=kind, origin=f"{kind}:{path}", root=path,
                                   error=f"{path} holds no {MANIFEST_NAME}", tree=tree))
        found.extend(_candidate(kind, f"{kind}:{p}", p, tree) for p in dirs)
    return found


def repo_candidates(tree: Path, extra_paths: tuple[Path, ...] = ()) -> list[Candidate]:
    found = path_candidates([tree / ".harness" / "plugins"], kind="repo", tree=tree)
    found.extend(path_candidates(list(extra_paths), kind="repo", tree=tree))
    return found


def git_candidates(cache_dir: Path, specs: list[str], tree: Path | None = None) -> list[Candidate]:
    found: list[Candidate] = []
    for text in specs:
        try:
            spec = parse_git_spec(text)
        except ValueError as error:
            found.append(Candidate(kind="git", origin=f"git:{text}", root=Path("."), error=str(error), tree=tree))
            continue
        checkout = git_checkout_dir(cache_dir, spec)
        root = checkout / spec.subpath if spec.subpath else checkout
        if not checkout.exists():
            found.append(Candidate(kind="git", origin=f"git:{spec}", root=root, tree=tree,
                                   error="not fetched yet (run: eks-harness plugin sync)"))
            continue
        dirs = plugin_dirs(root)
        if not dirs:
            found.append(Candidate(kind="git", origin=f"git:{spec}", root=root, tree=tree,
                                   error=f"{root} holds no {MANIFEST_NAME}"))
        found.extend(_candidate("git", f"git:{spec}", p, tree) for p in dirs)
    return found
