#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import claude_config, enabled, iter_transcript, payload, project_dir, text_of  # noqa: E402

ULID = r"[0-9A-HJKMNP-TV-Z]{26}"
RAW_LINK = re.compile(rf"/raw/({ULID})/([^\s\"'?,)\]]+)")
ANY_ID = re.compile(rf"\b({ULID})\b")
IMAGE = re.compile(r"\.(?:png|jpe?g|webp)$", re.IGNORECASE)
VIDEO = re.compile(r"\.(?:mp4|webm|mov)$", re.IGNORECASE)
CLI = r"\b(?:eks-harness|ehx)\s+(?:--\S+\s+)*"
RUN_COMMAND = re.compile(CLI + r"(?:run|do|debug|runs\s+(?:show|replay|control))\b")
CAPTURE_COMMAND = re.compile(CLI + r"(?:capture|upload|annotate)\b")
DOWNLOAD_COMMAND = re.compile(CLI + r"artifacts\s+download\b")
SHEET_COMMAND = re.compile(CLI + r"video\s+(?:sheet|check)\b")
REPORT_LINE = re.compile(r"^\s*(screenshot|video)\b(.*)$")
RUN_TOOLS = ("__eks_run", "__eks_do", "__eks_session_do", "__eks_run_status", "__eks_run_control")
SHOT_KINDS = ("screenshot",)
VIDEO_KINDS = ("video",)
SKIPPED_KEYS = frozenset({"pause", "snapshots"})


def _walk_artifacts(value: Any, found: list[dict[str, Any]]) -> None:
    if isinstance(value, dict):
        kind = value.get("kind")
        ident = value.get("id")
        if isinstance(kind, str) and isinstance(ident, str) and re.fullmatch(ULID, ident) \
                and kind in (*SHOT_KINDS, *VIDEO_KINDS):
            raw = str(value.get("rawUrl") or value.get("raw_url") or "")
            match = RAW_LINK.search(raw)
            found.append({"kind": kind, "id": ident, "name": value.get("name") or ident,
                          "file": match.group(2) if match else value.get("name")})
        for key, item in value.items():
            if key not in SKIPPED_KEYS:
                _walk_artifacts(item, found)
    elif isinstance(value, list):
        for item in value:
            _walk_artifacts(item, found)


def run_captures(output: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(output)
    except ValueError:
        data = None
    if isinstance(data, (dict, list)):
        found: list[dict[str, Any]] = []
        _walk_artifacts(data, found)
        return found
    found = []
    for line in output.splitlines():
        head = REPORT_LINE.match(line)
        if not head:
            continue
        kind, rest = head.group(1), head.group(2)
        raw = RAW_LINK.search(rest)
        ident = raw.group(1) if raw else (ANY_ID.search(rest).group(1) if ANY_ID.search(rest) else None)
        if ident is None:
            continue
        words = rest.split()
        name = words[0] if words and "://" not in words[0] else ident
        found.append({"kind": kind, "id": ident, "name": name, "file": raw.group(2) if raw else None})
    return found


def capture_links(output: str) -> list[dict[str, Any]]:
    found = []
    for ident, filename in RAW_LINK.findall(output):
        if IMAGE.search(filename):
            found.append({"kind": "screenshot", "id": ident, "name": filename, "file": filename})
        elif VIDEO.search(filename):
            found.append({"kind": "video", "id": ident, "name": filename, "file": filename})
    return found


def main() -> None:
    data = payload()
    if data.get("stop_hook_active"):
        return
    if not enabled("evidence", claude_config(project_dir(data))):
        return
    path = data.get("transcript_path")
    if not path or not os.path.isfile(path):
        return
    results: dict[str, str] = {}
    events: list[tuple[int, str, str, dict]] = []
    for index, entry in enumerate(iter_transcript(path)):
        content = (entry.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "tool_use":
                events.append((index, item.get("id"), item.get("name") or "", item.get("input") or {}))
            elif item.get("type") == "tool_result":
                results[item.get("tool_use_id")] = text_of(item.get("content"))

    def later(line: int, predicate) -> bool:
        return any(other > line and predicate(name, given) for other, _, name, given in events)

    def command_of(given: dict) -> str:
        return str(given.get("command") or "")

    def read_later(line: int, needle: str | None) -> bool:
        return bool(needle) and later(line, lambda n, i: n == "Read" and needle in str(i.get("file_path") or ""))

    def downloaded_later(line: int, ident: str) -> bool:
        return later(line, lambda n, i: n == "Bash" and ident in command_of(i)
                     and bool(DOWNLOAD_COMMAND.search(command_of(i))))

    def image_fetched_later(line: int, ident: str) -> bool:
        return later(line, lambda n, i: n.endswith("__get_artifact") and str(i.get("artifact_id") or "") == ident
                     and str(i.get("image") or "none") == "full")

    def sheet_later(line: int, ident: str) -> bool:
        if later(line, lambda n, i: (n.endswith("__video_sheet") and str(i.get("artifact_id") or "") == ident
                                     and i.get("images", True) is not False)
                 or (n.endswith("__video_check") and str(i.get("artifact_id") or "") == ident)):
            return True
        sheets = [other for other, _, n, i in events
                  if other > line and n == "Bash" and ident in command_of(i) and SHEET_COMMAND.search(command_of(i))]
        return any(later(sheet, lambda n, i: n == "Read") for sheet in sheets)

    def frames_later(line: int, ident: str) -> bool:
        return later(line, lambda n, i: n == "Bash" and "ffmpeg" in command_of(i) and ident in command_of(i))

    owed: list[str] = []
    seen: set[str] = set()
    for line, tool_id, name, given in events:
        output = results.get(tool_id, "")
        command = command_of(given) if name == "Bash" else ""
        if (name == "Bash" and RUN_COMMAND.search(command)) or name.endswith(RUN_TOOLS):
            captures = run_captures(output)
        elif name == "Bash" and CAPTURE_COMMAND.search(command):
            captures = capture_links(output)
        else:
            continue
        for capture in captures:
            ident = capture["id"]
            if ident in seen:
                continue
            seen.add(ident)
            opened = read_later(line, ident) or image_fetched_later(line, ident) or (
                downloaded_later(line, ident) and read_later(line, capture.get("file")))
            if capture["kind"] in SHOT_KINDS and not opened:
                owed.append(f"screenshot {capture['name']}: eks-harness artifacts download {ident} -o <dir>, then "
                            f"Read the file (or get_artifact(artifact_id='{ident}', image='full'))")
            if capture["kind"] in VIDEO_KINDS and not (opened or sheet_later(line, ident)
                                                       or frames_later(line, ident)):
                owed.append(f"video {capture['name']}: eks-harness video sheet {ident}, then Read the sheet "
                            f"(or video_sheet(artifact_id='{ident}'))")
    if not owed:
        return
    sys.stderr.write("You captured evidence you have not looked at. Open each item and note what is really on "
                     "screen (state, text, anything broken) before you report:\n"
                     + "\n".join(f"  - {item}" for item in owed) + "\n")
    sys.exit(2)


if __name__ == "__main__":
    main()
