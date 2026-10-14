from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

import httpx
from PIL import Image, ImageDraw
from pydantic import BaseModel

import eks
from eks import by, t
from eks_harness.engine.executors import ExecContext
from eks_harness.plugins.sdk import BackendChoice, BackendInfo, BackendSpec, ProcessSpec


RATE_ENDPOINT = "https://api.example.com/rates"


def fetch_rate(call: ExecContext) -> dict[str, Any]:
    args = call.args
    assert isinstance(args, RateArgs)
    response = httpx.get(RATE_ENDPOINT, params={"symbol": args.currency, "base": args.base}, timeout=10.0)
    response.raise_for_status()
    return {"currency": args.currency, "base": args.base, "rate": response.json()["rate"]}


@eks.op("cookbook", name="rate", out="json", location="hub", executor=fetch_rate, idempotent=True,
        description="Reads a live exchange rate on the hub")
class RateArgs(eks.Args):
    currency: str = "XAU"
    base: str = "TRY"


@eks.macro(platforms=["web"], description="Opens an invoice draft for a branch and fills the amount")
def new_invoice(s: eks.Session, branch: str, amount: str = "2450.10") -> eks.Ref[None]:
    s.ui.nav("/tenant/invoice/new", params={"branch": branch})
    s.ui.fill("#amount", amount)
    return s.ui.tap(by.role("button", name="Kaydet"))


def _luminance(color: str) -> float:
    channels = [int(color.lstrip("#")[index:index + 2], 16) / 255 for index in (0, 2, 4)]
    linear = [value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4 for value in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def low_contrast(args: Contrast, texts: Sequence[tuple[str, str, str]]) -> list[str]:
    failing: list[str] = []
    for label, foreground, background in texts:
        lighter, darker = sorted((_luminance(foreground), _luminance(background)), reverse=True)
        if (lighter + 0.05) / (darker + 0.05) < args.minimum:
            failing.append(label)
    return failing


@eks.check("contrast", platforms=["web"], impl=low_contrast,
           description="Text contrast ratio stays above a minimum")
class Contrast(eks.Args):
    minimum: float = 4.5


@eks.effect
class Sparkle(eks.EffectBase, frozen=True):
    kind: Literal["sparkle"] = "sparkle"
    density: float = 1.0
    color: str = "#f5c542"


@eks.transition
class GoldWipe(eks.TransitionBase, frozen=True):
    kind: Literal["gold_wipe"] = "gold_wipe"
    duration: eks.Time = t("12f")


@eks.easing
class OutSoft(eks.EasingBase, frozen=True):
    kind: Literal["out_soft"] = "out_soft"
    softness: float = 0.6


@eks.curve
class Heartbeat(eks.CurveBase, frozen=True):
    kind: Literal["heartbeat"] = "heartbeat"
    bpm: float = 72.0
    depth: float = 0.08


def draw_badge(image: Image.Image, box: tuple[int, int, int, int], text: str) -> Image.Image:
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(box, radius=12, outline="#f5c542", width=4)
    draw.text((box[0] + 8, box[1] - 24), text, fill="#f5c542")
    return image


@eks.overlay_kind(live="cookbook/badge.js", screenshot=draw_badge, video=draw_badge,
                  description="A gold badge pinned to a target")
class Badge(eks.OverlayBase, frozen=True):
    kind: Literal["badge"] = "badge"
    anchor: eks.Target
    text: str


def locate_chips(tree: Mapping[str, Any], label: str) -> list[tuple[int, int, int, int]]:
    boxes: list[tuple[int, int, int, int]] = []
    pending = [tree]
    while pending:
        node = pending.pop()
        if node.get("role") in ("button", "option", "checkbox") and node.get("name") == label:
            x, y, width, height = node["bounds"]
            boxes.append((int(x), int(y), int(width), int(height)))
        pending.extend(reversed(node.get("children", [])))
    return boxes


@eks.target_kind("cookbook_chip", surfaces=["web", "screenshot", "video"], shorthand="chip",
                 resolvers={"web": "cookbook/chip.js", "screenshot": locate_chips, "video": locate_chips},
                 description="A filter chip by its label")
class Chip(BaseModel, frozen=True, extra="forbid"):
    label: str


@eks.renderer("svg", description="Rasterizes an SVG still to PNG")
def render_svg(source: Path, out: Path, *, size: tuple[int, int]) -> Path:
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.text((16, 16), source.read_text(encoding="utf-8")[:200], fill="#ffffff")
    image.save(out, format="PNG")
    return out


@eks.backend("staging-readonly", description="The shared staging API, never seeded")
class StagingReadonly:
    url = "https://api-staging.example.com"

    def describe(self) -> dict[str, Any]:
        return {"name": "staging-readonly", "url": self.url, "seeded": False}

    def fingerprint(self, context: Mapping[str, Any]) -> str:
        return hashlib.sha256(json.dumps({"url": self.url}, sort_keys=True).encode()).hexdigest()

    def prepare(self, context: Mapping[str, Any]) -> None:
        httpx.get(f"{self.url}/health", timeout=10.0).raise_for_status()

    def spec(self, context: Mapping[str, Any]) -> BackendSpec:
        return BackendSpec(info=BackendInfo(url=self.url))

    def teardown(self, context: Mapping[str, Any], final: bool = False) -> None:
        return None


@eks.backend("eksenapi", description="The API built from the tree, started locally on a database restored from the seed")
class LocalApi:
    project = Path("src") / "api"

    def describe(self) -> dict[str, Any]:
        return {"name": "eksenapi", "project": self.project.as_posix(), "seeded": True}

    def fingerprint(self, context: Mapping[str, Any]) -> str:
        root = Path(str(context["tree"]))
        digest = hashlib.sha256()
        for source in sorted((root / self.project).rglob("*.cs")):
            digest.update(source.relative_to(root).as_posix().encode())
            digest.update(source.read_bytes())
        return digest.hexdigest()

    def prepare(self, context: Mapping[str, Any]) -> None:
        project = Path(str(context["tree"])) / self.project
        if not project.is_dir():
            raise FileNotFoundError(f"the tree has no API project at {project}")

    def spec(self, context: Mapping[str, Any]) -> BackendSpec:
        root = Path(str(context["tree"]))
        process = ProcessSpec(name="api", command=["dotnet", "run", "--project", self.project.as_posix()],
                              port="http", cwd=str(root), env={"ASPNETCORE_ENVIRONMENT": "Development"})
        return BackendSpec(processes=[process])

    def teardown(self, context: Mapping[str, Any], final: bool = False) -> None:
        return None


@eks.backend_policy("local-when-api-changes", description="Local backend when the branch changes the API")
class LocalWhenApiChanges:
    def choose(self, tree: Path, requested: str | None = None) -> BackendChoice:
        if requested is not None:
            return BackendChoice(backend=requested, reason="requested explicitly")
        changed = (tree / "src" / "api").exists()
        return BackendChoice(backend="local" if changed else "staging-readonly",
                             reason="the tree has an API project" if changed else "no API project in the tree")


@eks.preset("cookbook-tablet", description="An iPad Pro sized touch viewport")
def tablet() -> eks.Viewport:
    return eks.Viewport(width=1024, height=1366, scale=2.0, mobile=True, touch=True)


@eks.preset("cookbook-edge", description="A slow mobile network on the edge of coverage")
def edge() -> eks.NetProfile:
    return eks.NetProfile(latency=t("400ms"), down_kbps=240, up_kbps=120, loss=0.02)
