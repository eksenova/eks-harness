from __future__ import annotations

import zipfile
from collections.abc import Iterator, Sequence
from pathlib import Path, PurePosixPath

from eks_harness.db.repos.artifacts import Artifact
from eks_harness.paths import Paths
from eks_harness.store import layout

CHUNK = 256 * 1024
STORED_PREFIXES = ("image/", "video/", "audio/")
STORED_MIMES = frozenset({"application/zip", "application/gzip", "application/pdf", "font/woff2"})


class _Sink:
    def __init__(self) -> None:
        self.buffer = bytearray()
        self.position = 0

    def write(self, data: bytes) -> int:
        self.buffer.extend(data)
        self.position += len(data)
        return len(data)

    def tell(self) -> int:
        return self.position

    def flush(self) -> None:
        return None

    def take(self) -> bytes:
        data = bytes(self.buffer)
        self.buffer.clear()
        return data


def archive_names(items: Sequence[Artifact]) -> list[str]:
    used: set[str] = set()
    names = []
    for artifact in items:
        folder = layout.session_dir_name(artifact.session_slug)
        stem, suffix = PurePosixPath(artifact.filename).stem, PurePosixPath(artifact.filename).suffix
        candidate = f"{folder}/{artifact.filename}"
        counter = 2
        while candidate.lower() in used:
            candidate = f"{folder}/{stem} ({counter}){suffix}"
            counter += 1
        used.add(candidate.lower())
        names.append(candidate)
    return names


def stream_zip(paths: Paths, items: Sequence[Artifact]) -> Iterator[bytes]:
    sink = _Sink()
    with zipfile.ZipFile(sink, "w", allowZip64=True) as archive:
        for artifact, name in zip(items, archive_names(items)):
            try:
                source = layout.file_path(paths, artifact.rel_path)
            except layout.UnsafePath:
                continue
            if not source.is_file():
                continue
            info = zipfile.ZipInfo.from_file(source, name)
            stored = artifact.mime.startswith(STORED_PREFIXES) or artifact.mime in STORED_MIMES
            info.compress_type = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
            with open(source, "rb") as handle, archive.open(info, "w", force_zip64=True) as target:
                while True:
                    chunk = handle.read(CHUNK)
                    if not chunk:
                        break
                    target.write(chunk)
                    if len(sink.buffer) >= CHUNK:
                        yield sink.take()
            yield sink.take()
    yield sink.take()


def zip_filename(items: Sequence[Artifact]) -> str:
    slugs = {a.session_slug for a in items}
    projects = {a.project_id for a in items}
    if len(projects) == 1 and len(slugs) == 1:
        project = next(iter(projects)).replace("/", "-")
        return f"{project}-{layout.session_dir_name(next(iter(slugs)))}.zip"
    return "artifacts.zip"


def existing(paths: Paths, items: Sequence[Artifact]) -> list[Artifact]:
    found = []
    for artifact in items:
        try:
            if Path(layout.file_path(paths, artifact.rel_path)).is_file():
                found.append(artifact)
        except layout.UnsafePath:
            continue
    return found
