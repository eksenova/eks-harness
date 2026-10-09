from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Iterator, Sequence
from datetime import datetime
from pathlib import Path, PureWindowsPath
from typing import Any
from urllib.parse import quote

from rich.progress import BarColumn, DownloadColumn, Progress, TextColumn, TransferSpeedColumn

from eks_harness.cli.client import HarnessClient, client_from_args
from eks_harness.cli.ui import (
    EXIT_NOTHING_SELECTED,
    EXIT_USAGE,
    confirm_or_exit,
    console,
    err,
    fail,
    human_size,
    info,
    kv_panel,
    ok,
    print_json,
    print_links,
    table,
)
from eks_harness.ids import slugify

LINK_KEYS = ("rawUrl", "url", "sessionUrl")
SKIP_NAMES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})
PAGE = 200


def _client(args: argparse.Namespace) -> HarnessClient:
    return client_from_args(args)


def _project_path(project: str) -> str:
    owner, sep, name = project.strip().partition("/")
    if not sep or not owner or not name or "/" in name:
        fail(f"project must be owner/name, got {project!r}", EXIT_USAGE)
    return f"/api/projects/{quote(owner.lower(), safe='')}/{quote(name.lower(), safe='')}"


def _session_ref(session: str) -> str:
    value = session.strip()
    if "/" in value or any(c.isspace() for c in value) or value != value.lower():
        value = slugify(value)
    return quote(value, safe="")


def _session_path(project: str, session: str) -> str:
    return f"{_project_path(project)}/sessions/{_session_ref(session)}"


def _session_target(args: argparse.Namespace) -> tuple[str, str, str | None]:
    refs = list(args.refs)
    if len(refs) == 1:
        project, session = _split_session(None, refs[0])
    elif len(refs) == 2:
        project, session = refs
    else:
        fail("pass [owner/name] <session>", EXIT_USAGE)
    if project:
        return _session_path(project, session), f"{session} of {project}", project
    return f"/api/sessions/{_session_ref(session)}", session, None


def _session_title(session: dict) -> str:
    where = session.get("projectId") or ", ".join(session.get("projectIds") or []) or "no project"
    return f"{where} / {session['name']}"


def _split_session(project: str | None, session: str | None) -> tuple[str | None, str | None]:
    if project or not session or session.count("/") < 2:
        return project, session
    owner, name, rest = session.split("/", 2)
    return f"{owner}/{name}", rest


def _tags(values: Sequence[str] | None) -> list[str]:
    found: list[str] = []
    for value in values or []:
        found.extend(t.strip() for t in value.split(",") if t.strip())
    return list(dict.fromkeys(found))


