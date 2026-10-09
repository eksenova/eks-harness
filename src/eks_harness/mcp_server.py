from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import quote

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import TextContent, ToolAnnotations
from pydantic import Field

from eks_harness import __version__
from eks_harness.cli.client import ApiClientError, HarnessClient, NotFound
from eks_harness.ids import is_sid, parse_project_id, slugify

SERVER_NAME = "eks-harness"
SOURCE = "mcp"
DEFAULT_MAX_IMAGE_BYTES = 4 * 1024 * 1024
IMAGE_MIMES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})

INSTRUCTIONS = """Tools for the eks-harness artifact store: projects (owner/name), sessions (usually the branch;
one session can span several projects, for example the web and mobile app of the same branch), and the artifacts (screenshots, videos, DOM/MHTML dumps, HAR, logs, sites, files) captured in them.

- A sid is the 6-character id of a live lease (browser, iOS or Android). Uploads and notes can be
  addressed by sid instead of project + session while the lease holds its resource. A released sid
  fails with a message that says how to reacquire it; use lease_status to check one.
- Every artifact comes back with links: url (the UI page), rawUrl (the file), sessionUrl and
  downloadUrl. Give the user the url and rawUrl of anything worth showing. Both need a login;
  share_artifact returns a directUrl that opens the file itself (no page, no player) without one.
- Sessions can be named by their name (for example a branch name like feature/x) or their slug.
- get_artifact can attach the image itself (image="full") or its thumbnail (image="thumbnail")."""

ProjectArg = Annotated[str, Field(description="Project id as owner/name, for example acme/web-app")]
SessionArg = Annotated[str, Field(description="Session name (often the branch name) or its slug")]
ArtifactIdArg = Annotated[str, Field(description="Artifact id (26-character ULID)")]
SidArg = Annotated[str, Field(description="Lease sid, 6 lowercase characters")]

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
IDEMPOTENT_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                                   open_world_hint=False)


def _tool_error(problem: ApiClientError) -> ToolError:
    parts = [problem.message]
    reacquire = problem.payload.get("reacquire")
    if reacquire:
        parts.append(f"Reacquire with: {reacquire}")
    return ToolError(" ".join(parts))


def _project_path(project: str) -> str:
    try:
        owner, name = parse_project_id(project.strip())
    except ValueError as problem:
        raise ToolError(str(problem)) from None
    return f"/api/projects/{quote(owner, safe='')}/{quote(name, safe='')}"


