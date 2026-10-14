# pyright: strict
from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from fractions import Fraction
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from numpy.typing import NDArray
from pydantic import JsonValue

from eks_harness.engine.cache import ContentResolver
from eks_harness.engine.errors import NodeFailure
from eks_harness.engine.executors import (
    ArtifactSink,
    ExecContext,
    ExecResult,
    ResolveContext,
    SessionState,
)
from eks_harness.engine.model import RunInfo, RunResources
from eks_harness.engine.values import validate_args
from eks_harness.ir.plan import Node, Plan
from eks_harness.ir.registry import Registry, default_registry
from eks_harness.ir.time import TempoMap, TimeContext
from eks_harness.ir.types import Artifact
from eks_harness.media.gfx.compile import compile_scene
from eks_harness.ops.gfx import GFX_EXECUTOR, GfxExecutor, probe_gpu

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                                reason="ffmpeg/ffprobe not on PATH")

NODE_HASH = "sha256:" + "a" * 64
PLAN_HASH = "sha256:" + "b" * 64
WIDTH, HEIGHT = 160, 120
FPS = 10
Pixels = NDArray[np.uint8]


@pytest.fixture(autouse=True)
def registry() -> Iterator[Registry]:
    chosen = default_registry()
    state = chosen.snapshot()
    try:
        yield chosen
    finally:
        chosen.restore(state)


class Store:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.items: dict[str, tuple[Artifact, Path]] = {}

    def ingest(self, path: Path, *, project: str, session: str, kind: str | None, name: str | None, caption: str,
               tags: Sequence[str], meta: Mapping[str, JsonValue], actor: str | None, move: bool) -> Artifact:
        artifact_id = f"A{len(self.items) + 1:05d}"
        target = self.root / "store" / artifact_id / (name or path.name)
        target.parent.mkdir(parents=True)
        if move:
            shutil.move(path, target)
        else:
            shutil.copyfile(path, target)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        artifact = Artifact(id=artifact_id, kind=kind or "file", name=target.name, size=target.stat().st_size,
                            sha256=f"sha256:{digest}")
        self.items[artifact_id] = (artifact, target)
        return artifact

    def get(self, artifact_id: str) -> Artifact | None:
        found = self.items.get(artifact_id)
        return found[0] if found is not None else None

    def file(self, artifact_id: str) -> Path | None:
        found = self.items.get(artifact_id)
        return found[1] if found is not None else None

    def scratch(self) -> Path:
        base = self.root / "scratch"
        base.mkdir(exist_ok=True)
        return Path(tempfile.mkdtemp(dir=base))


class Sink:
    def __init__(self) -> None:
        self.progress_seen: list[float] = []

    def event(self, type: str, detail: Mapping[str, JsonValue]) -> None:
        return None

    def progress(self, fraction: float, message: str) -> None:
        self.progress_seen.append(fraction)

    def artifact(self, artifact: Artifact) -> None:
        return None


@pytest.fixture(scope="module")
def gpu_available() -> bool:
    try:
        probe_gpu()
    except NodeFailure:
        return False
    return True


def media(path: Path, kind: str = "image") -> dict[str, JsonValue]:
    return {"uri": f"file:{path}", "kind": kind}


def scene(layers: list[dict[str, JsonValue]], *, triggers: list[dict[str, JsonValue]] | None = None,
          background: str | None = None, duration: str = "1s") -> dict[str, JsonValue]:
    found: dict[str, JsonValue] = {"size": [WIDTH, HEIGHT], "fps": FPS, "duration": duration,
                                   "layers": list[JsonValue](layers)}
    if triggers is not None:
        found["triggers"] = list[JsonValue](triggers)
    if background is not None:
        found["background"] = background
    return found


