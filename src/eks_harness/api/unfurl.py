from __future__ import annotations

import html
import re
import threading
from datetime import datetime, timezone

from fastapi import Request

from eks_harness.api.links import Links
from eks_harness.db.repos.artifacts import Artifact
from eks_harness.db.repos.shares import Share
from eks_harness.paths import Paths
from eks_harness.store import layout, media

SITE_NAME = "eks-harness"
THEME_COLOR = "#1f3a60"
TITLE_LIMIT = 90
DESCRIPTION_LIMIT = 200
PREVIEW_BOTS = re.compile(
    r"facebookexternalhit|Facebot|Twitterbot|Slackbot|Discordbot|LinkedInBot|TelegramBot|WhatsApp/|"
    r"SkypeUriPreview|MicrosoftPreview|redditbot|Pinterestbot|vkShare|Mastodon/|Bluesky|Cardyb|Iframely|"
    r"Embedly|Mattermost-Bot|Rocket\.Chat|Synapse \(bot|ZulipURLPreview|NotionEmbedder|Snap URL Preview|"
    r"kakaotalk-scrap|line-poker|Google-PageRenderer|XING-contenttabreceiver|Yahoo Link Preview",
    re.IGNORECASE)
KIND_LABELS = {"screenshot": "Screenshot", "video": "Video", "audio": "Audio", "dom": "DOM snapshot",
               "mhtml": "Page snapshot",
               "a11y": "Accessibility tree", "har": "Network log (HAR)", "console": "Console log", "log": "Log",
               "site": "Site", "file": "File"}
_POSTER_LOCK = threading.Lock()


def is_preview_bot(request: Request) -> bool:
    return bool(PREVIEW_BOTS.search(request.headers.get("user-agent", "")))


def has_visual(artifact: Artifact) -> bool:
    return artifact.mime in media.FFMPEG_DEMUXERS


def poster_url(links: Links, share: Share) -> str:
    return links.absolute(f"/t/{share.token}.jpg")


def ensure_poster(paths: Paths, artifact: Artifact) -> bool:
    try:
        source = layout.file_path(paths, artifact.rel_path)
        poster = layout.poster_path(paths, artifact.rel_path)
    except layout.UnsafePath:
        return False
    if not source.is_file():
        return False
    with _POSTER_LOCK:
        if poster.is_file() and poster.stat().st_mtime >= source.stat().st_mtime:
            return True
        return media.make_poster(source, poster, artifact.mime)


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _size(size: int) -> str:
    value = float(size)
    for unit in ("bytes", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} bytes" if unit == "bytes" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _duration(ms: int) -> str:
    seconds = round(ms / 1000)
    return f"{seconds // 60}:{seconds % 60:02d}"


def title_of(artifact: Artifact) -> str:
    caption = (artifact.caption or "").strip()
    return _clip(caption.splitlines()[0] if caption else artifact.filename, TITLE_LIMIT)


def description_of(artifact: Artifact) -> str:
    facts: list[str] = [KIND_LABELS.get(artifact.kind) or artifact.kind.title()]
    if artifact.width and artifact.height:
        facts.append(f"{artifact.width}×{artifact.height}")
    if artifact.duration_ms:
        facts.append(_duration(artifact.duration_ms))
    facts.append(_size(artifact.size))
    captured = datetime.fromtimestamp(artifact.created_at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    facts.append(f"captured {captured}")
    summary = " · ".join(facts)
    caption = (artifact.caption or "").strip()
    rest = " ".join(caption.splitlines()[1:]).strip() if caption else ""
    if caption and title_of(artifact) != artifact.filename:
        summary = f"{artifact.filename} · {summary}"
    return _clip(f"{rest} {summary}".strip() if rest else summary, DESCRIPTION_LIMIT)


def meta_tags(links: Links, share: Share, artifact: Artifact, page_url: str) -> str:
    title = title_of(artifact)
    description = description_of(artifact)
    video = artifact.mime.startswith("video/") and has_visual(artifact)
    tags: list[tuple[str, str, str]] = [
        ("property", "og:site_name", SITE_NAME),
        ("property", "og:type", "video.other" if video else "website"),
        ("property", "og:title", title),
        ("property", "og:description", description),
        ("property", "og:url", page_url),
        ("name", "description", description),
        ("name", "theme-color", THEME_COLOR),
        ("name", "twitter:title", title),
        ("name", "twitter:description", description),
    ]
    if has_visual(artifact):
        image = poster_url(links, share)
        tags += [("property", "og:image", image), ("property", "og:image:secure_url", image),
                 ("property", "og:image:type", "image/jpeg"),
                 ("property", "og:image:alt", artifact.caption.strip() or artifact.filename)]
        size = media.poster_size(artifact.width, artifact.height)
        if size:
            tags += [("property", "og:image:width", str(size[0])), ("property", "og:image:height", str(size[1]))]
        tags += [("name", "twitter:card", "summary_large_image"), ("name", "twitter:image", image),
                 ("name", "twitter:image:alt", artifact.caption.strip() or artifact.filename)]
    else:
        tags.append(("name", "twitter:card", "summary"))
    if video:
        stream = links.share_raw(share.token)
        tags += [("property", "og:video", stream), ("property", "og:video:url", stream),
                 ("property", "og:video:secure_url", stream), ("property", "og:video:type", artifact.mime)]
        if artifact.width and artifact.height:
            tags += [("property", "og:video:width", str(artifact.width)),
                     ("property", "og:video:height", str(artifact.height))]
    return "".join(f'<meta {attr}="{key}" content="{html.escape(value, quote=True)}">' for attr, key, value in tags)


def card_page(links: Links, share: Share, artifact: Artifact, page_url: str) -> str:
    title = html.escape(title_of(artifact))
    target = html.escape(page_url, quote=True)
    return (f"<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><title>{title}</title>"
            f"{meta_tags(links, share, artifact, page_url)}<link rel=\"canonical\" href=\"{target}\"></head>"
            f"<body><p><a href=\"{target}\">{title}</a></p><p>{html.escape(description_of(artifact))}</p>"
            f"</body></html>")
