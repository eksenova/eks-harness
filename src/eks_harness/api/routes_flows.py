from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path, PurePosixPath
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from eks_harness.api.deps import current_principal, get_ctx
from eks_harness.api.errors import ApiError, bad_request, conflict, not_found, too_large
from eks_harness.api.routes_plans import _hub_tree, authorize_tree
from eks_harness.api.schemas_engine import (
    FlowEntryOut,
    FlowFileOut,
    FlowList,
    FlowSave,
    FlowSaved,
    FlowSource,
    PlanValidation,
)
from eks_harness.auth.core import Principal
from eks_harness.engine.build import INLINE_PREFIX, BuildRequest, EntryFile, EntryOut
from eks_harness.engine.errors import BuildError
from eks_harness.engine.model import EntryFolder, IssueOut, Platform
from eks_harness.engine.runs import RunManager, engine

router = APIRouter(tags=["flows"])

ENTRY_FOLDERS: tuple[EntryFolder, ...] = ("flows", "seeds", "edits", "scenes", "scores")
MAX_SOURCE_BYTES = 1024 * 1024
CurrentPrincipal = Annotated[Principal, Depends(current_principal)]

_HARNESS = ".harness"
_APP_FOLDER: EntryFolder = "flows"
_SUFFIX = ".py"
_SHA_PREFIX = "sha256:"
_PLATFORMS: dict[str, Platform] = {"web": "web", "ios": "ios", "android": "android"}
_FLOW_KIND = "flow"


def _bad_path(path: str, why: str) -> ApiError:
    return bad_request(f"path {path!r} {why}; name a .py file under .harness/flows, .harness/seeds, .harness/edits, "
                       ".harness/scenes, .harness/scores or <app>/.harness/flows of the tree", error="bad_path",
                       path=path)


def entry_path(tree: Path, path: str) -> Path:
    if not path or "\\" in path or "\0" in path:
        raise _bad_path(path, "is empty or not a POSIX path")
    pure = PurePosixPath(path)
    if pure.is_absolute() or (len(path) > 1 and path[1] == ":"):
        raise _bad_path(path, "is absolute")
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise _bad_path(path, "has empty, '.' or '..' segments")
    if not parts[-1].endswith(_SUFFIX) or parts[-1] == _SUFFIX:
        raise _bad_path(path, "is not a .py file")
    if parts[-1].startswith(INLINE_PREFIX):
        raise _bad_path(path, f"uses the {INLINE_PREFIX} prefix the hub keeps for inline sources")
    if _HARNESS not in parts:
        raise _bad_path(path, "is outside the entry folders")
    index = parts.index(_HARNESS)
    allowed = ENTRY_FOLDERS if index == 0 else (_APP_FOLDER,)
    if len(parts) != index + 3 or parts[index + 1] not in allowed:
        raise _bad_path(path, "is not a file directly inside an entry folder")
    root = tree.resolve()
    target = (root / Path(*parts)).resolve()
    if not target.is_relative_to(root):
        raise _bad_path(path, "leaves the tree")
    return target


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _flow_tree(request: Request, principal: Principal, tree: str, level: str) -> Path:
    root = _hub_tree(tree)
    authorize_tree(get_ctx(request), principal, root, level)
    return root


def _entry(entry: EntryOut) -> FlowEntryOut:
    return FlowEntryOut(kind=entry.kind, name=entry.name, file=entry.file, qualname=entry.qualname, line=entry.line,
                        params=[dict(item) for item in entry.params], tags=list(entry.tags),
                        description=entry.description, app=entry.app, platforms=list(entry.platforms))


def _file(item: EntryFile) -> FlowFileOut:
    return FlowFileOut(path=item.path, kind=item.kind, entries=[_entry(entry) for entry in item.entries],
                       error=item.error)


def _relative(tree: Path, target: Path) -> str:
    return target.relative_to(tree.resolve()).as_posix()


def _folder_of(relative: str) -> str:
    parts = relative.split("/")
    return parts[parts.index(_HARNESS) + 1]