def make_call(tmp_path: Path, args: dict[str, JsonValue]) -> tuple[ExecContext, Store]:
    registry = default_registry()
    spec = registry.op("gfx.render")
    resolved = validate_args(spec, args)
    node = Node(id="n0000000001", op="gfx.render", args=args, out=spec.out, hash=NODE_HASH)
    plan = Plan(kind="scene", name="gfx", nodes=[node], hash=PLAN_HASH)
    info = RunInfo(id="01JGFXTESTRUN000000000000", project="acme/gfx", session="gfx", plan_hash=PLAN_HASH,
                   plan_kind="scene", plan_name="gfx", created_at=time.time())
    store = Store(tmp_path)
    sink = Sink()
    clock = TimeContext.static(fps=Fraction(FPS), tempo=TempoMap(bpm=120.0))
    downloads = tmp_path / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    content = ContentResolver(store, tree=tmp_path, base=".", plugin_roots={}, scratch=downloads)
    call = ExecContext(run=info, plan=plan, registry=registry, node=node, spec=spec, instance="", attempt=1,
                       args=resolved.model, data=resolved.data, session=None, state=SessionState("main"),
                       resources=RunResources(), driver=None, sink=sink,
                       artifacts=ArtifactSink(store, run=info, node_id=node.id, instance="", op="gfx.render",
                                              sink=sink),
                       seed=0, clock=plan.clock, deadline=None, cancel=threading.Event(),
                       time_context=lambda: clock, content=content)
    return call, store


def render(tmp_path: Path, body: dict[str, JsonValue], *, cpu: bool = True) -> tuple[ExecResult, Path]:
    work = Path(tempfile.mkdtemp(dir=tmp_path))
    call, store = make_call(work, {"scene": body, "cpu": cpu})
    result = GfxExecutor().run(call)
    value = result.value
    assert isinstance(value, dict)
    artifact = value["artifact"]
    assert isinstance(artifact, str)
    path = store.file(artifact)
    assert path is not None
    return result, path


def decode(path: Path) -> Pixels:
    done = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "rgba", "-"],
                          capture_output=True, check=True)
    data = np.frombuffer(done.stdout, dtype=np.uint8)
    return data.reshape(-1, HEIGHT, WIDTH, 4)


def frames_of(tmp_path: Path, body: dict[str, JsonValue], *, cpu: bool = True) -> Pixels:
    return decode(render(tmp_path, body, cpu=cpu)[1])


def at(frame: Pixels, x: int, y: int) -> tuple[int, int, int, int]:
    pixel = frame[y, x]
    return int(pixel[0]), int(pixel[1]), int(pixel[2]), int(pixel[3])


def near(pixel: tuple[int, int, int, int], expected: tuple[int, int, int, int], tolerance: int = 6) -> bool:
    return all(abs(a - b) <= tolerance for a, b in zip(pixel, expected, strict=True))


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def png(tmp_path: Path, color: str, size: str = "20x20") -> Path:
    path = tmp_path / f"{color}.png"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={color}:s={size}",
                    "-frames:v", "1", str(path)], check=True)
    return path


def clip(tmp_path: Path) -> Path:
    path = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=64x48:r=10:d=2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def svg(tmp_path: Path) -> Path:
    path = tmp_path / "logo.svg"
    path.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20" viewBox="0 0 40 20">'
                    '<rect x="0" y="0" width="20" height="20" fill="#ff0000"/>'
                    '<circle cx="30" cy="10" r="10" fill="#00ff00"/></svg>', encoding="utf-8")
    return path


def rect(layer_id: str, width: float, height: float, fill: JsonValue, **extra: JsonValue) -> dict[str, JsonValue]:
    return {"id": layer_id, "kind": "rect", "width": width, "height": height, "fill": fill, **extra}


def every_kind(tmp_path: Path) -> dict[str, JsonValue]:
    return scene([
        rect("box", 30.0, 30.0, "#ff0000", x=-60.0, y=-40.0),
        {"id": "dot", "kind": "ellipse", "width": 30.0, "height": 30.0, "fill": "#00ff00", "x": 0.0, "y": -40.0},
        {"id": "line", "kind": "path", "d": "M -70 30 L -10 30", "stroke": "#0000ff", "stroke_width": 6.0},
        {"id": "label", "kind": "text", "text": "Eksen", "size": 24.0, "color": "#ffffff", "x": 40.0, "y": 30.0},
        {"id": "pic", "kind": "image", "media": media(png(tmp_path, "yellow")), "x": 60.0, "y": -40.0},
        {"id": "film", "kind": "video", "media": media(clip(tmp_path), "video"), "width": 32.0, "height": 24.0,
         "x": 60.0, "y": 40.0},
        {"id": "pair", "kind": "group", "x": 10.0, "y": 0.0, "rotation": 0.0, "children": [
            rect("inner", 10.0, 10.0, "#ff00ff", x=20.0),
        ]},
    ])


