"""Long-form talking head with explicit YouTube chapter markers.

A five-minute horizontal talking head. :class:`SceneMarkers` declares a
``chapters`` stream so each chapter title overlay can anchor to
``MarkerRef(name="chapters", index=N)`` rather than hard-coded times.
The ``__main__`` block also writes a ``youtube_chapters.txt`` file
next to ``project.json``, in the ``MM:SS Title`` format YouTube's
description box accepts verbatim.

Chapter overlays use a short :class:`TextOverlay` strip in the lower
quarter so they read like broadcast chyron cards without competing
with the speaker's face.
"""

from dataclasses import dataclass

from eks_harness.video import (
    MarkerRef,
    Project,
    SceneMarkers,
    Seconds,
    Segment,
    TextOverlay,
    Track,
    VideoFile,
)

_DURATION = 300.0


@dataclass(frozen=True)
class Chapter:
    index: int
    title: str
    start_seconds: float


_CHAPTERS: tuple[Chapter, ...] = (
    Chapter(0, "Intro",                    0.0),
    Chapter(1, "The problem",             30.0),
    Chapter(2, "Why nothing else worked", 75.0),
    Chapter(3, "Our approach",           135.0),
    Chapter(4, "Live demo",              195.0),
    Chapter(5, "Wrap-up",                270.0),
)


def _chapter_card(chapter: Chapter) -> TextOverlay:
    return TextOverlay(
        text=chapter.title,
        size=56,
        x=120,
        y=860,
        color=(255, 255, 255, 255),
        start=MarkerRef(name="chapters", index=chapter.index),
        duration=6.0,
    )


def _format_youtube_timestamp(seconds: float) -> str:
    total = int(seconds)
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def render_youtube_chapters(chapters: tuple[Chapter, ...] = _CHAPTERS) -> str:
    return "\n".join(
        f"{_format_youtube_timestamp(c.start_seconds)} {c.title}" for c in chapters
    )


project = Project(
    fps=30,
    resolution=(1920, 1080),
    duration=_DURATION,
    markers=[
        SceneMarkers(
            name="chapters",
            source="tracks[0].segments[0]",
            threshold=35.0,
        ),
    ],
    tracks=[
        Track(
            name="main",
            segments=[
                Segment(
                    id="talk",
                    start=Seconds(t=0.0),
                    media=VideoFile(path="assets/longform_talk.mp4"),
                    in_=Seconds(t=0.0),
                    out=Seconds(t=_DURATION),
                    effects=[_chapter_card(c) for c in _CHAPTERS],
                ),
            ],
        ),
    ],
)


if __name__ == "__main__":
    from pathlib import Path

    out_dir = Path("./out")
    out_dir.mkdir(exist_ok=True)
    (out_dir / "project.json").write_text(
        project.model_dump_json(by_alias=True, indent=2),
        encoding="utf-8",
    )
    (out_dir / "youtube_chapters.txt").write_text(
        render_youtube_chapters() + "\n",
        encoding="utf-8",
    )
