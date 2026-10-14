from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest
from numpy.typing import NDArray
from PIL import Image as PILImage
from pydantic import JsonValue

from eks_harness.engine.errors import NodeFailure
from eks_harness.engine.executors import ArtifactSink, ExecContext, SessionState
from eks_harness.engine.model import RunInfo, RunResources
from eks_harness.engine.values import validate_args
from eks_harness.ir.ops.overlay import (
    Blur,
    Callout,
    Caption,
    Highlight,
    Icon,
    Redact,
    Spotlight,
    Step,
    Title,
)
from eks_harness.ir.plan import Node, Plan, SessionSpec
from eks_harness.ir.registry import default_registry
from eks_harness.ir.target import Target
from eks_harness.ir.types import Artifact
from eks_harness.media.overlays.boxmap import (
    BOXES_SCHEMA,
    BoxMap,
    BoxMapError,
    RecordingBoxes,
    from_bridge,
    from_native,
    normalize_recording,
    trim_mapper,
)
from eks_harness.media.overlays.targets import resolve_on_boxmap
from eks_harness.media.overlays.video import (
    VideoOverlay,
    blur_filter,
    render_overlay_video,
)
from eks_harness.ops.overlay import ANNOTATE_OP, AnnotateExecutor

HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not installed")


def node(box: list[float], parent: int | None = None, **fields: str) -> dict[str, JsonValue]:
    found: dict[str, JsonValue] = {key: value for key, value in fields.items()}
    found["box"] = list[JsonValue](box)
    found["parent"] = parent
    return found


BOXES: dict[str, JsonValue] = {
    "schema": BOXES_SCHEMA,
    "size": [400, 300],
    "scale": 2,
    "source": "web",
    "nodes": [
        node([0, 0, 400, 40], kind="header", testId="header", role="banner"),
        node([10, 10, 80, 20], 0, kind="h1", role="heading", name="Faturalar", text="Faturalar"),
        node([0, 50, 400, 200], kind="ul", testId="list", role="list"),
        node([10, 60, 60, 20], 2, kind="button", role="button", name="Sil", text="Sil"),
        node([10, 200, 60, 20], 2, kind="button", role="button", name="Sil", text="Sil"),
        node([300, 260, 80, 30], kind="button", testId="save", role="button", name="Kaydet", text="Kaydet"),
        node([300, 262, 40, 20], 5, kind="span", text="Kaydet"),
        node([100, 260, 150, 30], kind="input", role="textbox", name="Adı", label="Adı", placeholder="Ad Soyad"),
        node([100, 100, 120, 20], 2, kind="p", text="Şirket ünvanı"),
    ],
}


def target(by: str, **args: JsonValue) -> dict[str, JsonValue]:
    return {"by": by, "args": dict(args)}


def boxes() -> BoxMap:
    return BoxMap.from_json(BOXES)


def test_box_maps_round_trip_and_reject_bad_input() -> None:
    parsed = boxes()
    assert parsed.size == (400, 300) and parsed.scale == 2 and len(parsed.nodes) == 9
    assert BoxMap.from_json(parsed.to_json()) == parsed
    assert parsed.ancestors(6) == [5]
    with pytest.raises(BoxMapError, match="schema"):
        BoxMap.from_json({**BOXES, "schema": "eks/boxes@2"})
    with pytest.raises(BoxMapError, match="parent"):
        BoxMap.from_json({**BOXES, "nodes": [node([0, 0, 1, 1], 0)]})


def test_targets_resolve_strictly_on_a_box_map() -> None:
    found = boxes()
    assert resolve_on_boxmap(target("test_id", id="save"), found) == (300, 260, 80, 30)
    assert resolve_on_boxmap(target("text", text="Kaydet"), found) == (300, 260, 80, 30)
    assert resolve_on_boxmap(target("text", text="sirket UNVANI", exact=False), found) == (100, 100, 120, 20)
    assert resolve_on_boxmap(target("role", role="heading", name="faturalar"), found) == (10, 10, 80, 20)
    assert resolve_on_boxmap(target("label", text="adı"), found) == (100, 260, 150, 30)
    assert resolve_on_boxmap(target("placeholder", text="Soyad"), found) == (100, 260, 150, 30)
    assert resolve_on_boxmap(target("kind", kind="input"), found) == (100, 260, 150, 30)
    assert resolve_on_boxmap(target("point", x=5, y=6), found) == (5, 6, 0, 0)
    assert resolve_on_boxmap(target("rect", x=1, y=2, w=3, h=4), found) == (1, 2, 3, 4)
    assert resolve_on_boxmap({**target("text", text="Sil"), "nth": -1}, found) == (10, 200, 60, 20)
    near = {**target("text", text="Sil"), "near": target("text", text="Şirket ünvanı")}
    assert resolve_on_boxmap(near, found) == (10, 60, 60, 20)
    within = {**target("role", role="button"), "within": target("test_id", id="list"), "nth": 1}
    assert resolve_on_boxmap(within, found) == (10, 200, 60, 20)
    model = Target.model_validate(target("test_id", id="header"))
    assert resolve_on_boxmap(model, found) == (0, 0, 400, 40)