def test_executor_spec_draws_text() -> None:
    spec = GfxExecutor().spec()
    assert spec.name == GFX_EXECUTOR
    assert spec.ops == ("gfx.render",)
    assert spec.fonts is True
    assert spec.resolver is not None


def test_cpu_render_is_byte_identical(tmp_path: Path) -> None:
    body = every_kind(tmp_path)
    first_result, first = render(tmp_path, body)
    _second_result, second = render(tmp_path, body)
    assert sha(first) == sha(second)
    assert first_result.detail["frames"] == FPS
    assert first_result.resolution is not None
    assert first_result.resolution["renderer"] == "cpu"


def test_every_layer_kind_draws_where_expected(tmp_path: Path) -> None:
    frame = frames_of(tmp_path, every_kind(tmp_path))[0]
    assert near(at(frame, 20, 20), (255, 0, 0, 255))
    assert near(at(frame, 80, 20), (0, 255, 0, 255))
    assert near(at(frame, 40, 90), (0, 0, 255, 255))
    assert near(at(frame, 140, 20), (255, 255, 0, 255))
    assert near(at(frame, 140, 100), (0, 0, 255, 255), tolerance=12)
    assert near(at(frame, 110, 60), (255, 0, 255, 255))
    assert near(at(frame, 3, 60), (0, 0, 0, 0))
    text = frame[80:100, 90:150]
    assert int(text[..., 3].max()) == 255
    assert int((text[..., 3] > 0).sum()) > 40


def test_gpu_render_matches_itself_and_the_cpu_reference(tmp_path: Path, gpu_available: bool) -> None:
    if not gpu_available:
        pytest.skip("no OpenGL context on this machine")
    body = every_kind(tmp_path)
    gpu_result, first = render(tmp_path, body, cpu=False)
    _again, second = render(tmp_path, body, cpu=False)
    assert sha(first) == sha(second)
    resolution = gpu_result.resolution
    assert resolution is not None
    gpu = resolution["gpu"]
    assert isinstance(gpu, dict)
    assert gpu["adapter"] and gpu["driver"]
    reference = decode(render(tmp_path, body)[1]).astype(np.int16)
    drawn = decode(first).astype(np.int16)
    assert float(np.abs(reference - drawn).mean()) < 1.0


def test_resolver_reports_the_gpu(tmp_path: Path, gpu_available: bool) -> None:
    call, _store = make_call(tmp_path, {"scene": scene([]), "cpu": True})
    context = ResolveContext(run=call.run, plan=call.plan, registry=call.registry, node=call.node, spec=call.spec,
                             args=call.args, data=call.data, resources=call.resources)
    assert GfxExecutor().resolve(context)["renderer"] == "cpu"
    if not gpu_available:
        pytest.skip("no OpenGL context on this machine")
    gpu_call, _store = make_call(tmp_path / "gpu", {"scene": scene([]), "cpu": False})
    gpu_context = ResolveContext(run=gpu_call.run, plan=gpu_call.plan, registry=gpu_call.registry,
                                 node=gpu_call.node, spec=gpu_call.spec, args=gpu_call.args, data=gpu_call.data,
                                 resources=gpu_call.resources)
    found = GfxExecutor().resolve(gpu_context)
    gpu = found["gpu"]
    assert isinstance(gpu, dict)
    assert isinstance(gpu["adapter"], str) and gpu["adapter"]


