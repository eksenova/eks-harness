from __future__ import annotations

import hashlib
import os
import secrets
import shutil
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

from python_multipart import MultipartParser
from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import parse_options_header

from eks_harness.api.errors import ApiError, bad_request, too_large

MAX_FIELD_BYTES = 1024 * 1024
MAX_FIELDS = 200
MAX_FILES = 20000


@dataclass
class UploadedFile:
    field: str
    filename: str
    content_type: str
    path: Path
    size: int = 0
    sha256: str = ""
    head: bytes = b""


@dataclass
class ParsedForm:
    fields: dict[str, list[str]] = field(default_factory=dict)
    files: list[UploadedFile] = field(default_factory=list)
    workdir: Path | None = None

    def get(self, name: str, default: str | None = None) -> str | None:
        values = self.fields.get(name)
        return values[-1] if values else default

    def get_all(self, name: str) -> list[str]:
        return list(self.fields.get(name, []))

    def files_for(self, *names: str) -> list[UploadedFile]:
        return [f for f in self.files if f.field in names]

    def cleanup(self) -> None:
        if self.workdir is not None:
            shutil.rmtree(self.workdir, ignore_errors=True)
            self.workdir = None


class _Collector:
    def __init__(self, workdir: Path, max_bytes: int) -> None:
        self.workdir = workdir
        self.max_bytes = max_bytes
        self.form = ParsedForm(workdir=workdir)
        self.total_file_bytes = 0
        self._header_field = bytearray()
        self._header_value = bytearray()
        self._headers: dict[str, bytes] = {}
        self._current: UploadedFile | None = None
        self._handle = None
        self._hash = None
        self._field_name: str | None = None
        self._field_buffer = bytearray()
        self.error: ApiError | None = None

    def on_part_begin(self) -> None:
        self._headers = {}
        self._current = None
        self._field_name = None
        self._field_buffer = bytearray()

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._header_field.extend(data[start:end])

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._header_value.extend(data[start:end])

    def on_header_end(self) -> None:
        name = bytes(self._header_field).decode("latin-1").strip().lower()
        self._headers[name] = bytes(self._header_value).strip()
        self._header_field.clear()
        self._header_value.clear()

    def on_headers_finished(self) -> None:
        disposition, params = parse_options_header(self._headers.get("content-disposition", b""))
        name = params.get(b"name", b"").decode("utf-8", "replace")
        if disposition != b"form-data" or not name:
            raise _Abort(bad_request("Every multipart part needs Content-Disposition: form-data with a name.",
                                     error="bad_multipart"))
        if b"filename" in params or b"filename*" in params:
            raw_name = params.get(b"filename*") or params.get(b"filename") or b""
            filename = _decode_filename(raw_name)
            if len(self.form.files) >= MAX_FILES:
                raise _Abort(bad_request(f"At most {MAX_FILES} files per upload.", error="too_many_files"))
            path = self.workdir / f"part-{len(self.form.files):05d}-{secrets.token_hex(4)}"
            self._current = UploadedFile(field=name, filename=filename,
                                         content_type=_text(self._headers.get("content-type", b"")), path=path)
            self._handle = open(path, "wb")
            self._hash = hashlib.sha256()
        else:
            if sum(len(v) for v in self.form.fields.values()) >= MAX_FIELDS:
                raise _Abort(bad_request(f"At most {MAX_FIELDS} form fields.", error="too_many_fields"))
            self._field_name = name

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        chunk = data[start:end]
        if self._current is not None:
            self.total_file_bytes += len(chunk)
            if self.total_file_bytes > self.max_bytes:
                raise _Abort(too_large(
                    f"The upload is larger than storage.maxUploadMb ({self.max_bytes // (1024 * 1024)} MB).",
                    limit_bytes=self.max_bytes))
            if len(self._current.head) < 64:
                self._current.head = (self._current.head + chunk)[:64]
            self._handle.write(chunk)
            self._hash.update(chunk)
            self._current.size += len(chunk)
        elif self._field_name is not None:
            self._field_buffer.extend(chunk)
            if len(self._field_buffer) > MAX_FIELD_BYTES:
                raise _Abort(too_large(f"The form field {self._field_name} is larger than 1 MB."))

    def on_part_end(self) -> None:
        if self._current is not None:
            self._handle.close()
            self._handle = None
            self._current.sha256 = self._hash.hexdigest()
            self.form.files.append(self._current)
            self._current = None
        elif self._field_name is not None:
            self.form.fields.setdefault(self._field_name, []).append(
                bytes(self._field_buffer).decode("utf-8", "replace"))
            self._field_name = None

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


class _Abort(Exception):
    def __init__(self, error: ApiError) -> None:
        super().__init__(error.message)
        self.error = error


def _make_parser(boundary: bytes, callbacks: dict) -> MultipartParser:
    try:
        return MultipartParser(boundary, callbacks, max_header_count=16, max_header_size=16384)
    except TypeError:
        return MultipartParser(boundary, callbacks)


def _text(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def _decode_filename(raw: bytes) -> str:
    text = _text(raw)
    if text.lower().startswith("utf-8''"):
        text = unquote(text[7:])
    return text


async def parse_multipart(headers, stream: AsyncIterator[bytes], tmp_root: Path, max_bytes: int) -> ParsedForm:
    content_type = headers.get("content-type", "")
    mime, params = parse_options_header(content_type)
    if mime != b"multipart/form-data" or not params.get(b"boundary"):
        raise ApiError(415, "unsupported_media_type", "Send the upload as multipart/form-data.")
    length = headers.get("content-length")
    if length and length.isdigit() and int(length) > max_bytes + 1024 * 1024:
        raise too_large(f"The upload is larger than storage.maxUploadMb ({max_bytes // (1024 * 1024)} MB).",
                        limit_bytes=max_bytes)
    tmp_root.mkdir(parents=True, exist_ok=True)
    workdir = tmp_root / f"upload-{os.getpid()}-{secrets.token_hex(8)}"
    workdir.mkdir()
    collector = _Collector(workdir, max_bytes)
    callbacks = {
        "on_part_begin": collector.on_part_begin,
        "on_part_data": collector.on_part_data,
        "on_part_end": collector.on_part_end,
        "on_header_field": collector.on_header_field,
        "on_header_value": collector.on_header_value,
        "on_header_end": collector.on_header_end,
        "on_headers_finished": collector.on_headers_finished,
    }
    parser = _make_parser(params[b"boundary"], callbacks)
    try:
        async for chunk in stream:
            if chunk:
                parser.write(chunk)
        parser.finalize()
    except _Abort as abort:
        collector.close()
        collector.form.cleanup()
        raise abort.error from None
    except MultipartParseError as problem:
        collector.close()
        collector.form.cleanup()
        raise bad_request(f"The multipart body is malformed: {problem}", error="bad_multipart") from None
    except BaseException:
        collector.close()
        collector.form.cleanup()
        raise
    collector.close()
    return collector.form