def _validations(manager: RunManager, tree: Path, relative: str,
                 entries: tuple[EntryOut, ...]) -> dict[str, PlanValidation]:
    found: dict[str, PlanValidation] = {}
    for entry in entries:
        matrix: list[Platform | None] = [_PLATFORMS[name] for name in entry.platforms if name in _PLATFORMS]
        platforms: list[Platform | None] = matrix if entry.kind == _FLOW_KIND and len(matrix) > 1 else [None]
        for platform in platforms:
            key = f"{entry.kind}:{entry.name}" + (f"@{platform}" if platform is not None else "")
            try:
                built = manager.builder.build(BuildRequest(spec=f"{relative}::{entry.name}", tree=tree, cwd=tree,
                                                           platform=platform))
            except BuildError as error:
                found[key] = PlanValidation(ok=False, hash="", issues=[IssueOut(
                    severity="error", code="plan_build", message=error.message, src=error.where)])
                continue
            found[key] = PlanValidation(ok=not any(issue.severity == "error" for issue in built.issues),
                                        hash=built.plan.hash, issues=list(built.issues))
    return found


@router.get("/api/flows", response_model=FlowList)
def list_flows(request: Request, principal: CurrentPrincipal, tree: Annotated[str, Query()],
               kind: Annotated[str | None, Query()] = None,
               plugins: Annotated[bool, Query()] = True) -> FlowList:
    root = _flow_tree(request, principal, tree, "viewer")
    files = engine(get_ctx(request)).builder.files(root, kind=kind, plugins=plugins)
    return FlowList(tree=str(root), files=[_file(item) for item in files])


@router.get("/api/flows/source", response_model=FlowSource)
def get_flow(request: Request, principal: CurrentPrincipal, tree: Annotated[str, Query()],
             path: Annotated[str, Query()]) -> FlowSource:
    root = _flow_tree(request, principal, tree, "viewer")
    target = entry_path(root, path)
    if not target.is_file():
        raise not_found(f"No entry file {path} in the tree {root}.", error="flow_not_found", path=path)
    data = target.read_bytes()
    try:
        source = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise bad_request(f"{path} is not UTF-8 text: {error}", error="bad_source") from error
    return FlowSource(tree=str(root), path=_relative(root, target), source=source, sha256=_sha(data))


@router.put("/api/flows/source", response_model=FlowSaved)
def save_flow(body: FlowSave, request: Request, principal: CurrentPrincipal) -> FlowSaved:
    root = _flow_tree(request, principal, body.tree, "editor")
    target = entry_path(root, body.path)
    data = body.source.encode("utf-8")
    if len(data) > MAX_SOURCE_BYTES:
        raise too_large(f"{body.path} has {len(data)} bytes; entry files hold at most {MAX_SOURCE_BYTES} bytes",
                        limit=MAX_SOURCE_BYTES)
    current = _sha(target.read_bytes()) if target.is_file() else None
    if body.expect_sha256 is not None and body.expect_sha256.removeprefix(_SHA_PREFIX) != current:
        what = f"has sha256 {current}" if current is not None else "does not exist"
        raise conflict(f"{body.path} {what}, not {body.expect_sha256}; read it again and merge your change",
                       error="sha_mismatch", path=body.path, current=current, expected=body.expect_sha256)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    manager = engine(get_ctx(request))
    manager.registries.invalidate(root, target)
    relative = _relative(root, target)
    saved = FlowSaved(path=relative, sha256=_sha(data))
    try:
        files = manager.builder.files(root, kind=_folder_of(relative))
    except BuildError as error:
        return saved.model_copy(update={"error": f"{error.message}\n  at {error.where}" if error.where
                                        else error.message})
    item = next((file for file in files if file.path == relative), None)
    if item is None:
        return saved
    if item.error is not None:
        return saved.model_copy(update={"error": item.error})
    entries = [_entry(entry) for entry in item.entries]
    validations = _validations(manager, root, relative, item.entries) if body.validate_entries else {}
    return saved.model_copy(update={"entries": entries, "validations": validations})


@router.delete("/api/flows/source", response_model=FlowSaved)
def delete_flow(request: Request, principal: CurrentPrincipal, tree: Annotated[str, Query()],
                path: Annotated[str, Query()]) -> FlowSaved:
    root = _flow_tree(request, principal, tree, "editor")
    target = entry_path(root, path)
    if not target.is_file():
        raise not_found(f"No entry file {path} in the tree {root}.", error="flow_not_found", path=path)
    removed = _sha(target.read_bytes())
    target.unlink()
    engine(get_ctx(request)).registries.invalidate(root, target)
    return FlowSaved(path=_relative(root, target), sha256=removed)


__all__ = [
    "ENTRY_FOLDERS",
    "MAX_SOURCE_BYTES",
    "CurrentPrincipal",
    "delete_flow",
    "entry_path",
    "get_flow",
    "list_flows",
    "router",
    "save_flow",
]