def _meta(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as problem:
        fail(f"--meta is not valid JSON: {problem}", EXIT_USAGE)
    if not isinstance(value, dict):
        fail("--meta must be a JSON object", EXIT_USAGE)
    return value


def _when(value: str | None) -> str:
    if not value:
        return ""
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return stamp.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _progress_callback(progress: Progress | None, label: str) -> Callable[[int, int | None], None] | None:
    if progress is None:
        return None
    task = progress.add_task(label, total=None)

    def update(done: int, total: int | None) -> None:
        progress.update(task, completed=done, total=total)

    return update


def _progress() -> Progress | None:
    if not err.is_terminal:
        return None
    return Progress(TextColumn("{task.description}"), BarColumn(), DownloadColumn(), TransferSpeedColumn(),
                    console=err, transient=True)


def _target_fields(args: argparse.Namespace) -> dict[str, Any]:
    project, session = _split_session(getattr(args, "project", None), getattr(args, "session", None))
    if args.sid:
        if project or session:
            fail("use either --sid or --project/--session, not both", EXIT_USAGE)
        return {"sid": args.sid}
    if not project:
        fail("pass --sid <sid>, or --project owner/name with an optional --session", EXIT_USAGE)
    return {"project": project, "session": session}


def _print_artifact_links(item: dict) -> None:
    print_links(item, LINK_KEYS)


def _artifact_rows(items: Sequence[dict]) -> list[list[Any]]:
    return [[a["id"], a["kind"], a["filename"], human_size(a["size"]), a.get("sessionSlug") or "_project",
             "" if a.get("seen") else "unseen", "pinned" if a.get("pinned") else "", _when(a.get("createdAt")),
             a.get("caption") or ""] for a in items]


ID_COLUMN = ("Id", {"no_wrap": True, "min_width": 26})
ARTIFACT_COLUMNS = [ID_COLUMN, "Kind", "File", ("Size", {"justify": "right"}), "Session", "Seen", "Pinned", "Created",
                    "Caption"]


def cmd_projects_list(args: argparse.Namespace) -> int:
    with _client(args) as client:
        items = client.get("/api/projects")["items"]
    if args.json:
        print_json(items)
        return 0
    if not items:
        info("No projects yet. Upload something or run: eks-harness projects create owner/name")
        return 0
    console.print(table(["Project", "Title", ("Sessions", {"justify": "right"}), ("Artifacts", {"justify": "right"}),
                         ("Unseen", {"justify": "right"}), ("Size", {"justify": "right"}), "Last activity",
                         "Access"],
                        [[p["id"], p.get("title") or "", p["sessionCount"], p["artifactCount"], p["unseenCount"],
                          human_size(p["sizeBytes"]), _when(p.get("lastActivityAt")), p.get("access") or ""]
                         for p in items]))
    return 0


def _days(days: int | None) -> str:
    return f"{days} days" if days else "keep forever"


def _project_retention(project: dict) -> str:
    source = project.get("retentionSource") or ("project" if project.get("retentionDays") else "global")
    if source == "forever":
        return "keep forever (project)"
    if source == "project":
        return f"{project['retentionDays']} days (project)"
    return f"{_days(project.get('effectiveRetentionDays'))} (global default)"


def _artifact_retention(item: dict) -> str:
    source = item.get("retentionSource")
    if source == "pinned":
        return "keep forever (pinned)"
    if not source:
        return _days(item.get("retentionDays")) if item.get("retentionDays") else "inherited"
    days = item.get("effectiveRetentionDays")
    where = {"artifact": "this artifact", "project": "project", "global": "global default"}[source]
    expires = f", deleted after {_when(item['expiresAt'])}" if item.get("expiresAt") else ""
    return f"{_days(days)} ({where}{expires})"


def _retention_body(args: argparse.Namespace) -> dict[str, Any]:
    if getattr(args, "keep_forever", False):
        return {"retentionDays": 0}
    if getattr(args, "inherit_retention", False):
        return {"retentionDays": None}
    if args.retention_days is not None:
        if args.retention_days < 1:
            fail("--retention-days must be 1 or more; use --keep-forever to never delete", EXIT_USAGE)
        return {"retentionDays": args.retention_days}
    return {}


def _project_panel(project: dict) -> None:
    console.print(kv_panel(project["id"], [
        ("Title", project.get("title") or ""), ("Description", project.get("description") or ""),
        ("Implicit", "yes" if project.get("implicit") else "no"),
        ("Retention", _project_retention(project)),
        ("Sessions", project["sessionCount"]), ("Artifacts", project["artifactCount"]),
        ("Unseen", project["unseenCount"]), ("Size", human_size(project["sizeBytes"])),
        ("Live leases", project.get("activeLeases", 0)), ("Last activity", _when(project.get("lastActivityAt"))),
        ("Access", project.get("access") or ""), ("Link", project["url"])]))


def cmd_projects_show(args: argparse.Namespace) -> int:
    with _client(args) as client:
        project = client.get(_project_path(args.project))
    if args.json:
        print_json(project)
    else:
        _project_panel(project)
    return 0


def cmd_projects_create(args: argparse.Namespace) -> int:
    body = {"id": args.project, "title": args.title or "", "description": args.description or ""}
    body.update(_retention_body(args))
    with _client(args) as client:
        project = client.post("/api/projects", json=body)
    if args.json:
        print_json(project)
    else:
        ok(f"Created {project['id']}")
        print(project["url"])
    return 0


def cmd_projects_edit(args: argparse.Namespace) -> int:
    body: dict[str, Any] = {}
    if args.title is not None:
        body["title"] = args.title
    if args.description is not None:
        body["description"] = args.description
    body.update(_retention_body(args))
    if not body:
        fail("nothing to change: pass --title, --description, --retention-days, --keep-forever or "
             "--inherit-retention", EXIT_USAGE)
    with _client(args) as client:
        project = client.patch(_project_path(args.project), json=body)
    if args.json:
        print_json(project)
    else:
        ok(f"Updated {project['id']}")
    return 0


def _summary_text(summary: dict) -> str:
    parts = [f"{summary['artifacts']} files ({human_size(summary['bytes'])})"]
    if summary.get("sessions"):
        parts.append(f"{summary['sessions']} sessions")
    if summary.get("notes"):
        parts.append(f"{summary['notes']} notes")
    if summary.get("shares"):
        parts.append(f"{summary['shares']} share links")
    return ", ".join(parts)


def _confirmed_delete(args: argparse.Namespace, what: str, run: Callable[[bool], dict]) -> dict:
    preview = run(True)
    confirm_or_exit(f"Delete {what}: {_summary_text(preview)}?", args.yes)
    return run(False)


def cmd_projects_delete(args: argparse.Namespace) -> int:
    path = _project_path(args.project)
    with _client(args) as client:
        summary = _confirmed_delete(args, f"the project {args.project}", lambda dry: client.delete(
            path, params={"dryRun": "true" if dry else None, "force": "true" if args.force else None}))
    if args.json:
        print_json(summary)
    else:
        ok(f"Deleted {args.project}: {_summary_text(summary)}")
    return 0


def _shared_session_rows(items: Sequence[dict]) -> list[list[Any]]:
    return [[s["name"], ", ".join(p.rpartition("/")[2] for p in s.get("projectIds") or []),
             ", ".join(l["sid"] for l in s.get("activeLeases") or []), s["artifactCount"], s["unseenCount"],
             _when(s.get("lastActiveAt"))] for s in items]


def _session_rows(items: Sequence[dict]) -> list[list[Any]]:
    return [[s["name"], s["slug"], ", ".join(f"{l['sid']} ({l['kind']})" for l in s.get("activeLeases") or []),
             s["artifactCount"], s["unseenCount"], s["noteCount"], human_size(s["sizeBytes"]),
             _when(s.get("lastActiveAt"))] for s in items]


def cmd_sessions_list(args: argparse.Namespace) -> int:
    with _client(args) as client:
        path = _project_path(args.project) + "/sessions" if args.project else "/api/sessions"
        items = client.get(path)["items"]
    if args.json:
        print_json(items)
        return 0
    if not items:
        info(f"No sessions in {args.project} yet." if args.project else "No sessions yet.")
        return 0
    if not args.project:
        console.print(table(["Session", "Projects", "Live leases", ("Artifacts", {"justify": "right"}),
                             ("Unseen", {"justify": "right"}), "Last active"], _shared_session_rows(items)))
        return 0
    console.print(table(["Session", "Slug", "Live leases", ("Artifacts", {"justify": "right"}),
                         ("Unseen", {"justify": "right"}), ("Notes", {"justify": "right"}),
                         ("Size", {"justify": "right"}), "Last active"], _session_rows(items)))
    return 0


def cmd_sessions_show(args: argparse.Namespace) -> int:
    with _client(args) as client:
        session = client.get(_session_target(args)[0])
    if args.json:
        print_json(session)
        return 0
    leases = ", ".join(f"{l['sid']} ({l['kind']} on {l.get('resource') or 'queue'}, {l['state']})"
                       for l in session.get("activeLeases") or []) or "none"
    console.print(kv_panel(_session_title(session), [
        ("Slug", session["slug"]), ("Live leases", leases), ("Artifacts", session["artifactCount"]),
        ("Unseen", session["unseenCount"]), ("Notes", session["noteCount"]), ("Size", human_size(session["sizeBytes"])),
        ("Created", _when(session.get("createdAt"))), ("Last active", _when(session.get("lastActiveAt"))),
        ("Link", session["url"]), ("All projects", session["sharedUrl"])]))
    return 0


def cmd_sessions_rename(args: argparse.Namespace) -> int:
    body = {"name": args.new_name}
    if args.slug:
        body["slug"] = args.slug
    with _client(args) as client:
        session = client.patch(_session_target(args)[0], json=body)
    if args.json:
        print_json(session)
    else:
        ok(f"Renamed to {session['name']} ({session['slug']})")
        print(session["url"])
    return 0


def cmd_sessions_delete(args: argparse.Namespace) -> int:
    path, label, _ = _session_target(args)
    with _client(args) as client:
        summary = _confirmed_delete(args, f"the session {label}", lambda dry: client.delete(
            path, params={"dryRun": "true" if dry else None, "force": "true" if args.force else None}))
    if args.json:
        print_json(summary)
    else:
        ok(f"Deleted {label}: {_summary_text(summary)}")
    return 0


def _timeline_row(entry: dict) -> list[str]:
    kind = entry["type"]
    if kind == "event":
        event = entry["event"]
        detail = event.get("detail") or {}
        parts = [event.get("resource") or "", f"sid {event['leaseSid']}" if event.get("leaseSid") else "",
                 str(detail.get("reason") or detail.get("message") or "")]
        return [_when(entry["ts"]), event["type"], event.get("actor") or "", " ".join(p for p in parts if p)]
    if kind == "note":
        note = entry["note"]
        return [_when(entry["ts"]), "note", note["author"], note["body"]]
    artifact = entry["artifact"]
    text = f"{artifact['kind']} {artifact['filename']}"
    if artifact.get("caption"):
        text += f": {artifact['caption']}"
    return [_when(entry["ts"]), "artifact", artifact.get("createdBy") or "", f"{text}  {artifact['url']}"]


def cmd_timeline(args: argparse.Namespace) -> int:
    params = {"limit": args.limit, "types": ",".join(args.types) if args.types else None, "since": args.since}
    with _client(args) as client:
        data = client.get(_session_target(args)[0] + "/timeline", params=params)
    if args.json:
        print_json(data)
        return 0
    if not data["items"]:
        info("The timeline is empty.")
        return 0
    console.print(table(["Time", "Type", "By", "What"], [_timeline_row(e) for e in data["items"]],
                        title=_session_title(data["session"])))
    return 0


def cmd_note(args: argparse.Namespace) -> int:
    body = " ".join(args.text).strip()
    if body == "-":
        body = sys.stdin.read().strip()
    if not body:
        fail("the note is empty", EXIT_USAGE)
    with _client(args) as client:
        note = client.post(f"/api/sid/{quote(args.sid, safe='')}/notes", json={"body": body})
    if args.json:
        print_json(note)
    else:
        ok(f"Note {note['id']} added")
    return 0


def cmd_upload(args: argparse.Namespace) -> int:
    fields = _target_fields(args)
    fields.update(kind=args.kind, caption=args.caption, tags=_tags(args.tags) or None, meta=_meta(args.meta),
                  source=args.source)
    files = [Path(p) for p in args.files]
    for path in files:
        if not path.is_file():
            fail(f"{path} is not a file", EXIT_USAGE)
    results = []
    progress = _progress()
    with _client(args) as client:
        if progress:
            progress.start()
        try:
            for path in files:
                callback = _progress_callback(progress, path.name)
                results.append(client.upload("/api/artifacts", [path], fields, progress=callback))
        finally:
            if progress:
                progress.stop()
    if args.json:
        print_json(results[0] if len(results) == 1 else results)
        return 0
    for item in results:
        _print_artifact_links(item)
    return 0


def _site_files(root: Path) -> list[tuple[str, Path]]:
    found = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name not in SKIP_NAMES and not path.is_symlink():
            found.append((path.relative_to(root).as_posix(), path))
    return found


def cmd_site_upload(args: argparse.Namespace) -> int:
    source = Path(args.path)
    fields = _target_fields(args)
    fields.update(entry=args.entry, caption=args.caption, tags=_tags(args.tags) or None, meta=_meta(args.meta),
                  name=args.name, source=args.source)
    progress = _progress()
    with _client(args) as client:
        if progress:
            progress.start()
        try:
            callback = _progress_callback(progress, source.name)
            if source.is_dir():
                files = _site_files(source)
                if not files:
                    fail(f"{source} has no files", EXIT_USAGE)
                fields["name"] = fields.get("name") or source.resolve().name
                result = client.upload("/api/sites", files, fields, field_name="files", progress=callback)
            elif source.is_file():
                result = client.upload("/api/sites", [source], fields, field_name="file", progress=callback)
            else:
                fail(f"{source} is not a directory or a zip file", EXIT_USAGE)
        finally:
            if progress:
                progress.stop()
    if args.json:
        print_json(result)
        return 0
    if result.get("siteUrl"):
        print(result["siteUrl"])
    _print_artifact_links(result)
    return 0


def _list_params(args: argparse.Namespace, cursor: str | None = None) -> dict:
    project, session = _split_session(args.project, args.session)
    unseen = True if args.unseen else (False if args.seen else None)
    return {"project": project, "session": session, "kind": ",".join(args.kind) if args.kind else None,
            "tag": ",".join(_tags(args.tag)) or None, "q": args.query, "sid": args.sid,
            "unseen": None if unseen is None else str(unseen).lower(),
            "pinned": "true" if args.pinned else None, "cursor": cursor, "limit": args.limit}


def _iter_artifacts(client: HarnessClient, args: argparse.Namespace, fetch_all: bool) -> Iterator[dict]:
    cursor = None
    while True:
        page = client.get("/api/artifacts", params=_list_params(args, cursor))
        yield from page["items"]
        cursor = page.get("nextCursor")
        if not fetch_all or not cursor:
            return


def cmd_artifacts_list(args: argparse.Namespace) -> int:
    with _client(args) as client:
        items = list(_iter_artifacts(client, args, args.all))
    if args.json:
        print_json(items)
        return 0
    if not items:
        info("No artifacts match.")
        return 0
    console.print(table(ARTIFACT_COLUMNS, _artifact_rows(items)))
    return 0


def cmd_artifacts_show(args: argparse.Namespace) -> int:
    with _client(args) as client:
        item = client.get(f"/api/artifacts/{quote(args.id, safe='')}", params={
            "markSeen": "true" if args.mark_seen else None})
    if args.json:
        print_json(item)
        return 0
    rows = [("Kind", item["kind"]), ("File", item["filename"]), ("Size", human_size(item["size"])),
            ("Mime", item["mime"]), ("Project", item["projectId"]), ("Session", item.get("sessionName") or "_project"),
            ("Sid", item.get("leaseSid") or ""), ("Caption", item.get("caption") or ""),
            ("Tags", ", ".join(item.get("tags") or [])), ("Pinned", "yes" if item.get("pinned") else "no"),
            ("Retention", _artifact_retention(item)),
            ("Seen", "yes" if item.get("seen") else "no"), ("Created", _when(item.get("createdAt"))),
            ("Source", item.get("source")), ("By", item.get("createdBy") or ""), ("SHA-256", item["sha256"])]
    if item.get("width"):
        rows.append(("Dimensions", f"{item['width']} x {item['height']}"))
    if item.get("durationMs"):
        rows.append(("Duration", f"{item['durationMs'] / 1000:.1f} s"))
    if item.get("shareCount"):
        rows.append(("Share links", item["shareCount"]))
    rows += [("Direct link", item["rawUrl"]), ("UI link", item["url"]), ("Session link", item["sessionUrl"])]
    if item.get("siteUrl"):
        rows.append(("Site", item["siteUrl"]))
    console.print(kv_panel(item["id"], rows))
    return 0


def safe_filename(value: object, fallback: str) -> str:
    name = PureWindowsPath(str(value or "")).name
    name = Path(name).name.strip()
    if name in ("", ".", ".."):
        return fallback
    return name


def _unique(dest: Path) -> Path:
    if not dest.exists():
        return dest
    counter = 2
    while True:
        candidate = dest.with_name(f"{dest.stem} ({counter}){dest.suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def cmd_artifacts_download(args: argparse.Namespace) -> int:
    project, session = _split_session(args.project, args.session)
    if not args.ids and not project:
        fail("pass artifact ids, or --project owner/name with an optional --session", EXIT_USAGE)
    output = Path(args.output) if args.output else Path.cwd()
    downloaded: list[dict] = []
    progress = _progress()
    with _client(args) as client:
        if progress:
            progress.start()
        try:
            if args.zip:
                params = {"ids": ",".join(args.ids) if args.ids else None, "project": project, "session": session}
                callback = _progress_callback(progress, "zip")
                dest = client.download("/api/artifacts/zip", output, params=params, progress=callback)
                downloaded.append({"path": str(dest)})
            else:
                if args.ids:
                    items = [client.get(f"/api/artifacts/{quote(i, safe='')}") for i in args.ids]
                else:
                    list_args = argparse.Namespace(project=project, session=session, kind=args.kind, tag=args.tag,
                                                   query=None, sid=None, unseen=False, seen=False, pinned=False,
                                                   limit=PAGE)
                    items = list(_iter_artifacts(client, list_args, True))
                if not items:
                    fail("nothing to download", EXIT_NOTHING_SELECTED)
                many = len(items) > 1 or output.is_dir() or args.output is None
                if many:
                    output.mkdir(parents=True, exist_ok=True)
                for item in items:
                    name = safe_filename(item.get("filename"), f"{item['id']}.bin")
                    dest = _unique(output / name) if many else output
                    callback = _progress_callback(progress, name)
                    path = client.download(f"/raw/{quote(str(item['id']), safe='')}/{quote(name, safe='')}", dest,
                                           params={"download": "1"}, progress=callback)
                    downloaded.append({"id": item["id"], "path": str(path), "filename": item["filename"],
                                       "url": item["url"], "rawUrl": item["rawUrl"]})
        finally:
            if progress:
                progress.stop()
    if args.json:
        print_json(downloaded)
    else:
        for entry in downloaded:
            print(entry["path"])
    return 0


def cmd_artifacts_delete(args: argparse.Namespace) -> int:
    with _client(args) as client:
        if len(args.ids) == 1:
            path = f"/api/artifacts/{quote(args.ids[0], safe='')}"
            summary = _confirmed_delete(args, args.ids[0], lambda dry: client.delete(
                path, params={"dryRun": "true" if dry else None}))
        else:
            summary = _confirmed_delete(args, f"{len(args.ids)} artifacts", lambda dry: client.post(
                "/api/artifacts/delete", json={"ids": args.ids}, params={"dryRun": "true" if dry else None}))
    if args.json:
        print_json(summary)
    else:
        ok(f"Deleted {_summary_text(summary)}")
    return 0


def _patch_each(args: argparse.Namespace, body: dict, done: str) -> int:
    results = []
    with _client(args) as client:
        for artifact_id in args.ids:
            results.append(client.patch(f"/api/artifacts/{quote(artifact_id, safe='')}", json=body))
    if args.json:
        print_json(results[0] if len(results) == 1 else results)
    else:
        for item in results:
            ok(f"{done} {item['id']} ({item['filename']})")
    return 0


def cmd_artifacts_pin(args: argparse.Namespace) -> int:
    return _patch_each(args, {"pinned": True}, "Pinned")


def cmd_artifacts_unpin(args: argparse.Namespace) -> int:
    return _patch_each(args, {"pinned": False}, "Unpinned")


def cmd_artifacts_caption(args: argparse.Namespace) -> int:
    args.ids = [args.id]
    return _patch_each(args, {"caption": " ".join(args.text)}, "Captioned")


def _seen(args: argparse.Namespace, seen: bool) -> int:
    with _client(args) as client:
        result = client.post("/api/artifacts/seen", json={"ids": args.ids, "seen": seen})
    if args.json:
        print_json(result)
    else:
        ok(f"Marked {len(args.ids)} {'seen' if seen else 'unseen'}")
    return 0


def cmd_artifacts_seen(args: argparse.Namespace) -> int:
    return _seen(args, True)


def cmd_artifacts_unseen(args: argparse.Namespace) -> int:
    return _seen(args, False)


def cmd_artifacts_tag(args: argparse.Namespace) -> int:
    add, remove = _tags(args.add), _tags(args.remove)
    if not add and not remove:
        fail("pass --add and/or --remove", EXIT_USAGE)
    with _client(args) as client:
        result = client.post("/api/artifacts/tags", json={"ids": args.ids, "add": add, "remove": remove})
    if args.json:
        print_json(result["items"])
    else:
        for item in result["items"]:
            ok(f"{item['id']}: {', '.join(item['tags']) or 'no tags'}")
    return 0


def cmd_artifacts_retention(args: argparse.Namespace) -> int:
    if args.inherit == (args.days is not None):
        fail("pass --days N or --inherit", EXIT_USAGE)
    if args.days is not None and args.days < 1:
        fail("--days must be 1 or more; pin an artifact to keep it forever", EXIT_USAGE)
    with _client(args) as client:
        result = client.post("/api/artifacts/retention", json={"ids": args.ids, "retentionDays": args.days})
    if args.json:
        print_json(result["items"])
    else:
        for item in result["items"]:
            ok(f"{item['id']}: {_days(item['retentionDays']) if item.get('retentionDays') else 'inherits'}")
    return 0


def cmd_tags_list(args: argparse.Namespace) -> int:
    with _client(args) as client:
        items = client.get("/api/tags/catalog")["items"]
    if args.json:
        print_json(items)
        return 0
    console.print(table(["Tag", "Label", "Color", "Built in", "Artifacts"],
                        [[t["tag"], t["label"], t.get("color") or "", "yes" if t["builtin"] else "",
                          t["count"]] for t in items]))
    return 0


def cmd_tags_color(args: argparse.Namespace) -> int:
    if args.reset == bool(args.color):
        fail("pass a #rrggbb color or --reset", EXIT_USAGE)
    with _client(args) as client:
        result = client.put(f"/api/tags/{quote(args.tag, safe='')}/color",
                            json={"color": None if args.reset else args.color})
    if args.json:
        print_json(result)
    else:
        ok(f"{result['tag']}: {result.get('color') or 'no color'}")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    query = " ".join(args.query)
    with _client(args) as client:
        result = client.get("/api/search", params={"q": query, "project": args.project, "limit": args.limit})
    if args.json:
        print_json(result)
        return 0
    if not result["items"]:
        info(f"Nothing matches {query!r}.")
        return 0
    console.print(table([ID_COLUMN, "Kind", "File", "Project", "Session", "Match", "Link"],
                        [[h["artifact"]["id"], h["artifact"]["kind"], h["artifact"]["filename"],
                          h["artifact"]["projectId"], h["artifact"].get("sessionSlug") or "_project", h["snippet"],
                          h["artifact"]["url"]] for h in result["items"]]))
    return 0


def _share_rows(items: Sequence[dict]) -> list[list[Any]]:
    return [[s["token"], "active" if s["active"] else ("revoked" if s.get("revokedAt") else "expired"),
             _when(s.get("expiresAt")) or "never", s["views"], _when(s.get("lastViewedAt")), s.get("createdBy") or "",
             s.get("directUrl") or s["url"]] for s in items]


def cmd_share_create(args: argparse.Namespace) -> int:
    body = {"expires": None if args.expires in (None, "never") else args.expires}
    with _client(args) as client:
        share = client.post(f"/api/artifacts/{quote(args.id, safe='')}/shares", json=body)
    if args.json:
        print_json(share)
        return 0
    print(share["directUrl"])
    if args.direct:
        return 0
    print(share["url"])
    if share.get("expiresAt"):
        info(f"expires {_when(share['expiresAt'])}")
    return 0


def cmd_share_list(args: argparse.Namespace) -> int:
    with _client(args) as client:
        items = client.get(f"/api/artifacts/{quote(args.id, safe='')}/shares")["items"]
    if args.json:
        print_json(items)
        return 0
    if not items:
        info("No share links.")
        return 0
    console.print(table([("Token", {"no_wrap": True, "min_width": 32}), "State", "Expires", ("Views", {"justify": "right"}), "Last view", "By", "Link"],
                        _share_rows(items)))
    return 0


def cmd_share_revoke(args: argparse.Namespace) -> int:
    with _client(args) as client:
        share = client.delete(f"/api/shares/{quote(args.token, safe='')}")
    if args.json:
        print_json(share)
    else:
        ok(f"Revoked {share['token']}")
    return 0


def _json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="print JSON")


def _yes_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")


def _target_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--sid", help="session id of a live lease (project and session come from it)")
    parser.add_argument("--project", help="project as owner/name (created on first use)")
    parser.add_argument("--session", help="session name, for example the branch (default: project level)")


def _capture_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--caption", default="", help="caption shown with the artifact")
    parser.add_argument("--tags", action="append", help="comma separated tags (repeatable)")
    parser.add_argument("--meta", help="JSON object with extra metadata, for example the URL at capture")
    parser.add_argument("--source", choices=("agent", "cli", "ui", "mcp"), help="who produced it (default: cli)")


def _filter_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", help="project as owner/name")
    parser.add_argument("--session", help="session name or slug (_project for project-level artifacts); "
                                          "owner/name/session works without --project")
    parser.add_argument("--kind", action="append", help="screenshot, video, dom, mhtml, a11y, har, console, log, "
                                                        "site or file (repeatable)")
    parser.add_argument("--tag", action="append", help="only artifacts with this tag (repeatable)")


def register(subparsers: argparse._SubParsersAction) -> None:
    projects = subparsers.add_parser("projects", help="list, create, edit and delete projects")
    projects_sub = projects.add_subparsers(dest="projects_command", metavar="<action>", required=True)
    p = projects_sub.add_parser("list", help="list the projects you can see")
    _json_flag(p)
    p.set_defaults(func=cmd_projects_list)
    p = projects_sub.add_parser("show", help="show one project")
    p.add_argument("project", help="owner/name")
    _json_flag(p)
    p.set_defaults(func=cmd_projects_show)
    p = projects_sub.add_parser("create", help="create a project (admin)")
    p.add_argument("project", help="owner/name")
    p.add_argument("--title")
    p.add_argument("--description")
    retention = p.add_mutually_exclusive_group()
    retention.add_argument("--retention-days", type=int, help="delete unpinned artifacts older than this")
    retention.add_argument("--keep-forever", action="store_true", help="never delete, whatever the global default")
    _json_flag(p)
    p.set_defaults(func=cmd_projects_create)
    p = projects_sub.add_parser("edit", help="change a project's title, description or retention")
    p.add_argument("project", help="owner/name")
    p.add_argument("--title")
    p.add_argument("--description")
    retention = p.add_mutually_exclusive_group()
    retention.add_argument("--retention-days", type=int, help="delete unpinned artifacts older than this")
    retention.add_argument("--keep-forever", "--no-retention", dest="keep_forever", action="store_true",
                           help="never delete, whatever the global default")
    retention.add_argument("--inherit-retention", action="store_true",
                           help="follow the global default (retention.defaultDays)")
    _json_flag(p)
    p.set_defaults(func=cmd_projects_edit)
    p = projects_sub.add_parser("delete", help="delete a project with all its sessions and files")
    p.add_argument("project", help="owner/name")
    p.add_argument("--force", action="store_true", help="delete even when leases are live in it")
    _yes_flag(p)
    _json_flag(p)
    p.set_defaults(func=cmd_projects_delete)

    sessions = subparsers.add_parser("sessions", help="list, show, rename and delete sessions")
    sessions_sub = sessions.add_subparsers(dest="sessions_command", metavar="<action>", required=True)
    p = sessions_sub.add_parser("list", help="list sessions, of one project or across all of them")
    p.add_argument("project", nargs="?", help="owner/name (default: every project)")
    _json_flag(p)
    p.set_defaults(func=cmd_sessions_list)
    p = sessions_sub.add_parser("show", help="show one session")
    p.add_argument("refs", nargs="+", metavar="[owner/name] session",
                   help="session name or slug, optionally after its project (without one: across projects)")
    _json_flag(p)
    p.set_defaults(func=cmd_sessions_show)
    p = sessions_sub.add_parser("rename", help="rename a session (in every project that shares it)")
    p.add_argument("refs", nargs="+", metavar="[owner/name] session",
                   help="session name or slug, optionally after its project")
    p.add_argument("new_name", help="the new name")
    p.add_argument("--slug", help="explicit new slug (default: derived from the name)")
    _json_flag(p)
    p.set_defaults(func=cmd_sessions_rename)
    p = sessions_sub.add_parser("delete", help="delete a session's files, notes and share links (with a project: "
                                              "only that project's part)")
    p.add_argument("refs", nargs="+", metavar="[owner/name] session",
                   help="session name or slug, optionally after its project (without one: across projects)")
    p.add_argument("--force", action="store_true", help="delete even when leases are live in it")
    _yes_flag(p)
    _json_flag(p)
    p.set_defaults(func=cmd_sessions_delete)

    p = subparsers.add_parser("timeline", help="lease events, captures and notes of a session in order")
    p.add_argument("refs", nargs="+", metavar="[owner/name] session",
                   help="session name or slug, optionally after its project (without one: across projects)")
    p.add_argument("--limit", type=int, default=500)
    p.add_argument("--since", help="ISO time, for example 2026-09-25T10:00:00Z")
    p.add_argument("--types", action="append", choices=("event", "note", "artifact"))
    _json_flag(p)
    p.set_defaults(func=cmd_timeline)

    p = subparsers.add_parser("note", help="add a note to the session of a sid")
    p.add_argument("sid", help="session id of the lease")
    p.add_argument("text", nargs="+", help="the note ('-' reads it from stdin)")
    _json_flag(p)
    p.set_defaults(func=cmd_note)

    p = subparsers.add_parser("upload", help="upload files as artifacts")
    _target_flags(p)
    p.add_argument("files", nargs="+", help="files to upload")
    p.add_argument("--kind", help="artifact kind (default: from the file type)")
    _capture_flags(p)
    _json_flag(p)
    p.set_defaults(func=cmd_upload)

    site = subparsers.add_parser("site", help="upload a static HTML site")
    site_sub = site.add_subparsers(dest="site_command", metavar="<action>", required=True)
    p = site_sub.add_parser("upload", help="upload a directory or a zip as a sandboxed site")
    _target_flags(p)
    p.add_argument("path", help="directory or zip file")
    p.add_argument("--entry", default="index.html", help="page to open (default: index.html)")
    p.add_argument("--name", help="name of the site (default: the directory or zip name)")
    _capture_flags(p)
    _json_flag(p)
    p.set_defaults(func=cmd_site_upload)

    artifacts = subparsers.add_parser("artifacts", help="list, show, download, delete, pin, tag and mark artifacts")
    artifacts_sub = artifacts.add_subparsers(dest="artifacts_command", metavar="<action>", required=True)
    p = artifacts_sub.add_parser("list", help="list artifacts, newest first")
    _filter_flags(p)
    p.add_argument("-q", "--query", help="full-text filter over file name, caption, tags and session")
    p.add_argument("--sid", help="only artifacts captured under this sid")
    seen = p.add_mutually_exclusive_group()
    seen.add_argument("--unseen", action="store_true", help="only artifacts you have not seen")
    seen.add_argument("--seen", action="store_true", help="only artifacts you have seen")
    p.add_argument("--pinned", action="store_true", help="only pinned artifacts")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--all", action="store_true", help="fetch every page")
    _json_flag(p)
    p.set_defaults(func=cmd_artifacts_list)
    p = artifacts_sub.add_parser("show", help="show one artifact with its links")
    p.add_argument("id")
    p.add_argument("--mark-seen", action="store_true", help="also mark it seen")
    _json_flag(p)
    p.set_defaults(func=cmd_artifacts_show)
    p = artifacts_sub.add_parser("download", help="download artifacts (or a whole session)")
    p.add_argument("ids", nargs="*", help="artifact ids")
    _filter_flags(p)
    p.add_argument("-o", "--output", help="file or directory to write to (default: current directory)")
    p.add_argument("--zip", action="store_true", help="download one zip instead of separate files")
    _json_flag(p)
    p.set_defaults(func=cmd_artifacts_download)
    p = artifacts_sub.add_parser("delete", help="delete artifacts")
    p.add_argument("ids", nargs="+")
    _yes_flag(p)
    _json_flag(p)
    p.set_defaults(func=cmd_artifacts_delete)
    for name, func, text in (("pin", cmd_artifacts_pin, "pin (exempt from retention)"),
                             ("unpin", cmd_artifacts_unpin, "unpin"),
                             ("seen", cmd_artifacts_seen, "mark seen"),
                             ("unseen", cmd_artifacts_unseen, "mark unseen")):
        p = artifacts_sub.add_parser(name, help=text)
        p.add_argument("ids", nargs="+")
        _json_flag(p)
        p.set_defaults(func=func)
    p = artifacts_sub.add_parser("tag", help="add or remove tags")
    p.add_argument("ids", nargs="+")
    p.add_argument("--add", action="append", help="comma separated tags to add")
    p.add_argument("--remove", action="append", help="comma separated tags to remove")
    _json_flag(p)
    p.set_defaults(func=cmd_artifacts_tag)
    p = artifacts_sub.add_parser("retention", help="give artifacts their own retention, or make them inherit again")
    p.add_argument("ids", nargs="+")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--days", type=int, help="delete these unpinned artifacts this many days after creation")
    group.add_argument("--inherit", action="store_true", help="follow the project, then the global default")
    _json_flag(p)
    p.set_defaults(func=cmd_artifacts_retention)
    p = artifacts_sub.add_parser("caption", help="set the caption")
    p.add_argument("id")
    p.add_argument("text", nargs="+")
    _json_flag(p)
    p.set_defaults(func=cmd_artifacts_caption)

    tags = subparsers.add_parser("tags", help="list tags and set their shared colors")
    tags_sub = tags.add_subparsers(dest="tags_command", metavar="<action>", required=True)
    p = tags_sub.add_parser("list", help="built-in and used tags with their colors")
    _json_flag(p)
    p.set_defaults(func=cmd_tags_list)
    p = tags_sub.add_parser("color", help="set a tag's color everywhere, or reset it")
    p.add_argument("tag")
    p.add_argument("color", nargs="?", help="#rrggbb")
    p.add_argument("--reset", action="store_true", help="back to the default (none, or the built-in color)")
    _json_flag(p)
    p.set_defaults(func=cmd_tags_color)

    p = subparsers.add_parser("search", help="full-text search over artifacts")
    p.add_argument("query", nargs="+")
    p.add_argument("--project", help="only this project (owner/name)")
    p.add_argument("--limit", type=int, default=50)
    _json_flag(p)
    p.set_defaults(func=cmd_search)

    share = subparsers.add_parser("share", help="create, list and revoke share links")
    share_sub = share.add_subparsers(dest="share_command", metavar="<action>", required=True)
    p = share_sub.add_parser("create", help="create a public link to one artifact")
    p.add_argument("id")
    p.add_argument("--expires", help="1h, 1d, 7d, 30d, <n>m|h|d|w or never (default: never)")
    p.add_argument("--direct", action="store_true", help="print only the direct link to the file")
    _json_flag(p)
    p.set_defaults(func=cmd_share_create)
    p = share_sub.add_parser("list", help="list the share links of an artifact")
    p.add_argument("id")
    _json_flag(p)
    p.set_defaults(func=cmd_share_list)
    p = share_sub.add_parser("revoke", help="revoke a share link")
    p.add_argument("token")
    _json_flag(p)
    p.set_defaults(func=cmd_share_revoke)