def test_linear_and_radial_gradients(tmp_path: Path) -> None:
    linear: dict[str, JsonValue] = {"kind": "linear", "start": [0.0, 0.0], "end": [160.0, 0.0],
                                    "stops": [[0.0, "#ff0000"], [1.0, "#0000ff"]]}
    radial: dict[str, JsonValue] = {"kind": "radial", "center": [20.0, 20.0], "radius": 20.0,
                                    "stops": [[0.0, "#ffffff"], [1.0, "#00000000"]]}
    frame = frames_of(tmp_path, scene([
        rect("bar", 160.0, 40.0, linear, y=-40.0),
        rect("spot", 40.0, 40.0, radial, y=30.0),
    ]))[0]
    left, right = at(frame, 2, 20), at(frame, 157, 20)
    assert left[0] > 240 and left[2] < 15
    assert right[2] > 240 and right[0] < 15
    middle = at(frame, 80, 20)
    assert 100 < middle[0] < 160 and 100 < middle[2] < 160
    assert near(at(frame, 80, 90), (255, 255, 255, 255), tolerance=10)
    assert at(frame, 63, 73)[3] < 40


@pytest.mark.parametrize(("mode", "center", "corner"), [
    ("alpha", 255, 0),
    ("alpha_inverted", 0, 255),
    ("luminance", 255, 0),
    ("luminance_inverted", 0, 255),
])
def test_mask_modes(tmp_path: Path, mode: str, center: int, corner: int) -> None:
    frame = frames_of(tmp_path, scene([
        rect("card", 100.0, 100.0, "#ff0000", mask="window", mask_mode=mode),
        {"id": "window", "kind": "ellipse", "width": 40.0, "height": 40.0, "fill": "#ffffff"},
    ]))[0]
    assert at(frame, 80, 60)[3] == center
    assert at(frame, 35, 15)[3] == corner
    assert at(frame, 2, 2)[3] == 0


def test_luminance_mask_hides_under_dark_pixels(tmp_path: Path) -> None:
    frame = frames_of(tmp_path, scene([
        rect("card", 100.0, 100.0, "#ff0000", mask="shade", mask_mode="luminance"),
        rect("shade", 100.0, 100.0, "#000000"),
    ]))[0]
    assert at(frame, 80, 60)[3] == 0


def test_feathered_reveal_with_a_gradient_mask(tmp_path: Path) -> None:
    feather: dict[str, JsonValue] = {"kind": "linear", "start": [0.0, 0.0], "end": [100.0, 0.0],
                                     "stops": [[0.0, "#ffffffff"], [1.0, "#ffffff00"]]}
    frame = frames_of(tmp_path, scene([
        rect("card", 100.0, 40.0, "#f5c542", mask="reveal"),
        rect("reveal", 100.0, 40.0, feather),
    ]))[0]
    alphas = [at(frame, x, 60)[3] for x in (32, 56, 80, 104, 127)]
    assert alphas == sorted(alphas, reverse=True)
    assert alphas[0] > 220 and alphas[-1] < 35


@pytest.mark.parametrize(("blend", "expected"), [
    ("normal", (0, 255, 0, 255)),
    ("multiply", (0, 0, 0, 255)),
    ("screen", (255, 255, 0, 255)),
    ("add", (255, 255, 0, 255)),
    ("darken", (0, 0, 0, 255)),
    ("lighten", (255, 255, 0, 255)),
    ("difference", (255, 255, 0, 255)),
    ("dst_out", (0, 0, 0, 0)),
    ("dst_in", (255, 0, 0, 255)),
    ("src_atop", (0, 255, 0, 255)),
    ("xor", (0, 0, 0, 0)),
])
def test_blend_modes(tmp_path: Path, blend: str, expected: tuple[int, int, int, int]) -> None:
    frame = frames_of(tmp_path, scene([
        rect("base", 60.0, 60.0, "#ff0000"),
        rect("top", 60.0, 60.0, "#00ff00", blend=blend),
    ]))[0]
    assert near(at(frame, 80, 60), expected)


def test_src_atop_stays_inside_its_group(tmp_path: Path) -> None:
    frame = frames_of(tmp_path, scene([
        {"id": "word", "kind": "group", "children": [
            rect("ink", 40.0, 40.0, "#ff0000"),
            rect("sheen", 120.0, 20.0, "#ffffff", blend="src_atop"),
        ]},
    ]))[0]
    assert near(at(frame, 80, 60), (255, 255, 255, 255))
    assert near(at(frame, 30, 60), (0, 0, 0, 0))
    assert near(at(frame, 80, 75), (255, 0, 0, 255))


