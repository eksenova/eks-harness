from __future__ import annotations

import shutil
import subprocess
import threading
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
from pydantic import Field, JsonValue

from eks_harness.engine.clock import run_time_context
from eks_harness.engine.errors import NodeFailure
from eks_harness.engine.executors import (
    ArtifactSink,
    ExecContext,
    ResolveContext,
    SessionState,
)
from eks_harness.engine.model import RunInfo, RunResources
from eks_harness.engine.values import validate_args
from eks_harness.ir.plan import Clock, Node, Plan
from eks_harness.ir.registry import (
    Args,
    CheckSpec,
    Registry,
    callable_ref,
    default_registry,
)
from eks_harness.ir.time import Span, TempoMap, TimeContext, t
from eks_harness.ir.types import Artifact
from eks_harness.ops.check import (
    MEDIA_CHECK_EXECUTOR,
    MEDIA_CHECK_OP,
    MediaCheckCall,
    MediaCheckExecutor,
    tolerance_frames,
)
from eks_harness.review import ocr

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                                reason="ffmpeg/ffprobe not on PATH")

FPS = Fraction(30)
SIZE = "320x240"
CUT_AT = 2.0
BLACK = (3.0, 3.5)
FROZEN = (3.5, 5.0)
CLICKS = [0.25 + 0.5 * index for index in range(12)]
HASH_NODE = "sha256:" + "a" * 64
HASH_PLAN = "sha256:" + "b" * 64
FONTS = ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Helvetica.ttc",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
HAS_TESSERACT = shutil.which("tesseract") is not None
needs_tesseract = pytest.mark.skipif(not HAS_TESSERACT, reason="tesseract is not installed")


@pytest.fixture(autouse=True)
def registry() -> Iterator[Registry]:
    chosen = default_registry()
    state = chosen.snapshot()
    try:
        yield chosen
    finally:
        chosen.restore(state)


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


@dataclass(frozen=True)
class Fixtures:
    video: Path
    tone: Path
    text: Path
    edge_text: Path
    logo_video: Path
    logo: Path


def _font(size: int) -> Any:
    from PIL import ImageFont

    found = next((path for path in FONTS if Path(path).is_file()), None)
    return ImageFont.truetype(found, size) if found else ImageFont.load_default(size=size)


def _card(path: Path, text: str, xy: tuple[int, int]) -> Path:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (640, 360), (255, 255, 255))
    ImageDraw.Draw(image).text(xy, text, fill=(0, 0, 0), font=_font(48))
    image.save(path)
    return path


def _logo(path: Path) -> Path:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (64, 64), (20, 20, 20))
    draw = ImageDraw.Draw(image)
    for row in range(4):
        for column in range(4):
            if (row + column) % 2 == 0:
                draw.rectangle((column * 16, row * 16, column * 16 + 15, row * 16 + 15), fill=(240, 200, 30))
    draw.ellipse((20, 20, 44, 44), fill=(200, 30, 60))
    image.save(path)
    return path


def _still(image: Path, out: Path, seconds: float) -> Path:
    ffmpeg("-loop", "1", "-framerate", str(FPS), "-t", str(seconds), "-i", str(image), "-vf", "format=yuv420p",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "12", str(out))
    return out