def test_target_failures_name_candidates_and_near_misses() -> None:
    found = boxes()
    with pytest.raises(NodeFailure) as ambiguous:
        resolve_on_boxmap(target("text", text="Sil"), found)
    assert ambiguous.value.code == "target_ambiguous"
    candidates = ambiguous.value.detail["candidates"]
    assert isinstance(candidates, list) and len(candidates) == 2 and "Sil" in str(candidates[0])
    with pytest.raises(NodeFailure) as missing:
        resolve_on_boxmap(target("text", text="Kaydet ve gönder"), found)
    assert missing.value.code == "target_not_found"
    misses = missing.value.detail["nearMisses"]
    assert isinstance(misses, list) and any("Kaydet" in str(item) for item in misses)
    with pytest.raises(NodeFailure) as exact:
        resolve_on_boxmap(target("text", text="kaydet"), found)
    assert exact.value.code == "target_not_found"
    with pytest.raises(NodeFailure) as css:
        resolve_on_boxmap(target("css", selector="#save"), found)
    assert css.value.code == "target_unsupported"
    with pytest.raises(NodeFailure) as beyond:
        resolve_on_boxmap({**target("text", text="Sil"), "nth": 5}, found)
    assert beyond.value.code == "target_not_found"


def test_bridge_and_native_box_maps_scale_to_image_pixels() -> None:
    raw = {"window": {"width": 390, "height": 844}, "pixelRatio": 3,
           "nodes": [{"testId": "save", "kind": "button", "role": "button", "text": "Kaydet", "box": [10, 20, 100, 40],
                      "parent": None}]}
    mapped = from_bridge(raw, (1170, 2532))
    assert mapped.source == "app" and mapped.scale == 3 and mapped.nodes[0].box == (30, 60, 300, 120)
    assert resolve_on_boxmap(target("role", role="link", name="kaydet"), mapped) == (30, 60, 300, 120)

    class Native:
        def __init__(self, kind: str, text: str, x: float, y: float, w: float, h: float) -> None:
            self.kind, self.text, self.label, self.ident, self.value = kind, text, "", "", text
            self.x, self.y, self.width, self.height, self.visible = x, y, w, h, True

    native = from_native([Native("Application", "", 0, 0, 390, 844), Native("Button", "İzin ver", 20, 700, 150, 44)],
                         (1170, 2532))
    assert native.source == "native" and native.nodes[1].parent == 0 and native.nodes[1].box == (60, 2100, 450, 132)
    assert resolve_on_boxmap(target("role", role="button"), native) == (60, 2100, 450, 132)


def test_recording_box_maps_scale_css_steps_and_pick_the_last_step() -> None:
    step: dict[str, JsonValue] = {**BOXES, "size": [200, 150], "scale": 1}
    raw: dict[str, JsonValue] = {"schema": "eks/recording-boxes@1", "units": "css", "events": [1.0],
                                 "steps": [{"t": 0, "action": None, "boxes": step},
                                           {"t": 1.5, "action": "ui.tap", "boxes": step}]}
    recording = normalize_recording(raw, video_size=(400, 300), video_duration=None, trimmed=False, video="tour.mp4")
    assert [item.t for item in recording.steps] == [0, 1.5]
    assert recording.steps[0].boxes.nodes[5].box == (600, 520, 160, 60)
    assert recording.at(1.4) is recording.steps[0].boxes and recording.at(2.0) is recording.steps[1].boxes
    assert RecordingBoxes.from_json(recording.to_json()) == recording


class MemoryStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.files: dict[str, Path] = {}
        self.metas: dict[str, dict[str, JsonValue]] = {}
        self.count = 0

    def add(self, path: Path, meta: Mapping[str, JsonValue] | None = None) -> str:
        self.count += 1
        artifact_id = f"A{self.count:03d}"
        self.files[artifact_id] = path
        self.metas[artifact_id] = dict(meta or {})
        return artifact_id

    def ingest(self, path: Path, *, project: str, session: str, kind: str | None, name: str | None, caption: str,
               tags: Sequence[str], meta: Mapping[str, JsonValue], actor: str | None, move: bool) -> Artifact:
        kept = self.root / f"stored-{self.count}-{name or path.name}"
        shutil.copyfile(path, kept)
        artifact_id = self.add(kept, meta)
        return Artifact(id=artifact_id, kind=kind or "file", name=name)

    def get(self, artifact_id: str) -> Artifact | None:
        return Artifact(id=artifact_id, kind="screenshot") if artifact_id in self.files else None

    def file(self, artifact_id: str) -> Path | None:
        return self.files.get(artifact_id)

    def meta(self, artifact_id: str) -> dict[str, JsonValue] | None:
        return self.metas.get(artifact_id)

    def scratch(self) -> Path:
        return Path(tempfile.mkdtemp(dir=self.root))


class Sink:
    def event(self, type: str, detail: Mapping[str, JsonValue]) -> None:
        return None

    def progress(self, fraction: float, message: str) -> None:
        return None

    def artifact(self, artifact: Artifact) -> None:
        return None


def annotate_call(store: MemoryStore, args: dict[str, JsonValue]) -> ExecContext:
    registry = default_registry()
    spec = registry.op(ANNOTATE_OP)
    plan_node = Node(id="n0000000002", op=ANNOTATE_OP, args=args, out=spec.out, hash=HASH_A)
    plan = Plan(kind="flow", name="annotate", session=SessionSpec(app="web-app", platform="web"), nodes=[plan_node],
                hash=HASH_B)
    resolved = validate_args(spec, args)
    info = RunInfo(id="01JOVERLAYRUN0000000000000", project="acme/overlay", session="overlay", plan_hash=plan.hash,
                   plan_kind=plan.kind, plan_name=plan.name, created_at=time.time())
    artifacts = ArtifactSink(store, run=info, node_id=plan_node.id, instance="", op=ANNOTATE_OP, sink=Sink())
    return ExecContext(run=info, plan=plan, registry=registry, node=plan_node, spec=spec, instance="", attempt=1,
                       args=resolved.model, data=resolved.data, session=plan.session, state=SessionState("main"),
                       resources=RunResources(), driver=None, sink=Sink(), artifacts=artifacts, seed=0,
                       clock=plan.clock, deadline=None, cancel=threading.Event())


def screenshot(tmp_path: Path) -> MemoryStore:
    store = MemoryStore(tmp_path)
    image = PILImage.new("RGB", (400, 300), (240, 240, 240))
    image.paste((30, 120, 200), (300, 260, 380, 290))
    clean = tmp_path / "shot.png"
    image.save(clean)
    sidecar = tmp_path / "shot.boxes.json"
    sidecar.write_text(json.dumps(BOXES), encoding="utf-8")
    boxes_id = store.add(sidecar)
    store.add(clean, {"boxes": boxes_id})
    return store


def overlay_args(*overlays: dict[str, JsonValue], style: str | None = "review") -> dict[str, JsonValue]:
    args: dict[str, JsonValue] = {"artifact": {"id": "A002", "kind": "screenshot"}, "overlays": list(overlays)}
    if style is not None:
        args["style"] = style
    return args


def pixels(path: Path) -> NDArray[np.uint8]:
    with PILImage.open(path) as opened:
        return np.asarray(opened.convert("RGBA"))


def test_the_screenshot_renderer_draws_on_the_box_map(tmp_path: Path) -> None:
    store = screenshot(tmp_path)
    args = overlay_args(
        {"kind": "highlight", "anchor": target("test_id", id="save")},
        {"kind": "redact", "anchor": target("placeholder", text="Ad Soyad")},
        {"kind": "callout", "anchor": target("text", text="Faturalar"), "text": "Fatura listesi"},
        {"kind": "step", "anchor": {**target("text", text="Sil"), "nth": 0}},
        {"kind": "icon", "anchor": target("test_id", id="header"), "name": "check"},
        {"kind": "blur", "anchor": target("rect", x=0, y=0, w=50, h=50)},
    )
    result = AnnotateExecutor().run(annotate_call(store, args))
    assert isinstance(result.value, dict)
    output_id = str(result.value["id"])
    meta = store.metas[output_id]
    assert meta["boxes"] == "A001"
    annotation = meta["annotation"]
    assert isinstance(annotation, dict) and annotation["style"] == "review" and annotation["source"] == "A002"
    assert isinstance(annotation["versions"], dict) and "resvg-py" in annotation["versions"]
    drawn = pixels(store.files[output_id])
    clean = pixels(store.files["A002"])
    assert drawn.shape == clean.shape
    assert tuple(drawn[275, 150][:3]) == (0, 0, 0)
    assert tuple(drawn[150, 390][:3]) == tuple(clean[150, 390][:3])
    assert not np.array_equal(drawn[256:258, 296:384], clean[256:258, 296:384])
    again = AnnotateExecutor().run(annotate_call(store, args))
    assert isinstance(again.value, dict)
    assert store.files[str(again.value["id"])].read_bytes() == store.files[output_id].read_bytes()