def test_text_with_the_shipped_font_tracking_and_wrapping(tmp_path: Path) -> None:
    body = scene([
        {"id": "narrow", "kind": "text", "text": "AV AV", "size": 20.0, "color": "#ffffff", "y": -40.0},
        {"id": "wide", "kind": "text", "text": "AV AV", "size": 20.0, "color": "#ffffff", "y": 0.0,
         "tracking": 8.0},
        {"id": "wrapped", "kind": "text", "text": "Kadıköy Şubesi", "size": 16.0, "color": "#ffffff",
         "y": 40.0, "max_width": 70.0, "weight": 400, "font": "IBM Plex Sans", "line_height": 1.25},
    ])
    frame = frames_of(tmp_path, body)[0]

    def ink(top: int, bottom: int) -> tuple[int, int]:
        columns = np.flatnonzero(frame[top:bottom, :, 3].max(axis=0) > 0)
        return int(columns[0]), int(columns[-1])

    def rows(top: int, bottom: int) -> int:
        return int(np.count_nonzero(frame[top:bottom, :, 3].max(axis=1) > 0))

    narrow, wide = ink(8, 32), ink(48, 72)
    assert wide[1] - wide[0] > narrow[1] - narrow[0] + 25
    assert rows(80, 120) > 24


def test_unshipped_weight_fails_with_font_unavailable(tmp_path: Path) -> None:
    call, _store = make_call(tmp_path, {"scene": scene([
        {"id": "bold", "kind": "text", "text": "Kalın", "weight": 700},
    ]), "cpu": True})
    with pytest.raises(NodeFailure) as failure:
        GfxExecutor().run(call)
    assert failure.value.code == "font_unavailable"
    assert "400, 600" in failure.value.message


def test_svg_image_is_drawn_as_vectors(tmp_path: Path) -> None:
    frame = frames_of(tmp_path, scene([
        {"id": "logo", "kind": "image", "media": media(svg(tmp_path)), "width": 80.0},
    ]))[0]
    assert near(at(frame, 60, 60), (255, 0, 0, 255))
    assert near(at(frame, 100, 60), (0, 255, 0, 255))
    assert at(frame, 60, 35)[3] == 0


def test_path_trim_draws_part_of_the_stroke(tmp_path: Path) -> None:
    frame = frames_of(tmp_path, scene([
        {"id": "rule", "kind": "path", "d": "M -60 0 L 60 0", "stroke": "#ffffff", "stroke_width": 4.0,
         "trim_end": {"keys": [{"t": "0s", "v": 0.0}, {"t": "1s", "v": 1.0}]}},
    ]))
    drawn = [int(np.count_nonzero(frame[index, 60, :, 3])) for index in range(FPS)]
    assert drawn[0] == 0
    assert drawn == sorted(drawn)
    assert drawn[5] < drawn[9]


def test_triggers_pulse_set_and_animate(tmp_path: Path) -> None:
    triggers: list[dict[str, JsonValue]] = [
        {"at": "0.5s", "layer": "beat", "action": {"kind": "pulse", "prop": "scale", "amount": 2.0,
                                                    "duration": "0.3s"}},
        {"at": "0.3s", "layer": "gone", "action": {"kind": "set", "prop": "opacity", "value": 0.0}},
        {"at": "0.2s", "layer": "mover", "action": {"kind": "animate", "prop": "x", "keys": [
            {"t": "0s", "v": -60.0}, {"t": "0.5s", "v": 60.0}]}},
    ]
    frames = frames_of(tmp_path, scene([
        rect("beat", 20.0, 20.0, "#ffffff", y=-40.0),
        rect("gone", 20.0, 20.0, "#ff0000", y=0.0),
        rect("mover", 10.0, 10.0, "#00ff00", x=-60.0, y=40.0),
    ], triggers=triggers))

    def width(frame: Pixels) -> int:
        return int(np.count_nonzero(frame[20, :, 3]))

    assert width(frames[4]) == 20
    assert width(frames[5]) >= 38
    assert 20 < width(frames[6]) < width(frames[5])
    assert width(frames[8]) == 20
    assert at(frames[2], 80, 60)[3] == 255
    assert at(frames[3], 80, 60)[3] == 0
    assert at(frames[1], 20, 100)[1] == 255
    assert at(frames[7], 140, 100)[1] == 255


