from __future__ import annotations

import email
import hashlib
import html
import posixpath
import re
import shutil
import stat
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from email import policy
from email.message import Message
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urldefrag, urljoin, urlsplit

from eks_harness.store.layout import SITE_DIR, UnsafePath, normalize_site_path, sanitize_filename
from eks_harness.store.media import guess_mime

MAX_SITE_FILES = 20000
MAX_LISTED_FILES = 2000
IGNORED_PREFIXES = ("__MACOSX/",)
IGNORED_NAMES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})
COPY_CHUNK = 1024 * 1024


class SiteError(ValueError):
    def __init__(self, message: str, error: str = "invalid_site") -> None:
        super().__init__(message)
        self.error = error


@dataclass
class SiteInfo:
    entry: str
    files: list[str] = field(default_factory=list)
    bytes: int = 0
    source: str = "upload"
    extra: dict = field(default_factory=dict)

    def meta(self) -> dict:
        listed = sorted(self.files)[:MAX_LISTED_FILES]
        data = {"entry": self.entry, "files": listed, "fileCount": len(self.files), "bytes": self.bytes,
                "source": self.source}
        if len(self.files) > len(listed):
            data["truncated"] = True
        data.update(self.extra)
        return data


def _ignored(path: str) -> bool:
    return path.startswith(IGNORED_PREFIXES) or PurePosixPath(path).name in IGNORED_NAMES


def _strip_common_folder(paths: list[str], entry: str) -> tuple[list[str], str | None]:
    if entry in paths or not paths:
        return paths, None
    tops = {p.split("/", 1)[0] for p in paths}
    if len(tops) != 1 or not all("/" in p for p in paths):
        return paths, None
    top = next(iter(tops))
    stripped = [p.split("/", 1)[1] for p in paths]
    return (stripped, top) if entry in stripped else (paths, None)


def _check_entry(entry: str, files: Iterable[str]) -> str:
    try:
        normalized = normalize_site_path(entry or "index.html")
    except UnsafePath as problem:
        raise SiteError(f"Invalid entry: {problem}", "invalid_entry") from None
    files = list(files)
    if normalized not in files:
        pages = sorted(f for f in files if f.lower().endswith((".html", ".htm")))[:10]
        hint = f" HTML files in the upload: {', '.join(pages)}." if pages else " The upload has no HTML files."
        raise SiteError(f"The entry {normalized} is not in the upload.{hint}", "entry_missing")
    return normalized


def _place(dest_root: Path, relative: str) -> Path:
    target = dest_root.joinpath(*relative.split("/"))
    resolved_root = dest_root.resolve()
    if not target.parent.resolve().is_relative_to(resolved_root):
        raise SiteError(f"The path {relative} escapes the site root.", "unsafe_path")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except (FileExistsError, NotADirectoryError):
        raise SiteError(f"The path {relative} conflicts with a file of the same name.", "conflicting_paths") from None
    if target.exists():
        raise SiteError(f"The path {relative} appears twice.", "conflicting_paths")
    return target