def _clean(params: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in params.items():
        if value is None or value == "" or value == []:
            continue
        if isinstance(value, bool):
            cleaned[key] = "true" if value else "false"
        else:
            cleaned[key] = value
    return cleaned


class HarnessTools:
    def __init__(self, client_factory: Callable[[], HarnessClient]) -> None:
        self._client_factory = client_factory
        self._client: HarnessClient | None = None
        self._lock = threading.Lock()

    @property
    def client(self) -> HarnessClient:
        with self._lock:
            if self._client is None:
                self._client = self._client_factory()
            return self._client

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                self._client.close()
                self._client = None

    def call(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            return self.client.request(method, path, **kwargs)
        except ApiClientError as problem:
            raise _tool_error(problem) from None

    def fetch_bytes(self, path: str, limit: int) -> bytes | None:
        try:
            with self.client.stream("GET", path) as response:
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > limit:
                    return None
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > limit:
                        return None
                    chunks.append(chunk)
                return b"".join(chunks)
        except ApiClientError as problem:
            raise _tool_error(problem) from None

    def resolve_session(self, project: str | None, session: str) -> tuple[str, dict[str, Any]]:
        base = f"{_project_path(project)}/sessions" if project else "/api/sessions"
        wanted = session.strip()
        if not wanted:
            raise ToolError("session must not be empty")
        if wanted == slugify(wanted):
            try:
                found = self.client.get(f"{base}/{quote(wanted, safe='')}")
                return f"{base}/{quote(found['slug'], safe='')}", found
            except NotFound:
                pass
            except ApiClientError as problem:
                raise _tool_error(problem) from None
        listing = self.call("GET", base)
        items = listing.get("items", []) if isinstance(listing, dict) else []
        for matcher in (lambda s: s.get("name") == wanted, lambda s: s.get("slug") == wanted,
                        lambda s: s.get("slug") == slugify(wanted),
                        lambda s: str(s.get("name", "")).casefold() == wanted.casefold()):
            for item in items:
                if matcher(item):
                    return f"{base}/{quote(item['slug'], safe='')}", item
        known = ", ".join(f"{s.get('name')} ({s.get('slug')})" for s in items[:20]) or "none"
        where = f" in project {project}" if project else ""
        raise ToolError(f"No session '{wanted}'{where}. Sessions: {known}")

    def list_projects(self) -> dict[str, Any]:
        return self.call("GET", "/api/projects")

    def list_sessions(self, project: str | None) -> dict[str, Any]:
        if project:
            return self.call("GET", f"{_project_path(project)}/sessions")
        return self.call("GET", "/api/sessions")

    def list_artifacts(self, project: str | None, session: str | None, kind: str | None, tag: str | None,
                       query: str | None, unseen: bool | None, cursor: str | None, limit: int | None) -> dict[str, Any]:
        session_slug = None
        if project:
            _project_path(project)
        if session:
            _, found = self.resolve_session(project, session)
            session_slug = found["slug"]
        params = _clean({"project": project.strip() if project else None, "session": session_slug, "kind": kind,
                         "tag": tag, "q": query, "unseen": unseen, "cursor": cursor, "limit": limit})
        return self.call("GET", "/api/artifacts", params=params)

    def get_artifact(self, artifact_id: str, image: str, max_image_bytes: int) -> list[Any]:
        artifact = self.call("GET", f"/api/artifacts/{quote(artifact_id.strip(), safe='')}")
        content: list[Any] = [artifact]
        if image == "none":
            return content
        mime = str(artifact.get("mime") or "")
        notes: list[str] = []
        if image == "full":
            if mime in IMAGE_MIMES:
                data = self.fetch_bytes(f"/raw/{quote(artifact['id'], safe='')}/{quote(artifact['filename'], safe='')}",
                                        max_image_bytes)
                if data is not None:
                    content.append(Image(data=data, format=mime.split("/", 1)[1]))
                    return content
                notes.append(f"The image is larger than {max_image_bytes} bytes; open rawUrl for the full file.")
            else:
                notes.append(f"The artifact is {mime or 'of an unknown type'}, not an image.")
        if artifact.get("thumbnailUrl"):
            data = self.fetch_bytes(f"/thumb/{quote(artifact['id'], safe='')}.jpg", max_image_bytes)
            if data is not None:
                content.append(Image(data=data, format="jpeg"))
                if notes:
                    notes.append("Its thumbnail is attached instead.")
            else:
                notes.append(f"The thumbnail is larger than {max_image_bytes} bytes and was not attached.")
        else:
            notes.append("This artifact has no thumbnail, so no image is attached.")
        content.append(TextContent(type="text", text=" ".join(notes)) if notes else None)
        return [item for item in content if item is not None]

    def upload_artifact(self, path: str, project: str | None, session: str | None, sid: str | None,
                        kind: str | None, caption: str | None, tags: list[str] | None) -> dict[str, Any]:
        if not sid and not project:
            raise ToolError("Pass sid, or project (and optionally session).")
        if sid and not is_sid(sid.strip()):
            raise ToolError(f"'{sid}' is not a sid: expected 6 lowercase characters.")
        if project:
            _project_path(project)
        file_path = Path(path).expanduser()
        if not file_path.is_absolute():
            file_path = Path.cwd() / file_path
        if file_path.is_dir():
            raise ToolError(f"{file_path} is a directory; upload a single file (zip a site first).")
        if not file_path.is_file():
            raise ToolError(f"No such file: {file_path}")
        fields = {"project": project.strip() if project else None, "session": session.strip() if session else None,
                  "sid": sid.strip() if sid else None, "kind": kind, "caption": caption or None,
                  "tags": [t.strip() for t in tags or [] if t.strip()] or None, "source": SOURCE}
        try:
            return self.client.upload("/api/artifacts", [file_path], fields)
        except ApiClientError as problem:
            raise _tool_error(problem) from None

    def share_artifact(self, artifact_id: str, expires: str | None) -> dict[str, Any]:
        body = {"expires": expires} if expires else {}
        return self.call("POST", f"/api/artifacts/{quote(artifact_id.strip(), safe='')}/shares", json=body)

    def mark_seen(self, artifact_ids: list[str], seen: bool) -> dict[str, Any]:
        ids = [i.strip() for i in artifact_ids if i and i.strip()]
        if not ids:
            raise ToolError("Pass at least one artifact id.")
        return self.call("POST", "/api/artifacts/seen", json={"ids": ids, "seen": seen})

    def tag_artifacts(self, artifact_ids: list[str], add: list[str] | None, remove: list[str] | None) -> dict[str, Any]:
        ids = [i.strip() for i in artifact_ids if i and i.strip()]
        add_tags = [t.strip() for t in add or [] if t.strip()]
        remove_tags = [t.strip() for t in remove or [] if t.strip()]
        if not ids:
            raise ToolError("Pass at least one artifact id.")
        if not add_tags and not remove_tags:
            raise ToolError("Pass tags to add or remove.")
        return self.call("POST", "/api/artifacts/tags", json={"ids": ids, "add": add_tags, "remove": remove_tags})

    def set_retention(self, artifact_ids: list[str], days: int | None) -> dict[str, Any]:
        ids = [i.strip() for i in artifact_ids if i and i.strip()]
        if not ids:
            raise ToolError("Pass at least one artifact id.")
        return self.call("POST", "/api/artifacts/retention", json={"ids": ids, "retentionDays": days})

    def search(self, query: str, limit: int | None) -> dict[str, Any]:
        if not query.strip():
            raise ToolError("query must not be empty")
        return self.call("GET", "/api/search", params=_clean({"q": query.strip(), "limit": limit}))

    def add_note(self, body: str, sid: str | None, project: str | None, session: str | None) -> dict[str, Any]:
        if not body.strip():
            raise ToolError("body must not be empty")
        sid = sid.strip() if sid else None
        if project and session:
            session_path, _ = self.resolve_session(project, session)
            return self.call("POST", f"{session_path}/notes", json=_clean({"body": body, "sid": sid}))
        if sid:
            return self.call("POST", f"/api/sid/{quote(sid, safe='')}/notes", json={"body": body})
        raise ToolError("Pass sid, or project and session.")

    def session_timeline(self, project: str | None, session: str, limit: int | None) -> dict[str, Any]:
        session_path, _ = self.resolve_session(project, session)
        timeline = self.call("GET", f"{session_path}/timeline", params=_clean({"limit": limit}))
        if limit and isinstance(timeline, dict) and isinstance(timeline.get("items"), list):
            timeline["items"] = timeline["items"][-limit:]
        return timeline

    def lease_status(self, sid: str | None, instance: str | None) -> dict[str, Any]:
        if sid:
            return self.call("GET", f"/api/sid/{quote(sid.strip(), safe='')}")
        return self.call("GET", "/api/leases", params=_clean({"instance": instance}))

    def annotate(self, artifact_id: str, spec: dict[str, Any] | str, style: str | None,
                 dry_run: bool) -> dict[str, Any]:
        if not artifact_id.strip():
            raise ToolError("artifact_id must not be empty")
        if isinstance(spec, str) and not spec.strip():
            raise ToolError("spec must not be empty")
        if isinstance(spec, dict) and not spec:
            raise ToolError("spec must not be empty")
        return self.call("POST", f"/api/artifacts/{quote(artifact_id.strip(), safe='')}/annotate",
                         json=_clean({"spec": spec, "style": style, "dryRun": dry_run or None}))

    def annotate_rerender(self, artifact_id: str, style: str | None) -> dict[str, Any]:
        if not artifact_id.strip():
            raise ToolError("artifact_id must not be empty")
        return self.call("POST", f"/api/artifacts/{quote(artifact_id.strip(), safe='')}/annotate/rerender",
                         json=_clean({"style": style}))

    def annotate_recapture(self, artifact_id: str, sid: str | None) -> dict[str, Any]:
        if not artifact_id.strip():
            raise ToolError("artifact_id must not be empty")
        if sid is not None and not is_sid(sid.strip()):
            raise ToolError(f"'{sid}' is not a sid: expected 6 lowercase characters.")
        return self.call("POST", f"/api/artifacts/{quote(artifact_id.strip(), safe='')}/annotate/recapture",
                         json=_clean({"sid": sid.strip() if sid else None}))

    def list_annotation_versions(self, artifact_id: str) -> dict[str, Any]:
        if not artifact_id.strip():
            raise ToolError("artifact_id must not be empty")
        return self.call("GET", f"/api/artifacts/{quote(artifact_id.strip(), safe='')}/annotations")

    def restore_annotation_version(self, artifact_id: str, version: int) -> dict[str, Any]:
        if not artifact_id.strip():
            raise ToolError("artifact_id must not be empty")
        if version < 1:
            raise ToolError("version must be at least 1")
        return self.call("POST", f"/api/artifacts/{quote(artifact_id.strip(), safe='')}/annotate/restore",
                         json={"version": version})


def create_server(client_factory: Callable[[], HarnessClient],
                  tools: HarnessTools | None = None) -> tuple[MCPServer, HarnessTools]:
    tools = tools or HarnessTools(client_factory)
    server = MCPServer(SERVER_NAME, title="eks-harness", instructions=INSTRUCTIONS, version=__version__,
                       log_level="WARNING")

    @server.tool(description="List the projects you can see, with session, artifact and unseen counts and links.",
                 annotations=READ_ONLY)
    def list_projects() -> dict[str, Any]:
        return tools.list_projects()

    @server.tool(description="List sessions with their artifact and unseen counts, active leases (sids) and links. "
                             "Without project it lists every session with the projects it spans (projects[]).",
                 annotations=READ_ONLY)
    def list_sessions(
        project: Annotated[str | None, Field(description="Project id as owner/name; omit for all projects")] = None,
    ) -> dict[str, Any]:
        return tools.list_sessions(project)

    @server.tool(description="List artifacts, newest first, filtered by project, session, kind, tag, text or "
                             "unseen state. Pass nextCursor from a previous page as cursor to continue.",
                 annotations=READ_ONLY)
    def list_artifacts(
        project: Annotated[str | None, Field(description="Project id as owner/name")] = None,
        session: Annotated[str | None, Field(description="Session name or slug; without project it covers "
                                                         "every project the session spans")] = None,
        kind: Annotated[str | None, Field(description="screenshot, video, dom, mhtml, a11y, har, console, log, "
                                                      "site or file")] = None,
        tag: Annotated[str | None, Field(description="Only artifacts with this tag")] = None,
        query: Annotated[str | None, Field(description="Full-text filter over filename, caption and tags")] = None,
        unseen: Annotated[bool | None, Field(description="true for only artifacts you have not seen")] = None,
        cursor: Annotated[str | None, Field(description="nextCursor from the previous page")] = None,
        limit: Annotated[int | None, Field(ge=1, le=500, description="Page size")] = None,
    ) -> dict[str, Any]:
        return tools.list_artifacts(project, session, kind, tag, query, unseen, cursor, limit)

    @server.tool(description="Get one artifact's metadata and links (url, rawUrl, sessionUrl, downloadUrl). "
                             "With image='full' an image artifact is attached as image content; with "
                             "image='thumbnail' the JPEG thumbnail of an image or video is attached.",
                 annotations=READ_ONLY, structured_output=False)
    def get_artifact(
        artifact_id: ArtifactIdArg,
        image: Annotated[Literal["none", "thumbnail", "full"],
                         Field(description="Attach nothing, the thumbnail, or the full image")] = "none",
        max_image_bytes: Annotated[int, Field(ge=1024, le=20 * 1024 * 1024,
                                              description="Largest image to attach")] = DEFAULT_MAX_IMAGE_BYTES,
    ) -> list[Any]:
        return tools.get_artifact(artifact_id, image, max_image_bytes)

    @server.tool(description="Upload a local file as an artifact, addressed by a live lease sid or by project "
                             "(and optionally session, created if new). Returns the artifact with its links.",
                 annotations=WRITE)
    def upload_artifact(
        path: Annotated[str, Field(description="Path of the local file to upload")],
        project: Annotated[str | None, Field(description="Project id as owner/name, when no sid")] = None,
        session: Annotated[str | None, Field(description="Session name; omit for a project-level artifact")] = None,
        sid: Annotated[str | None, Field(description="Live lease sid; resolves project and session")] = None,
        kind: Annotated[str | None, Field(description="Artifact kind; inferred from the file type if omitted")] = None,
        caption: Annotated[str | None, Field(description="Caption shown with the artifact")] = None,
        tags: Annotated[list[str] | None, Field(description="Tags to attach")] = None,
    ) -> dict[str, Any]:
        return tools.upload_artifact(path, project, session, sid, kind, caption, tags)

    @server.tool(description="Create a public share link for one artifact (no login needed to open it). "
                             "directUrl is the file itself with its filename, opened natively by the browser "
                             "or embedded anywhere; url is the share page with the viewer. "
                             "expires is 1h, 1d, 7d, 30d, another <n>m|h|d|w, or omitted for no expiry.",
                 annotations=WRITE)
    def share_artifact(
        artifact_id: ArtifactIdArg,
        expires: Annotated[str | None, Field(description="1h, 1d, 7d, 30d, <n>m|h|d|w, or omit")] = None,
    ) -> dict[str, Any]:
        return tools.share_artifact(artifact_id, expires)

    @server.tool(description="Mark artifacts as seen (or unseen with seen=false) for the current user.",
                 annotations=IDEMPOTENT_WRITE)
    def mark_seen(
        artifact_ids: Annotated[list[str], Field(min_length=1, description="Artifact ids")],
        seen: Annotated[bool, Field(description="false marks them unseen")] = True,
    ) -> dict[str, Any]:
        return tools.mark_seen(artifact_ids, seen)

    @server.tool(description="Add or remove tags on artifacts. Tags are 1-64 lowercase letters, digits or . _ : / + -. "
                             "Built-in tags (web, chrome, ios, android, mobile) are added to captures automatically.",
                 annotations=IDEMPOTENT_WRITE)
    def tag_artifacts(
        artifact_ids: Annotated[list[str], Field(min_length=1, description="Artifact ids")],
        add: Annotated[list[str] | None, Field(description="Tags to add")] = None,
        remove: Annotated[list[str] | None, Field(description="Tags to remove")] = None,
    ) -> dict[str, Any]:
        return tools.tag_artifacts(artifact_ids, add, remove)

    @server.tool(description="Give artifacts their own retention in days (unpinned ones are deleted that long after "
                             "creation), or pass days=null to make them follow the project and global retention "
                             "again. Pin an artifact to keep it forever.", annotations=IDEMPOTENT_WRITE)
    def set_artifact_retention(
        artifact_ids: Annotated[list[str], Field(min_length=1, description="Artifact ids")],
        days: Annotated[int | None, Field(ge=1, le=36500, description="Days to keep, or null to inherit")] = None,
    ) -> dict[str, Any]:
        return tools.set_retention(artifact_ids, days)

    @server.tool(description="Full-text search over artifact filenames, captions, tags, session names and "
                             "project ids.", annotations=READ_ONLY)
    def search(
        query: Annotated[str, Field(description="Search text")],
        limit: Annotated[int | None, Field(ge=1, le=200, description="Most hits to return")] = None,
    ) -> dict[str, Any]:
        return tools.search(query, limit)

    @server.tool(description="Add a note to a session's timeline, addressed by a live lease sid or by "
                             "project and session.", annotations=WRITE)
    def add_note(
        body: Annotated[str, Field(min_length=1, description="Note text")],
        sid: Annotated[str | None, Field(description="Lease sid; resolves the session")] = None,
        project: Annotated[str | None, Field(description="Project id as owner/name")] = None,
        session: Annotated[str | None, Field(description="Session name or slug")] = None,
    ) -> dict[str, Any]:
        return tools.add_note(body, sid, project, session)

    @server.tool(description="A session's timeline: lease events, captures and notes in chronological order. "
                             "Without project it covers every project the session spans.",
                 annotations=READ_ONLY)
    def session_timeline(
        session: SessionArg,
        project: Annotated[str | None, Field(description="Project id as owner/name; omit for all projects")] = None,
        limit: Annotated[int | None, Field(ge=1, le=2000, description="Only the most recent entries")] = None,
    ) -> dict[str, Any]:
        return tools.session_timeline(project, session, limit)

    @server.tool(description="Status of one lease by sid (valid, state, project, session, resource, links, and "
                             "how to reacquire a released one), or of all leases and the queue when no sid is given.",
                 annotations=READ_ONLY)
    def lease_status(
        sid: Annotated[str | None, Field(description="Lease sid")] = None,
        instance: Annotated[str | None, Field(description="Only leases of this owner instance, when no sid")] = None,
    ) -> dict[str, Any]:
        return tools.lease_status(sid, instance)

    @server.tool(description="Render an annotation spec onto a capture. The spec is a version 1 object "
                             "(or a JSON/YAML string) with items; returns the artifact, the new version, "
                             "the validation report and crop links. With dry_run only validation and crops "
                             "come back and no version is stored.",
                 annotations=WRITE)
    def annotate(
        artifact_id: ArtifactIdArg,
        spec: Annotated[dict[str, Any] | str, Field(description="Annotation spec object or JSON/YAML string")],
        style: Annotated[str | None, Field(description="Style name overriding the spec style")] = None,
        dry_run: Annotated[bool, Field(description="Validate only; store no version")] = False,
    ) -> dict[str, Any]:
        return tools.annotate(artifact_id, spec, style, dry_run)

    @server.tool(description="Re-render an annotated artifact from its clean source with no device. "
                             "Use for a style or text change.",
                 annotations=WRITE)
    def annotate_rerender(
        artifact_id: ArtifactIdArg,
        style: Annotated[str | None, Field(description="Re-render with this style")] = None,
    ) -> dict[str, Any]:
        return tools.annotate_rerender(artifact_id, style)

    @server.tool(description="Replay the capture recipe for a fresh capture, then re-measure, re-validate "
                             "and re-render. Needs a lease or device.",
                 annotations=WRITE)
    def annotate_recapture(
        artifact_id: ArtifactIdArg,
        sid: Annotated[str | None, Field(description="Lease sid for the replay")] = None,
    ) -> dict[str, Any]:
        return tools.annotate_recapture(artifact_id, sid)

    @server.tool(description="List the annotation versions of an artifact, oldest first, with restore targets.",
                 annotations=READ_ONLY)
    def list_annotation_versions(artifact_id: ArtifactIdArg) -> dict[str, Any]:
        return tools.list_annotation_versions(artifact_id)

    @server.tool(description="Roll an annotated artifact back to an older version. The chosen version is "
                             "copied back as a new version; history is never mutated.",
                 annotations=WRITE)
    def restore_annotation_version(
        artifact_id: ArtifactIdArg,
        version: Annotated[int, Field(ge=1, description="Version number to restore")],
    ) -> dict[str, Any]:
        return tools.restore_annotation_version(artifact_id, version)

    from eks_harness.mcp_system import register_driver_tools, register_plugin_tools, register_system_tools
    from eks_harness.mcp_video import register_video_tools

    register_system_tools(server, tools)
    register_driver_tools(server, tools)
    register_video_tools(server)
    register_plugin_tools(server, tools)
    return server, tools


def run_stdio(client_factory: Callable[[], HarnessClient]) -> None:
    server, tools = create_server(client_factory)
    try:
        server.run("stdio")
    finally:
        tools.close()
