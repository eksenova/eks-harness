"""Publish a finished render: list it in its studio project and store it as a harness artifact.

Renders made outside the studio (``eks-harness video render``,
``Renderer.render()``, build scripts) are linked into
``<project>/renders/<job_id>/`` with the ``metadata.json`` and thumbnail the
studio writes for its own renders, so they appear in the project's render
list, and uploaded to the harness artifact store under a harness project and
session. The result carries the studio deep link
(``<harness>/studio?project=<project>&render=<job_id>``) and the artifact's
links.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from .artifacts import extract_thumbnail, write_metadata
from .jobs import JobRecord

__all__ = ["Published", "configure", "harness_target", "main", "publish", "run", "studio_link"]


@dataclass(frozen=True)
class Published:
    job_id: str
    output: Path
    url: str
    file_url: str
    artifact: dict[str, Any] | None = None
    project: str | None = None
    session: str | None = None
    warnings: list[str] = field(default_factory=list)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "video"


def harness_target(project_dir: Path) -> tuple[str, str]:
    """Harness project and session for a studio project.

    The project comes from the enclosing repository's ``.harness/project.toml``
    (``[video] project``, else ``[project] id``); otherwise ``video/<project>``.
    The session is the studio project's folder name.
    """

    from eks_harness.plugins.project import find_tree, load_project_config

    tree = find_tree(project_dir)
    config = load_project_config(tree) if tree else None
    project = None
    if config is not None:
        project = config.section("video").get("project") or config.project_id
    return str(project or f"video/{_slug(project_dir.name)}"), _slug(project_dir.name)


def studio_link(base: str, project_name: str, job_id: str) -> str:
    return f"{base.rstrip('/')}/studio?{urlencode({'project': project_name, 'render': job_id})}"


def _default_client() -> Any:
    from eks_harness.cli.client import HarnessClient

    return HarnessClient()


def _public_base() -> str:
    from eks_harness.config import load as load_config

    return load_config().public_url()


def publish(output: Path, project_dir: Path, *, mode: str = "final", label: str = "",
            render_seconds: float | None = None, upload: bool = True, client: Any = None,
            harness_project: str | None = None, session: str | None = None,
            tags: list[str] | None = None) -> Published:
    output = Path(output).resolve()
    project_dir = Path(project_dir).resolve()
    if not output.is_file():
        raise FileNotFoundError(output)
    job_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    target_dir = project_dir / "renders" / job_id
    target_dir.mkdir(parents=True, exist_ok=False)
    target = target_dir / output.name
    try:
        os.link(output, target)
    except OSError:
        shutil.copy2(output, target)
    finished = output.stat().st_mtime
    record = JobRecord(job_id=job_id, project_id=project_dir.name, project_path=str(project_dir), mode=mode,
                       status="succeeded", progress=1.0, message=label or "published", output_path=str(target),
                       started_at=finished - (render_seconds or 0.0), updated_at=finished)
    write_metadata(output_path=target, record=record, condensed_events=[])
    extract_thumbnail(output_path=target)

    default_project, default_session = harness_target(project_dir)
    project = harness_project or default_project
    session_slug = session or default_session
    warnings: list[str] = []
    artifact: dict[str, Any] | None = None
    owns_client = upload and client is None
    if owns_client:
        client = _default_client()
    try:
        base = str(getattr(client, "base_url", "") or "") if client is not None else ""
        if upload and client is not None:
            try:
                artifact = client.upload("/api/artifacts", [target], {
                    "project": project, "session": session_slug, "kind": "video",
                    "caption": label or f"{project_dir.name} {mode} render",
                    "tags": ",".join(["render", mode, *(tags or [])]),
                    "meta": {"studioProject": project_dir.name, "renderJob": job_id, "mode": mode,
                             "renderSeconds": render_seconds},
                })
            except Exception as problem:
                warnings.append(f"artifact upload failed: {problem}")
    finally:
        if owns_client and client is not None:
            client.close()
    base = base or _public_base()
    artifact = artifact if isinstance(artifact, dict) else None
    raw = (artifact or {}).get("rawUrl") or (artifact or {}).get("raw_url")
    file_url = raw or f"{base.rstrip('/')}/api/studio/files/projects/{project_dir.name}/renders/{job_id}/{target.name}"
    return Published(job_id=job_id, output=target, url=studio_link(base, project_dir.name, job_id),
                     file_url=file_url, artifact=artifact, project=project if artifact else None,
                     session=session_slug if artifact else None, warnings=warnings)


def configure(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument("output", type=Path)
    parser.add_argument("--project", type=Path, required=True, help="studio project directory (holds project.py)")
    parser.add_argument("--mode", default="final")
    parser.add_argument("--label", default="")
    parser.add_argument("--harness-project", default=None, help="harness project id (owner/name)")
    parser.add_argument("--session", default=None, help="harness session (default: the studio project name)")
    parser.add_argument("--tag", action="append", default=[], help="extra artifact tag (repeatable)")
    parser.add_argument("--no-upload", action="store_true", help="only list the render in the studio project")
    return parser


def run(args: argparse.Namespace) -> int:
    result = publish(args.output, args.project, mode=args.mode, label=args.label, upload=not args.no_upload,
                     harness_project=args.harness_project, session=args.session, tags=args.tag)
    print(result.url)
    print(result.file_url)
    if result.artifact:
        print(result.artifact.get("url") or "")
    for warning in result.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eks-harness studio publish",
                                     description="Publish a render to the studio and the artifact store")
    return run(configure(parser).parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