@pytest.fixture(scope="module")
def media(tmp_path_factory: pytest.TempPathFactory) -> Fixtures:
    folder = tmp_path_factory.mktemp("media-checks")
    video = folder / "edit.mp4"
    graph = (
        f"testsrc2=s={SIZE}:r={FPS}:d=2[a];"
        f"color=c=red:s={SIZE}:r={FPS}:d=1,noise=alls=60:allf=t[b];"
        f"color=c=black:s={SIZE}:r={FPS}:d=0.5[c];"
        f"color=c=0x808080:s={SIZE}:r={FPS}:d=1.5[d];"
        f"testsrc2=s={SIZE}:r={FPS}:d=1[e];"
        "[a][b][c][d][e]concat=n=5:v=1:a=0,format=yuv420p[v]"
    )
    clicks = "+".join(f"between(t,{click},{click + 0.012})" for click in CLICKS)
    ffmpeg("-filter_complex", graph, "-f", "lavfi", "-i", f"aevalsrc='({clicks})*0.8*sin(2*PI*1500*t)':s=48000:d=6",
           "-map", "[v]", "-map", "0:a", "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-g", "30",
           "-c:a", "aac", "-b:a", "192k", str(video))
    tone = folder / "tone.wav"
    ffmpeg("-f", "lavfi", "-i", "aevalsrc=sin(2*PI*1000*t):s=48000:d=6", "-af", "volume=-20dB",
           "-c:a", "pcm_s16le", str(tone))
    text = _still(_card(folder / "text.png", "EKSEN 2026", (150, 150)), folder / "text.mp4", 2.0)
    edge_text = _still(_card(folder / "edge.png", "EKSEN 2026", (150, 2)), folder / "edge.mp4", 2.0)
    logo = _logo(folder / "logo.png")
    logo_video = folder / "logo.mp4"
    ffmpeg("-f", "lavfi", "-i", f"color=c=0x2060a0:s=640x360:r={FPS}:d=3", "-loop", "1", "-i", str(logo),
           "-filter_complex", "[0:v][1:v]overlay=x=300:y=120:enable='gte(t,1)':shortest=1,format=yuv420p[v]",
           "-map", "[v]", "-t", "3", "-c:v", "libx264", "-preset", "veryfast", "-crf", "12", str(logo_video))
    return Fixtures(video=video, tone=tone, text=text, edge_text=edge_text, logo_video=logo_video, logo=logo)


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, JsonValue]]] = []

    def event(self, type: str, detail: Mapping[str, JsonValue]) -> None:
        self.events.append((type, dict(detail)))

    def progress(self, fraction: float, message: str) -> None:
        return None

    def artifact(self, artifact: Artifact) -> None:
        return None


class NoStore:
    def ingest(self, *args: Any, **kwargs: Any) -> Artifact:
        raise AssertionError("check.media ingests nothing")

    def get(self, artifact_id: str) -> Artifact | None:
        return None

    def file(self, artifact_id: str) -> Path | None:
        return None

    def scratch(self) -> Path:
        raise AssertionError("check.media needs no scratch directory")


@dataclass(frozen=True)
class Content:
    path: Path | None
    sha256: str


class Files:
    def media(self, uri: str) -> Content:
        return Content(path=Path(uri.removeprefix("file:")), sha256="sha256:" + "0" * 64)


def media_value(path: Path, kind: str = "video") -> dict[str, JsonValue]:
    return {"uri": f"file:{path}", "kind": kind}


def make_call(path: Path, kind: str, *, params: Mapping[str, Any] | None = None, at: Any = None,
              tolerance: str | None = None, soft: bool = False, label: str | None = None,
              media_kind: str = "video", clock: Clock | None = None) -> ExecContext:
    registry = default_registry()
    spec = registry.op(MEDIA_CHECK_OP)
    data: dict[str, JsonValue] = {"media": media_value(path, media_kind), "kind": kind,
                                  "params": dict(params or {}), "soft": soft}
    if at is not None:
        data["at"] = at
    if tolerance is not None:
        data["tolerance"] = tolerance
    if label is not None:
        data["label"] = label
    raw: dict[str, JsonValue] = {**data, "media": {"$ref": "n0000000000", "path": []}}
    node = Node(id="n0000000001", op=MEDIA_CHECK_OP, args=raw, out=spec.out, hash=HASH_NODE)
    plan = Plan(kind="edit", name="checks", nodes=[node], hash=HASH_PLAN,
                clock=clock or Clock(fps=FPS, tempo=TempoMap(bpm=120.0, offset=t("0.25s"))))
    resolved = validate_args(spec, data)
    info = RunInfo(id="01JMEDIACHECKRUN0000000000", project="acme/media", session="media", plan_hash=plan.hash,
                   plan_kind=plan.kind, plan_name=plan.name, created_at=time.time())
    sink = RecordingSink()
    artifacts = ArtifactSink(NoStore(), run=info, node_id=node.id, instance="", op=MEDIA_CHECK_OP, sink=sink)

    def context() -> TimeContext:
        return run_time_context(plan, plan.clock, (), lambda _node: None)

    return ExecContext(run=info, plan=plan, registry=registry, node=node, spec=spec, instance="", attempt=1,
                       args=resolved.model, data=resolved.data, session=None, state=SessionState("main"),
                       resources=RunResources(), driver=None, sink=sink, artifacts=artifacts, seed=0,
                       clock=plan.clock, deadline=None, cancel=threading.Event(), time_context=context,
                       content=Files())


def run(call: ExecContext) -> dict[str, JsonValue]:
    value = MediaCheckExecutor().run(call).value
    assert isinstance(value, dict)
    return value


def failure(call: ExecContext) -> NodeFailure:
    with pytest.raises(NodeFailure) as caught:
        MediaCheckExecutor().run(call)
    return caught.value


