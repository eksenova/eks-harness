from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
import threading
import time
from collections.abc import Iterable, Iterator
from pathlib import Path

HASH = re.compile(r"[0-9a-f]{64}")
CHUNK = 1 << 20


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


class BlobError(ValueError):
    pass


class BlobStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, digest: str) -> Path:
        if not HASH.fullmatch(digest):
            raise BlobError(f"'{digest}' is not a sha256 hex digest")
        return self.root / digest[:2] / digest

    def has(self, digest: str) -> bool:
        try:
            return self.path(digest).is_file()
        except BlobError:
            return False

    def touch(self, digest: str) -> None:
        try:
            os.utime(self.path(digest))
        except OSError:
            pass

    def put_file(self, source: Path, *, move: bool = False) -> tuple[str, int]:
        digest = sha256_file(source)
        target = self.path(digest)
        size = source.stat().st_size
        if target.is_file():
            self.touch(digest)
            if move:
                source.unlink(missing_ok=True)
            return digest, size
        target.parent.mkdir(parents=True, exist_ok=True)
        if move:
            shutil.move(str(source), target)
        else:
            tmp = target.with_name(f"{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            shutil.copyfile(source, tmp)
            os.replace(tmp, target)
        return digest, size

    def put_stream(self, chunks: Iterable[bytes], expected: str | None = None) -> tuple[str, int]:
        self.root.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        fd, name = tempfile.mkstemp(dir=self.root, prefix=".upload-")
        try:
            with os.fdopen(fd, "wb") as handle:
                for chunk in chunks:
                    digest.update(chunk)
                    size += len(chunk)
                    handle.write(chunk)
            value = digest.hexdigest()
            if expected and value != expected:
                raise BlobError(f"content hash {value} does not match {expected}")
            target = self.path(value)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(name, target)
            return value, size
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def iter_chunks(self, digest: str) -> Iterator[bytes]:
        with self.path(digest).open("rb") as handle:
            yield from iter(lambda: handle.read(CHUNK), b"")

    def materialize(self, digest: str, target: Path) -> Path:
        source = self.path(digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.unlink()
        try:
            os.link(source, target)
        except OSError:
            shutil.copyfile(source, target)
        self.touch(digest)
        return target

    def prune(self, max_age_seconds: float) -> int:
        if not self.root.is_dir():
            return 0
        cutoff = time.time() - max_age_seconds
        removed = 0
        for path in self.root.glob("??/*"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        return removed

    def size(self) -> int:
        return sum(p.stat().st_size for p in self.root.glob("??/*") if p.is_file()) if self.root.is_dir() else 0