def extract_zip(zip_path: Path, dest_root: Path, entry: str, max_bytes: int) -> SiteInfo:
    try:
        archive = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError) as problem:
        raise SiteError(f"The upload is not a readable zip file: {problem}", "bad_zip") from None
    with archive:
        members: list[tuple[str, zipfile.ZipInfo]] = []
        total = 0
        for info in archive.infolist():
            if info.is_dir():
                continue
            mode = (info.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                raise SiteError(f"The zip contains a symbolic link ({info.filename}); links are not allowed.",
                                "unsafe_path")
            if info.flag_bits & 0x1:
                raise SiteError("Encrypted zip files are not supported.", "bad_zip")
            try:
                name = normalize_site_path(info.filename)
            except UnsafePath as problem:
                raise SiteError(f"Rejected zip entry: {problem}", "unsafe_path") from None
            if _ignored(name):
                continue
            total += info.file_size
            members.append((name, info))
        if not members:
            raise SiteError("The zip has no files.", "empty_site")
        if len(members) > MAX_SITE_FILES:
            raise SiteError(f"The zip has more than {MAX_SITE_FILES} files.", "too_many_files")
        if total > max_bytes:
            raise SiteError(f"The zip expands to more than {max_bytes // (1024 * 1024)} MB.", "site_too_large")
        names, _ = _strip_common_folder([n for n, _ in members], normalize_site_path(entry or "index.html"))
        entry_path = _check_entry(entry, names)
        dest_root.mkdir(parents=True, exist_ok=True)
        written = 0
        for name, (_, info) in zip(names, members):
            target = _place(dest_root, name)
            with archive.open(info) as source, open(target, "wb") as sink:
                while True:
                    chunk = source.read(COPY_CHUNK)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > max_bytes:
                        raise SiteError(f"The zip expands to more than {max_bytes // (1024 * 1024)} MB.",
                                        "site_too_large")
                    sink.write(chunk)
    return SiteInfo(entry=entry_path, files=names, bytes=written, source="zip")


def place_files(files: list[tuple[str, Path]], dest_root: Path, entry: str) -> SiteInfo:
    if not files:
        raise SiteError("Send at least one file.", "empty_site")
    normalized: list[tuple[str, Path]] = []
    for raw, source in files:
        try:
            name = normalize_site_path(raw)
        except UnsafePath as problem:
            raise SiteError(f"Rejected file path: {problem}", "unsafe_path") from None
        if not _ignored(name):
            normalized.append((name, source))
    if not normalized:
        raise SiteError("The upload has no files.", "empty_site")
    if len(normalized) > MAX_SITE_FILES:
        raise SiteError(f"More than {MAX_SITE_FILES} files.", "too_many_files")
    names, _ = _strip_common_folder([n for n, _ in normalized], normalize_site_path(entry or "index.html"))
    entry_path = _check_entry(entry, names)
    dest_root.mkdir(parents=True, exist_ok=True)
    total = 0
    for name, (_, source) in zip(names, normalized):
        target = _place(dest_root, name)
        shutil.move(str(source), target)
        total += target.stat().st_size
    return SiteInfo(entry=entry_path, files=names, bytes=total, source="files")


def zip_directory(root: Path, dest: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            relative = path.relative_to(root).as_posix()
            archive.write(path, relative)
    with open(dest, "rb") as handle:
        for chunk in iter(lambda: handle.read(COPY_CHUNK), b""):
            digest.update(chunk)
    return dest.stat().st_size, digest.hexdigest()


def list_files(root: Path) -> tuple[list[str], int]:
    files, total = [], 0
    for path in root.rglob("*"):
        if path.is_file():
            files.append(path.relative_to(root).as_posix())
            total += path.stat().st_size
    return sorted(files), total


_ATTR_QUOTED = re.compile(
    r"""(?P<lead>\s(?P<name>src|href|poster|background|data|xlink:href|lowsrc)\s*=\s*)(?P<q>["'])(?P<value>.*?)(?P=q)""",
    re.IGNORECASE | re.DOTALL)
_ATTR_BARE = re.compile(
    r"""(?P<lead>\s(?P<name>src|href|poster|background|lowsrc)\s*=\s*)(?P<value>[^\s"'>=`]+)""", re.IGNORECASE)
_SRCSET = re.compile(r"""(?P<lead>\s(?:srcset|imagesrcset)\s*=\s*)(?P<q>["'])(?P<value>.*?)(?P=q)""",
                     re.IGNORECASE | re.DOTALL)
_STYLE_ATTR = re.compile(r"""(?P<lead>\sstyle\s*=\s*)(?P<q>["'])(?P<value>.*?)(?P=q)""", re.IGNORECASE | re.DOTALL)
_STYLE_BLOCK = re.compile(r"(<style\b[^>]*>)(.*?)(</style\s*>)", re.IGNORECASE | re.DOTALL)
_BASE_TAG = re.compile(r"<base\b[^>]*>", re.IGNORECASE)
_BASE_HREF = re.compile(r"""<base\b[^>]*\bhref\s*=\s*(["'])(.*?)\1""", re.IGNORECASE | re.DOTALL)
_CSS_URL = re.compile(r"""url\(\s*(?P<q>['"]?)(?P<value>.*?)(?P=q)\s*\)""", re.IGNORECASE | re.DOTALL)
_CSS_IMPORT = re.compile(r"""@import\s+(?P<q>['"])(?P<value>.*?)(?P=q)""", re.IGNORECASE)

Resolver = Callable[[str], str | None]


def _rewrite_css(text: str, resolve: Resolver) -> str:
    def url_sub(match: re.Match) -> str:
        found = resolve(match.group("value"))
        return f'url("{found}")' if found else match.group(0)

    def import_sub(match: re.Match) -> str:
        found = resolve(match.group("value"))
        return f'@import "{found}"' if found else match.group(0)

    return _CSS_IMPORT.sub(import_sub, _CSS_URL.sub(url_sub, text))


def _rewrite_html(text: str, resolve: Resolver) -> str:
    def html_resolve(value: str) -> str | None:
        return resolve(html.unescape(value))

    def attr_sub(match: re.Match) -> str:
        found = html_resolve(match.group("value"))
        if not found:
            return match.group(0)
        quote_char = match.groupdict().get("q") or '"'
        return f"{match.group('lead')}{quote_char}{html.escape(found, quote=True)}{quote_char}"

    def srcset_sub(match: re.Match) -> str:
        parts = []
        changed = False
        for candidate in html.unescape(match.group("value")).split(","):
            bits = candidate.strip().split(None, 1)
            if not bits:
                continue
            found = resolve(bits[0])
            if found:
                changed = True
                bits[0] = found
            parts.append(" ".join(bits))
        if not changed:
            return match.group(0)
        return f"{match.group('lead')}{match.group('q')}{html.escape(', '.join(parts), quote=True)}{match.group('q')}"

    def style_attr_sub(match: re.Match) -> str:
        rewritten = _rewrite_css(html.unescape(match.group("value")), resolve)
        return f"{match.group('lead')}{match.group('q')}{html.escape(rewritten, quote=True)}{match.group('q')}"

    def style_block_sub(match: re.Match) -> str:
        return match.group(1) + _rewrite_css(match.group(2), resolve) + match.group(3)

    text = _BASE_TAG.sub("", text)
    text = _STYLE_BLOCK.sub(style_block_sub, text)
    text = _STYLE_ATTR.sub(style_attr_sub, text)
    text = _SRCSET.sub(srcset_sub, text)
    text = _ATTR_QUOTED.sub(attr_sub, text)
    text = _ATTR_BARE.sub(attr_sub, text)
    return text


def _charset(part: Message) -> str:
    charset = part.get_content_charset() or "utf-8"
    try:
        "".encode(charset)
    except LookupError:
        return "utf-8"
    return charset


def _content_id(part: Message) -> str | None:
    value = part.get("Content-ID")
    if not value:
        return None
    return value.strip().strip("<>").strip()


def _extension_for(part: Message, url: str) -> str:
    mime = part.get_content_type()
    known = {"text/html": ".html", "text/css": ".css", "image/png": ".png", "image/jpeg": ".jpg",
             "image/gif": ".gif", "image/webp": ".webp", "image/svg+xml": ".svg", "font/woff2": ".woff2",
             "font/woff": ".woff", "application/font-woff": ".woff", "application/font-woff2": ".woff2",
             "text/javascript": ".js", "application/javascript": ".js", "image/x-icon": ".ico",
             "image/vnd.microsoft.icon": ".ico", "image/avif": ".avif", "font/ttf": ".ttf", "font/otf": ".otf"}
    if mime in known:
        return known[mime]
    suffix = PurePosixPath(urlsplit(url).path).suffix.lower()
    return suffix if re.fullmatch(r"\.[a-z0-9]{1,8}", suffix or "") else ".bin"


def _local_name(index: int, part: Message, url: str) -> str:
    stem = PurePosixPath(unquote(urlsplit(url).path)).stem if url and not url.startswith("cid:") else ""
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-.")[:40] or "resource"
    return f"res/{index:04d}-{stem}{_extension_for(part, url)}"


def _relative(from_path: str, to_path: str) -> str:
    start = posixpath.dirname(from_path) or "."
    return posixpath.relpath(to_path, start)


def unpack_mhtml(mhtml_path: Path, dest_root: Path) -> SiteInfo:
    with open(mhtml_path, "rb") as handle:
        message = email.message_from_binary_file(handle, policy=policy.compat32)
    parts = [p for p in message.walk() if not p.is_multipart()]
    if not parts:
        raise SiteError("The MHTML file has no parts.", "bad_mhtml")
    start_id = None
    if message.is_multipart():
        start = message.get_param("start")
        if isinstance(start, str):
            start_id = start.strip("<>")
    main_index = None
    for index, part in enumerate(parts):
        if start_id and _content_id(part) == start_id:
            main_index = index
            break
    if main_index is None:
        main_index = next((i for i, p in enumerate(parts) if p.get_content_type() in ("text/html",
                                                                                      "application/xhtml+xml")), None)
    if main_index is None:
        raise SiteError("The MHTML file has no HTML document.", "bad_mhtml")
    local: dict[int, str] = {}
    by_url: dict[str, str] = {}
    counter = 0
    for index, part in enumerate(parts):
        location = (part.get("Content-Location") or "").strip()
        if index == main_index:
            local[index] = "index.html"
        else:
            counter += 1
            local[index] = _local_name(counter, part, location)
        if location:
            by_url.setdefault(location, local[index])
            by_url.setdefault(urldefrag(location)[0], local[index])
        content_id = _content_id(part)
        if content_id:
            by_url.setdefault(f"cid:{content_id}", local[index])
    main_location = (parts[main_index].get("Content-Location") or "").strip()
    dest_root.mkdir(parents=True, exist_ok=True)
    total = 0
    rewritten_links = 0
    files: list[str] = []
    for index, part in enumerate(parts):
        payload = part.get_payload(decode=True) or b""
        own_path = local[index]
        own_location = (part.get("Content-Location") or "").strip() or main_location
        mime = part.get_content_type()

        def resolve(value: str, own_path: str = own_path, base: str = own_location) -> str | None:
            nonlocal rewritten_links
            candidate = (value or "").strip()
            if not candidate or candidate.startswith(("data:", "javascript:", "#", "mailto:", "about:", "blob:")):
                return None
            if candidate.lower().startswith("cid:"):
                target = by_url.get(candidate) or by_url.get("cid:" + candidate[4:].strip("<>"))
                fragment = ""
            else:
                absolute = urljoin(base, candidate) if base else candidate
                without_fragment, fragment = urldefrag(absolute)
                target = by_url.get(absolute) or by_url.get(without_fragment)
            if not target:
                return None
            rewritten_links += 1
            relative = _relative(own_path, target)
            return f"{relative}#{fragment}" if fragment else relative

        if mime in ("text/html", "application/xhtml+xml", "text/css"):
            charset = _charset(part)
            text = payload.decode(charset, "surrogateescape")
            base_match = _BASE_HREF.search(text) if mime != "text/css" else None
            if base_match:
                base_href = html.unescape(base_match.group(2)).strip()
                own_location = urljoin(own_location, base_href) if own_location else base_href

                def resolve_with_base(value: str, _inner=resolve, _base=own_location) -> str | None:
                    return _inner(value, base=_base)

                active = resolve_with_base
            else:
                active = resolve
            text = _rewrite_css(text, active) if mime == "text/css" else _rewrite_html(text, active)
            payload = text.encode(charset, "surrogateescape")
        target = dest_root.joinpath(*own_path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        total += len(payload)
        files.append(own_path)
    return SiteInfo(entry="index.html", files=files, bytes=total, source="mhtml",
                    extra={"url": main_location or None, "resources": len(files) - 1, "rewrittenLinks": rewritten_links})


def dom_site(html_path: Path, dest_root: Path, base_url: str | None) -> SiteInfo:
    data = html_path.read_bytes()
    if base_url and base_url.startswith(("http://", "https://")) and not re.search(rb"<base\b", data[:65536], re.I):
        tag = f'<base href="{html.escape(base_url, quote=True)}">'.encode("utf-8")
        match = re.search(rb"<head\b[^>]*>", data[:65536], re.I)
        if match:
            data = data[:match.end()] + tag + data[match.end():]
        else:
            data = tag + data
    dest_root.mkdir(parents=True, exist_ok=True)
    (dest_root / "index.html").write_bytes(data)
    return SiteInfo(entry="index.html", files=["index.html"], bytes=len(data), source="dom",
                    extra={"baseUrl": base_url} if base_url else {})


def site_file_mime(path: str) -> str:
    return guess_mime(path) or "application/octet-stream"


def site_name(raw: str | None, fallback: str = "site") -> str:
    name = sanitize_filename(raw or fallback, fallback)
    if name.lower().endswith(".zip"):
        name = name[:-4] or fallback
    return f"{name}.zip"


__all__ = ["SITE_DIR", "SiteError", "SiteInfo", "dom_site", "extract_zip", "list_files", "place_files",
           "site_file_mime", "site_name", "unpack_mhtml", "zip_directory"]