def test_spec_registers_check_media_with_a_resolver() -> None:
    spec = MediaCheckExecutor().spec()
    assert spec.name == MEDIA_CHECK_EXECUTOR
    assert spec.ops == (MEDIA_CHECK_OP,)
    assert spec.resolver is not None
    assert spec.match(default_registry().op(MEDIA_CHECK_OP)) == 3


def test_cut_lands_on_the_hard_cut(media: Fixtures) -> None:
    value = run(make_call(media.video, "cut", params={"threshold": 0.1}, at="2s", tolerance="2f", label="main cut"))
    assert value["ok"] is True
    assert value["kind"] == "cut"
    assert value["at"] == CUT_AT
    assert value["label"] == "main cut"
    detail = value["detail"]
    assert isinstance(detail, dict)
    assert detail["status"] == "pass"
    assert detail["measured"] == CUT_AT
    assert detail["toleranceFrames"] == 2


def test_cut_at_a_beat_resolves_through_the_clock(media: Fixtures) -> None:
    value = run(make_call(media.video, "cut", at="beat:3.5", tolerance="2f"))
    assert value["at"] == CUT_AT


def test_cut_follows_the_clock_window_start(media: Fixtures) -> None:
    clock = Clock(fps=FPS, window=Span(start=t("1s"), end=t("7s")))
    value = run(make_call(media.video, "cut", at="3s", clock=clock))
    assert value["at"] == CUT_AT


def test_missing_cut_fails_with_an_assertion(media: Fixtures) -> None:
    problem = failure(make_call(media.video, "cut", at="1s", label="early cut"))
    assert problem.code == "assertion"
    assert problem.soft is False
    assert "early cut" in problem.message
    assert problem.detail["kind"] == "cut"
    assert problem.detail["at"] == 1.0


def test_soft_failure_is_marked_soft(media: Fixtures) -> None:
    problem = failure(make_call(media.video, "cut", at="1s", soft=True))
    assert problem.code == "assertion"
    assert problem.soft is True
    assert problem.error().soft is True


def test_cut_needs_one_time(media: Fixtures) -> None:
    problem = failure(make_call(media.video, "cut", at={"start": "1s", "end": "3s"}))
    assert problem.code == "args_invalid"


def test_tolerance_becomes_media_frames(media: Fixtures) -> None:
    call = make_call(media.video, "cut", at="2s")
    assert tolerance_frames(call, None, 30.0) == 2
    assert tolerance_frames(call, t("100ms"), 30.0) == 3
    assert tolerance_frames(call, t("4f"), 25.0) == 4
    assert tolerance_frames(call, t("1b"), 30.0) == 15
    assert tolerance_frames(call, t("0s"), 30.0) == 0


def test_black_stretches(media: Fixtures) -> None:
    problem = failure(make_call(media.video, "black", params={"longest": "0.2s"}))
    assert problem.code == "assertion"
    result = problem.detail["result"]
    assert isinstance(result, dict)
    data = result["data"]
    assert isinstance(data, dict)
    spans = data["spans"]
    assert isinstance(spans, list) and len(spans) == 1
    assert run(make_call(media.video, "black", params={"longest": "1s"}))["ok"] is True
    assert run(make_call(media.video, "black", params={"longest": "0s"},
                         at={"start": "0s", "end": "2.9s"}))["ok"] is True


def test_frozen_stretches(media: Fixtures) -> None:
    problem = failure(make_call(media.video, "frozen"))
    assert problem.code == "assertion"
    assert run(make_call(media.video, "frozen", params={"longest": "2.5s"}))["ok"] is True
    assert run(make_call(media.video, "frozen", at={"start": "0s", "end": "3s"}))["ok"] is True


def test_motion_level(media: Fixtures) -> None:
    moving = {"start": "0s", "end": "1.9s"}
    assert run(make_call(media.video, "motion", params={"min": 0.001}, at=moving))["ok"] is True
    still = {"start": "3.6s", "end": "4.9s"}
    problem = failure(make_call(media.video, "motion", params={"min": 0.001}, at=still))
    assert problem.code == "assertion"
    assert "below" in problem.message