def test_the_screenshot_renderer_refuses_titles_and_unanchored_targets(tmp_path: Path) -> None:
    store = screenshot(tmp_path)
    with pytest.raises(NodeFailure) as titled:
        AnnotateExecutor().run(annotate_call(store, overlay_args({"kind": "title", "text": "Faturalar"})))
    assert titled.value.code == "args_invalid"
    with pytest.raises(NodeFailure) as ambiguous:
        AnnotateExecutor().run(annotate_call(store, overlay_args({"kind": "highlight",
                                                                  "anchor": target("text", text="Sil")})))
    assert ambiguous.value.code == "target_ambiguous"
    store.metas["A002"] = {}
    with pytest.raises(NodeFailure) as no_map:
        AnnotateExecutor().run(annotate_call(store, overlay_args({"kind": "highlight",
                                                                  "anchor": target("test_id", id="save")})))
    assert no_map.value.code == "content_unavailable"
    point = AnnotateExecutor().run(annotate_call(store, overlay_args(
        {"kind": "highlight", "anchor": target("rect", x=10, y=10, w=20, h=20)}, style=None)))
    assert isinstance(point.value, dict)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def decode(path: Path, size: tuple[int, int]) -> NDArray[np.uint8]:
    assert FFMPEG is not None
    done = subprocess.run([FFMPEG, "-v", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "rgba", "-"],
                          capture_output=True, check=True)
    frames = np.frombuffer(done.stdout, dtype=np.uint8)
    return frames.reshape(-1, size[1], size[0], 4)


def video_overlays() -> list[VideoOverlay]:
    return [
        VideoOverlay(Caption(text="Kontör işlemleri", step=1), start=0.2, end=0.8),
        VideoOverlay(Highlight(anchor=Target.model_validate(target("test_id", id="save"))), start=0.4, end=1.0,
                     anchor=(100, 60, 60, 30)),
        VideoOverlay(Callout(anchor=Target.model_validate(target("test_id", id="save")), text="Kaydet"), start=0.4,
                     end=1.0, anchor=(100, 60, 60, 30)),
        VideoOverlay(Spotlight(anchor=Target.model_validate(target("test_id", id="save"))), start=1.2, end=1.6,
                     anchor=(100, 60, 60, 30)),
        VideoOverlay(Title(text="Faturalar", kicker="EksenGOLD"), start=1.5, end=2.0),
        VideoOverlay(Redact(anchor=Target.model_validate(target("rect", x=0, y=0, w=20, h=20))), start=0.0, end=0.5,
                     anchor=(0, 0, 20, 20)),
        VideoOverlay(Icon(anchor=Target.model_validate(target("rect", x=0, y=0, w=20, h=20)), name="star"),
                     start=0.0, end=0.5, anchor=(150, 10, 20, 20)),
        VideoOverlay(Step(anchor=Target.model_validate(target("rect", x=0, y=0, w=20, h=20))), start=0.0, end=0.5,
                     anchor=(200, 100, 20, 20)),
        VideoOverlay(Blur(anchor=Target.model_validate(target("rect", x=0, y=0, w=20, h=20))), start=0.0, end=0.5,
                     anchor=(0, 0, 20, 20)),
    ]


@needs_ffmpeg
def test_the_video_overlay_renderer_is_deterministic_and_fades_in_its_window(tmp_path: Path) -> None:
    size, fps = (320, 180), Fraction(10)
    first = render_overlay_video(video_overlays(), size=size, fps=fps, frames=20, style=None, out=tmp_path / "a.mov")
    second = render_overlay_video(video_overlays(), size=size, fps=fps, frames=20, style=None,
                                  out=tmp_path / "b.mov", threads=2)
    assert digest(first) == digest(second)
    frames = decode(first, size)
    assert frames.shape[0] == 20
    assert frames[0, 5, 5, 3] == 0
    assert frames[1, 5, 5, 3] == 170 and tuple(frames[1, 5, 5, :3]) == (0, 0, 0)
    assert frames[2, 5, 5, 3] == 255
    assert frames[4, 5, 5, 3] == 170
    assert frames[6, 5, 5, 3] == 0
    bar_row = size[1] - 5
    assert frames[2, bar_row, 160, 3] == 0
    assert frames[3, bar_row, 160, 3] == 170
    assert frames[5, bar_row, 160, 3] == 255
    assert frames[8, bar_row, 160, 3] == 0
    assert frames[11, 150, 10, 3] == 0
    assert frames[14, 150, 10, 3] == 153 and frames[14, 75, 130, 3] == 0
    assert frames[17, 90, 160, 3] == 255
    assert frames[10, 90, 300, 3] == 0