def test_layer_start_and_end_follow_scene_local_time(tmp_path: Path) -> None:
    frames = frames_of(tmp_path, scene([
        rect("late", 40.0, 40.0, "#ffffff", start="0.4s", end="0.7s"),
    ]))
    shown = [at(frames[index], 80, 60)[3] for index in range(FPS)]
    assert shown == [0, 0, 0, 0, 255, 255, 255, 0, 0, 0]


def test_transparent_unless_a_background_is_set(tmp_path: Path) -> None:
    clear = frames_of(tmp_path, scene([rect("dot", 10.0, 10.0, "#ffffff")]))[0]
    assert at(clear, 3, 3) == (0, 0, 0, 0)
    filled = frames_of(tmp_path, scene([rect("dot", 10.0, 10.0, "#ffffff")], background="#102030"))[0]
    assert at(filled, 3, 3) == (16, 32, 48, 255)


def test_opacity_shadow_and_blur(tmp_path: Path) -> None:
    frame = frames_of(tmp_path, scene([
        rect("half", 30.0, 30.0, "#ffffff", x=-40.0, opacity=0.5),
        rect("soft", 30.0, 30.0, "#ffffff", x=40.0, blur=6.0,
             shadow={"color": "#000000ff", "blur": 0.0, "dx": 0.0, "dy": 20.0}),
    ]))[0]
    assert 120 <= at(frame, 40, 60)[3] <= 136
    edge = at(frame, 120, 46)[3]
    assert 0 < edge < 255
    assert at(frame, 120, 82)[3] > 0


def test_event_triggers_without_event_times_fail_clearly(tmp_path: Path) -> None:
    source: dict[str, JsonValue] = {"uri": "artifact:INTRO", "kind": "video", "artifact": "INTRO"}
    body = scene([rect("box", 10.0, 10.0, "#ffffff")], triggers=[
        {"at": {"source": source, "name": "logo.done", "where": {}}, "layer": "box",
         "action": {"kind": "set", "prop": "opacity", "value": 0.5}},
    ])
    clock = TimeContext.static(fps=Fraction(FPS), tempo=TempoMap(bpm=120.0))

    def unused(value: JsonValue) -> Path:
        raise AssertionError(f"no media expected, got {value!r}")

    def no_probe(path: Path) -> tuple[int, int]:
        raise AssertionError(f"no video expected, got {path}")

    with pytest.raises(NodeFailure) as failure:
        compile_scene(body, clock, cpu=True, media=unused, probe=no_probe, workdir=tmp_path)
    assert failure.value.code == "time_unresolved"
    assert "logo.done" in failure.value.message
    assert "artifact:INTRO" in failure.value.message


def test_gradient_stops_must_ascend() -> None:
    from pydantic import ValidationError

    from eks_harness.ir.ops.gfx import LinearGradient

    with pytest.raises(ValidationError, match="must not decrease"):
        LinearGradient(start=(0.0, 0.0), end=(1.0, 0.0), stops=((0.6, "#ffffff"), (0.2, "#000000")))


def test_builder_writes_gradients_mask_modes_and_new_blends() -> None:
    import eks
    from eks.gfx import linear_gradient

    @eks.scene(kind="gfx", size=(WIDTH, HEIGHT), fps=FPS)
    def card(sc: eks.GfxScene) -> None:
        plate = sc.rect(100.0, 40.0, id="plate", fill=linear_gradient((0.0, 0.0), (100.0, 0.0),
                                                                    [(0.0, "#ffffff"), (1.0, "#000000")]))
        window = sc.ellipse(40.0, 40.0, id="window", fill="#ffffff")
        sc.mask(plate, window, mode="luminance_inverted")
        with sc.group(id="g", blend="soft_light"):
            sc.text("x", id="t", tracking=2.0, line_height=1.1)

    plan = eks.plan(card)
    (node,) = [node for node in plan.nodes if node.op == "gfx.render"]
    layers: Any = node.args["scene"]
    plate, _window, group = layers["layers"]
    assert plate["fill"]["kind"] == "linear"
    assert plate["mask_mode"] == "luminance_inverted"
    assert group["blend"] == "soft_light"
    assert group["children"][0]["tracking"] == 2.0