def test_loudness_of_an_audio_file(media: Fixtures) -> None:
    value = run(make_call(media.tone, "loudness", params={"lufs": -23.0, "tolerance": 1.0, "true_peak": -1.0},
                          media_kind="audio"))
    detail = value["detail"]
    assert isinstance(detail, dict)
    data = detail["data"]
    assert isinstance(data, dict)
    integrated = data["integrated"]
    assert isinstance(integrated, float) and abs(integrated + 23.0) < 1.0
    loud = failure(make_call(media.tone, "loudness", params={"lufs": -14.0}, media_kind="audio"))
    assert loud.code == "assertion" and "LUFS" in loud.message
    peak = failure(make_call(media.tone, "loudness", params={"lufs": -23.0, "true_peak": -30.0},
                             media_kind="audio"))
    assert "true peak" in peak.message


def test_loudness_of_silent_video_fails(media: Fixtures) -> None:
    problem = failure(make_call(media.text, "loudness", params={"lufs": -14.0}))
    assert problem.code == "assertion"
    assert "no audio" in problem.message


def test_video_checks_need_a_video_stream(media: Fixtures) -> None:
    problem = failure(make_call(media.tone, "cut", at="1s", media_kind="audio"))
    assert problem.code == "args_invalid"


def test_onset(media: Fixtures) -> None:
    assert run(make_call(media.video, "onset", params={"sensitivity": 1.0}, at="0.75s"))["ok"] is True
    problem = failure(make_call(media.video, "onset", params={"sensitivity": 1.0}, at="0.6s"))
    assert problem.code == "assertion"


def test_beats_follow_the_clock_stream(media: Fixtures) -> None:
    value = run(make_call(media.video, "beats", params={"stream": "beat", "min_ratio": 0.8},
                          at={"start": "0s", "end": "5.5s"}))
    detail = value["detail"]
    assert isinstance(detail, dict)
    data = detail["data"]
    assert isinstance(data, dict)
    assert data["total"] == len(CLICKS) - 1
    shifted = Clock(fps=FPS, tempo=TempoMap(bpm=120.0, offset=t("0.45s")))
    problem = failure(make_call(media.video, "beats", params={"stream": "beat"}, at={"start": "0s", "end": "5.5s"},
                                clock=shifted))
    assert problem.code == "assertion"


def test_beats_without_a_tempo_cannot_resolve(media: Fixtures) -> None:
    problem = failure(make_call(media.video, "beats", params={"stream": "beat"}, clock=Clock(fps=FPS)))
    assert problem.code == "time_unresolved"


def test_beats_need_stream_times_in_the_span(media: Fixtures) -> None:
    problem = failure(make_call(media.video, "beats", at={"start": "5.8s", "end": "5.9s"}))
    assert problem.code == "assertion"
    assert "no times" in problem.message


@needs_tesseract
def test_text_is_read_with_a_pinned_engine(media: Fixtures) -> None:
    result = MediaCheckExecutor().run(make_call(media.text, "text", params={"text": "EKSEN 2026",
                                                                         "engine": "tesseract"}, at="1s"))
    assert isinstance(result.value, dict) and result.value["ok"] is True
    assert result.resolution is not None
    assert result.resolution["engine"] == "tesseract"
    assert result.resolution["version"]
    assert result.resolution["ocr"] == {"engine": "tesseract", "version": result.resolution["version"]}
    span = {"start": "0s", "end": "1.5s"}
    assert run(make_call(media.text, "text", params={"text": "EKSEN", "engine": "tesseract"}, at=span))["ok"]
    problem = failure(make_call(media.text, "text", params={"text": "Kontor", "engine": "tesseract"}, at="1s",
                                soft=True))
    assert problem.code == "assertion" and problem.soft is True


@needs_tesseract
def test_text_region_is_a_frame_rect(media: Fixtures) -> None:
    inside = {"by": "rect", "args": {"x": 100, "y": 120, "w": 440, "h": 120}}
    assert run(make_call(media.text, "text", params={"text": "EKSEN", "engine": "tesseract", "region": inside},
                         at="1s"))["ok"] is True
    elsewhere = {"by": "rect", "args": {"x": 0, "y": 260, "w": 640, "h": 100}}
    problem = failure(make_call(media.text, "text", params={"text": "EKSEN", "engine": "tesseract",
                                                            "region": elsewhere}, at="1s"))
    assert problem.code == "assertion"


def test_element_regions_need_a_box_map(media: Fixtures) -> None:
    region = {"by": "test_id", "args": {"id": "cta"}}
    problem = failure(make_call(media.text, "safe_area", params={"region": region, "engine": "tesseract"})
                      if HAS_TESSERACT else make_call(media.text, "template", params={
                          "template": media_value(media.logo, "image"), "region": region}, at="1s"))
    assert problem.code == "args_invalid"
    assert "box map" in problem.message