@needs_ffmpeg
def test_blur_overlays_become_a_filter_over_the_composite(tmp_path: Path) -> None:
    assert FFMPEG is not None
    graph = blur_filter(video_overlays(), size=(320, 180), output="out")
    assert graph is not None and graph.endswith("[out]")
    source = tmp_path / "base.mp4"
    subprocess.run([FFMPEG, "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=10:duration=1",
                    "-pix_fmt", "yuv420p", "-c:v", "libx264", "-qp", "0", str(source)], check=True)
    blurred = tmp_path / "blurred.mp4"
    subprocess.run([FFMPEG, "-v", "error", "-i", str(source), "-filter_complex", graph, "-map", "[out]",
                    "-c:v", "libx264", "-qp", "0", "-pix_fmt", "yuv444p", str(blurred)], check=True)
    plain = decode(source, (320, 180)).astype(np.int32)
    out = decode(blurred, (320, 180)).astype(np.int32)
    corner = [float(np.abs(out[i, :20, :20, :3] - plain[i, :20, :20, :3]).mean()) for i in range(10)]
    assert corner[2] > 20 and corner[3] > 20
    assert corner[6] < 10 and corner[9] < 10 and corner[2] > 3 * corner[9]
    assert np.abs(out[3, 60:, 60:, :3] - plain[3, 60:, 60:, :3]).mean() < 4


def test_titles_without_a_frame_and_bad_windows_fail(tmp_path: Path) -> None:
    bad = [VideoOverlay(Caption(text="x"), start=1.0, end=1.0)]
    with pytest.raises(NodeFailure) as window:
        render_overlay_video(bad, size=(64, 64), fps=Fraction(10), frames=2, style=None, out=tmp_path / "x.mov")
    assert window.value.code == "args_invalid"


def test_worker_replies_link_box_map_sidecars_from_the_image_meta(tmp_path: Path) -> None:
    from eks_harness.engine.drivers import DriverFile, DriverReply
    from eks_harness.ops.worker import (
        _ingest_reply,  # pyright: ignore[reportPrivateUsage]
    )

    store = MemoryStore(tmp_path)
    image, sidecar = tmp_path / "home.png", tmp_path / "home.boxes.json"
    PILImage.new("RGB", (400, 300)).save(image)
    sidecar.write_text(json.dumps(BOXES), encoding="utf-8")
    reply = DriverReply(files=(DriverFile(path=image, role="value", kind="screenshot", name="home"),
                               DriverFile(path=sidecar, role="evidence", kind="file", name=sidecar.name)))
    value = _ingest_reply(annotate_call(store, overlay_args({"kind": "redact", "anchor": target("point", x=1, y=1)})),
                          reply)
    assert isinstance(value, dict)
    image_id = str(value["id"])
    boxes_id = store.metas[image_id]["boxes"]
    assert isinstance(boxes_id, str) and store.files[boxes_id].name.endswith("home.boxes.json")


def test_trimmed_recordings_map_step_times_onto_the_trimmed_video(monkeypatch: pytest.MonkeyPatch) -> None:
    from eks_harness.capture.trim import expected_duration, output_offset, plan

    for name in ("EKS_TRIM_KEEP_BEFORE", "EKS_TRIM_KEEP_AFTER", "EKS_TRIM_MAX_SPEEDUP", "EKS_TRIM_MIN_SCREEN"):
        monkeypatch.delenv(name, raising=False)
    events = [2.0, 20.0, 31.5]
    segments = plan(40.0, events)
    known = trim_mapper(events, expected_duration(segments), 40.0)
    solved = trim_mapper(events, expected_duration(segments))
    for seconds in (0.0, 2.0, 10.0, 20.0, 31.5, 33.5, 39.0):
        expected = output_offset(segments, seconds)
        assert expected is not None and abs(known(seconds) - expected) < 0.001
        if seconds <= 33.5:
            assert abs(solved(seconds) - expected) < 0.01
