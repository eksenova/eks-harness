from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MIN_SCREEN_SEC = 2.0
HIGHLIGHT_SEC = 0.5


@dataclass(frozen=True)
class TimelineStep:
    t: float
    action: str
    boxes: tuple[dict[str, Any], ...] = ()
    page: str = ""
    ok: bool = True


@dataclass(frozen=True)
class CaptionCue:
    start: float
    end: float
    text: str
    position: str = "bottom"


@dataclass(frozen=True)
class HighlightWindow:
    start: float
    end: float
    box: tuple[float, float, float, float]
    label: str = ""


@dataclass
class BurnPlan:
    captions: list[CaptionCue] = field(default_factory=list)
    highlights: list[HighlightWindow] = field(default_factory=list)
    titles: list[dict[str, Any]] = field(default_factory=list)
    freeze_windows: list[tuple[float, float]] = field(default_factory=list)
    slow_windows: list[tuple[float, float]] = field(default_factory=list)
    duration: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "captions": [{"start": c.start, "end": c.end, "text": c.text, "position": c.position}
                         for c in self.captions],
            "highlights": [{"start": h.start, "end": h.end, "box": list(h.box), "label": h.label}
                           for h in self.highlights],
            "titles": list(self.titles),
            "freezeWindows": [list(w) for w in self.freeze_windows],
            "slowWindows": [list(w) for w in self.slow_windows],
            "duration": self.duration,
        }

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> BurnPlan:
        plan = BurnPlan(duration=float(raw.get("duration", 0.0)))
        for cue in raw.get("captions", []):
            plan.captions.append(CaptionCue(start=float(cue["start"]), end=float(cue["end"]),
                                            text=str(cue["text"]), position=str(cue.get("position", "bottom"))))
        for window in raw.get("highlights", []):
            plan.highlights.append(HighlightWindow(start=float(window["start"]), end=float(window["end"]),
                                                   box=tuple(float(v) for v in window["box"]),
                                                   label=str(window.get("label", ""))))
        plan.titles = list(raw.get("titles", []))
        plan.freeze_windows = [tuple(float(v) for v in w) for w in raw.get("freezeWindows", [])]
        plan.slow_windows = [tuple(float(v) for v in w) for w in raw.get("slowWindows", [])]
        return plan


def _clamp_window(start: float, end: float, duration: float) -> tuple[float, float] | None:
    start = max(0.0, start)
    end = min(duration, end)
    if end - start < MIN_SCREEN_SEC:
        end = min(duration, start + MIN_SCREEN_SEC)
    if end <= start:
        return None
    return (start, end)


def plan_burn_in(steps: list[TimelineStep], spec_items: list[dict[str, Any]], *,
                 duration: float,
                 boxes_by_step: dict[int, dict[str, tuple[float, float, float, float]]] | None = None,
                 default_title_duration: float = 3.0) -> BurnPlan:
    plan = BurnPlan(duration=duration)
    boxes_by_step = boxes_by_step or {}
    for item in spec_items:
        item_type = item.get("type")
        if item_type == "caption" and ("start" in item or "end" in item):
            window = _clamp_window(float(item.get("start", 0.0)), float(item.get("end", duration)), duration)
            if window is not None:
                plan.captions.append(CaptionCue(start=window[0], end=window[1],
                                                text=str(item.get("text", "")),
                                                position=str(item.get("position", "bottom"))))
        elif item_type == "title":
            length = float(item.get("duration", default_title_duration))
            window = _clamp_window(0.0, length, duration)
            if window is not None:
                plan.titles.append({"text": str(item.get("text", "")),
                                    "subtitle": str(item.get("subtitle", "")),
                                    "start": window[0], "end": window[1]})
        emphasis = item.get("emphasis", "none")
        if emphasis in ("freeze", "slow") and "step" in item:
            step_index = int(item["step"])
            if 0 <= step_index < len(steps):
                anchor_t = steps[step_index].t
                window = _clamp_window(anchor_t - 1.0, anchor_t + 1.0, duration)
                if window is not None:
                    if emphasis == "freeze":
                        plan.freeze_windows.append(window)
                    else:
                        plan.slow_windows.append(window)
    for index, step in enumerate(steps):
        boxes = (boxes_by_step.get(index) or {})
        next_t = steps[index + 1].t if index + 1 < len(steps) else duration
        start = max(0.0, step.t)
        end = min(duration, next_t, start + HIGHLIGHT_SEC)
        if end <= start:
            continue
        for label, box in boxes.items():
            plan.highlights.append(HighlightWindow(start=start, end=end,
                                                   box=tuple(float(v) for v in box), label=label))
    return plan


def ass_subtitles(plan: BurnPlan, width: int, height: int, font_size: int = 16) -> str:
    def stamp(seconds: float) -> str:
        total = max(0, int(seconds * 100))
        hours, rest = divmod(total, 360000)
        minutes, rest = divmod(rest, 6000)
        secs, centis = divmod(rest, 100)
        return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"

    lines = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {width}", f"PlayResY: {height}",
             "", "[V4+ Styles]",
             "Format: Name, Fontname, Fontsize, PrimaryColour, BackColour, Alignment, MarginV",
             f"Style: CaptionTop,IBM Plex Sans,{font_size},&H00FFFFFF,&HB4000000,8,24",
             f"Style: CaptionBottom,IBM Plex Sans,{font_size},&H00FFFFFF,&HB4000000,2,24",
             "", "[Events]", "Format: Layer, Start, End, Style, Name, Text"]
    for cue in plan.captions:
        style = "CaptionTop" if cue.position == "top" else "CaptionBottom"
        text = cue.text.replace("\n", "\\N")
        lines.append(f"Dialogue: 0,{stamp(cue.start)},{stamp(cue.end)},{style},,{text}")
    return "\n".join(lines) + "\n"


def drawbox_filters(plan: BurnPlan) -> list[str]:
    filters: list[str] = []
    for window in plan.highlights:
        x, y, w, h = (int(v) for v in window.box)
        filters.append(f"drawbox=x={x}:y={y}:w={max(1, w)}:h={max(1, h)}:color=red@0.9:t=3:"
                       f"enable='gte(t,{window.start:.3f})*lt(t,{window.end:.3f})'")
    return filters


def emphasis_filters(plan: BurnPlan) -> list[str]:
    filters: list[str] = []
    for start, end in plan.freeze_windows:
        filters.append(f"tpad=clone:enable='between(t,{start:.2f},{end:.2f})'")
    for start, end in plan.slow_windows:
        filters.append(f"setpts='if(between(T,{start:.2f},{end:.2f}),2*PTS,PTS)'")
    return filters


def ffmpeg_command(plan: BurnPlan, source: str, dest: str, *, ass_path: str | None = None,
                   width: int = 0, height: int = 0) -> list[str]:
    from eks_harness.store.media import ffmpeg_binary

    binary = ffmpeg_binary()
    if binary is None:
        raise RuntimeError("ffmpeg is not on PATH; cannot burn annotations into video.")
    filters = drawbox_filters(plan) + emphasis_filters(plan)
    if ass_path is not None:
        filters.append(f"ass='{ass_path}'")
    command = [binary, "-nostdin", "-y", "-i", source]
    if filters:
        command += ["-vf", ",".join(filters)]
    command.append(dest)
    _ = (width, height)
    return command