@needs_tesseract
def test_safe_area_reads_the_text_boxes(media: Fixtures) -> None:
    value = run(make_call(media.text, "safe_area", params={"engine": "tesseract"}))
    detail = value["detail"]
    assert isinstance(detail, dict)
    data = detail["data"]
    assert isinstance(data, dict)
    lines = data["lines"]
    assert isinstance(lines, int) and lines > 0
    problem = failure(make_call(media.edge_text, "safe_area", params={"engine": "tesseract"}))
    assert problem.code == "assertion"
    assert "outside the safe area" in problem.message


@needs_tesseract
def test_safe_area_region_rect(media: Fixtures) -> None:
    inside = {"by": "rect", "args": {"x": 100, "y": 100, "w": 200, "h": 100}}
    assert run(make_call(media.text, "safe_area", params={"engine": "tesseract", "region": inside}))["ok"] is True
    top = {"by": "rect", "args": {"x": 100, "y": 0, "w": 200, "h": 40}}
    problem = failure(make_call(media.text, "safe_area", params={"engine": "tesseract", "region": top}))
    assert "unsafe margins" in problem.message


def test_pinned_engine_that_is_missing_is_unavailable(media: Fixtures, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ocr, "_ENGINES", {})

    def nothing(name: str) -> None:
        return None

    monkeypatch.setattr(ocr.shutil, "which", nothing)
    problem = failure(make_call(media.text, "text", params={"text": "EKSEN", "engine": "tesseract"}, at="1s"))
    assert problem.code == "resolution_unavailable"
    assert "tesseract" in problem.message


def test_resolver_reports_the_ocr_engine(media: Fixtures) -> None:
    call = make_call(media.text, "text", params={"text": "EKSEN"}, at="1s")
    context = ResolveContext(run=call.run, plan=call.plan, registry=call.registry, node=call.node, spec=call.spec,
                             args=call.args, data=call.data, resources=call.resources)
    try:
        found = ocr.engine("auto")
    except ocr.OcrError:
        with pytest.raises(NodeFailure) as caught:
            MediaCheckExecutor().resolve(context)
        assert caught.value.code == "resolution_unavailable"
        return
    record = MediaCheckExecutor().resolve(context)
    assert record["engine"] == found.name
    assert record["version"] == found.version()
    cut = make_call(media.text, "cut", at="1s")
    assert MediaCheckExecutor().resolve(ResolveContext(
        run=cut.run, plan=cut.plan, registry=cut.registry, node=cut.node, spec=cut.spec, args=cut.args,
        data=cut.data, resources=cut.resources)) == {}


def test_template_appears(media: Fixtures) -> None:
    template = media_value(media.logo, "image")
    assert run(make_call(media.logo_video, "template", params={"template": template, "threshold": 0.8},
                         at="2s"))["ok"] is True
    problem = failure(make_call(media.logo_video, "template", params={"template": template}, at="0.5s"))
    assert problem.code == "assertion"
    window = {"start": "0s", "end": "2.5s"}
    assert run(make_call(media.logo_video, "template", params={"template": template}, at=window))["ok"] is True


class Brightness(Args):
    above: float = Field(default=0.5, description="Smallest mean brightness")


seen: list[MediaCheckCall] = []


def brightness(check: MediaCheckCall) -> Mapping[str, JsonValue]:
    seen.append(check)
    above = check.params.get("above", 0.5)
    assert isinstance(above, float)
    return {"ok": above < 0.9, "message": f"brightness over {above}", "detail": {"above": above}}


def test_plugin_media_checks_run_their_implementation(media: Fixtures, registry: Registry) -> None:
    registry.register_check(CheckSpec(kind="brightness", family="media", args=Brightness,
                                      impl=callable_ref(brightness)))
    seen.clear()
    value = run(make_call(media.text, "brightness", params={"above": 0.2}, at="1s", tolerance="3f"))
    assert value["ok"] is True
    assert value["detail"] == {"status": "pass", "data": {"above": 0.2}}
    (check,) = seen
    assert check.path == media.text
    assert check.at == 1.0
    assert check.tolerance_frames == 3
    assert check.kind == "brightness"
    problem = failure(make_call(media.text, "brightness", params={"above": 0.95}))
    assert problem.code == "assertion"
    assert "brightness over 0.95" in problem.message


def test_worker_script_media_checks_are_refused(media: Fixtures, registry: Registry) -> None:
    registry.register_check(CheckSpec(kind="glow", family="media", args=Brightness, impl="checks/glow.mjs"))
    problem = failure(make_call(media.text, "glow"))
    assert problem.code == "executor_missing"
