#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import claude_config, enabled, iter_transcript, payload, project_dir, text_of  # noqa: E402

RAW_LINK = re.compile(r"/raw/([0-9A-HJKMNP-TV-Z]{26})/([^\s\"'?,)\]]+)")
IMAGE = re.compile(r"\.(?:png|jpe?g|webp)$", re.IGNORECASE)
VIDEO = re.compile(r"\.(?:mp4|webm|mov)$", re.IGNORECASE)
FLOW_COMMAND = re.compile(r"\b(eks-harness|ehx)\s+(?:--\S+\s+)*flow\s+run\b")
CAPTURE_COMMAND = re.compile(r"\b(eks-harness|ehx)\s+(?:--\S+\s+)*(capture|upload)\b")
DOWNLOAD_COMMAND = re.compile(r"\b(eks-harness|ehx)\s+(?:--\S+\s+)*artifacts\s+download\b")
FLOW_HEAD = re.compile(r"^(screenshot|video) (\S+)")
CAPTURE_TOOLS = ("flow_run", "driver_capture")


def flow_captures(output: str) -> list[dict]:
    try:
        report = json.loads(output)
    except ValueError:
        report = None
    if isinstance(report, dict) and isinstance(report.get("artifacts"), list):
        found = []
        for art in report["artifacts"]:
            raw = str(art.get("raw") or "")
            match = RAW_LINK.search(raw)
            if art.get("kind") in ("screenshot", "video") and match:
                found.append({"kind": art["kind"], "name": art.get("name"), "id": match.group(1),
                              "local": art.get("local"), "sheet": art.get("sheet")})
        return found
    blocks: list[dict] = []
    current = None
    for line in output.splitlines():
        head = FLOW_HEAD.match(line)
        if head:
            current = {"kind": head.group(1), "name": head.group(2)}
            blocks.append(current)
            continue
        if current is None or not line.startswith("  "):
            current = None
            continue
        key, _, value = line.strip().partition(": ")
        if key == "direct":
            match = RAW_LINK.search(value)
            if match:
                current["id"] = match.group(1)
        elif key == "local":
            current["local"] = value.strip()
        elif key == "contact sheet":
            current["sheet"] = value.strip()
    return [b for b in blocks if b.get("id")]


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

    def read_later(line: int, needle: str | None) -> bool:
        return bool(needle) and later(line, lambda n, i: n == "Read" and needle in (i.get("file_path") or ""))

    def frames_later(line: int, needle: str) -> bool:
        return later(line, lambda n, i: n == "Bash" and "ffmpeg" in (i.get("command") or "")
                     and needle in (i.get("command") or ""))

    owed: list[str] = []
    seen: set[str] = set()
    for line, tool_id, name, given in events:
        output = results.get(tool_id, "")
        command = given.get("command", "") if name == "Bash" else ""
        is_flow = (name == "Bash" and FLOW_COMMAND.search(command)) or name.endswith("__flow_run")
        is_capture = (name == "Bash" and CAPTURE_COMMAND.search(command)) or name.endswith("__driver_capture")
        if is_flow:
            for block in flow_captures(output):
                if block["id"] in seen:
                    continue
                seen.add(block["id"])
                opened = read_later(line, block.get("local")) or read_later(line, block["id"])
                if block["kind"] == "screenshot" and not opened:
                    where = block.get("local") or (f"a local copy whose path contains {block['id']} "
                                                   f"(eks-harness artifacts download {block['id']})")
                    owed.append(f"screenshot {block['name']}: Read {where}")
                if block["kind"] == "video" and not (opened or read_later(line, block.get("sheet"))
                                                     or frames_later(line, block["id"])):
                    owed.append(f"video {block['name']}: Read its contact sheet {block.get('sheet') or ''}".rstrip())
            continue
        if not is_capture:
            continue
        for artifact_id, filename in RAW_LINK.findall(output):
            if artifact_id in seen or not (IMAGE.search(filename) or VIDEO.search(filename)):
                continue
            seen.add(artifact_id)
            downloaded = later(line, lambda n, i: n == "Bash" and artifact_id in (i.get("command") or "")
                               and DOWNLOAD_COMMAND.search(i.get("command") or ""))
            if IMAGE.search(filename) and not (downloaded and read_later(line, artifact_id)):
                owed.append(f"screenshot {artifact_id} ({filename}): eks-harness artifacts download {artifact_id}, "
                            f"then Read the file")
            if VIDEO.search(filename) and not (downloaded and (frames_later(line, artifact_id)
                                                               or read_later(line, artifact_id))):
                owed.append(f"video {artifact_id} ({filename}): download it, extract frames with ffmpeg and Read them")
    if not owed:
        return
    sys.stderr.write("You captured evidence you have not looked at. Open each item and note what is really on "
                     "screen (state, text, anything broken) before you report:\n"
                     + "\n".join(f"  - {item}" for item in owed) + "\n")
    sys.exit(2)


if __name__ == "__main__":
    main()
