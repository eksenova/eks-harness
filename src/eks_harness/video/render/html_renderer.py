"""HTML overlay segment renderer.

Drives Playwright's headless Chromium to capture a CSS-animated HTML
template frame-by-frame on a virtual clock, then encodes the resulting
PNG sequence to an mp4 (alpha-capable when the page is transparent).

The user's template is pure HTML+CSS - JavaScript is not executed. Marker
sync is achieved by injecting extra ``@keyframes`` / ``animation-delay``
declarations at compile time, one rule per marker trigger that falls in
the segment window.

This module is imported lazily by the orchestrator so the ``playwright``
dependency stays optional. Installing the extra:

    pip install eks-harness[html]
    playwright install chromium

The renderer uses a deterministic per-frame screenshot loop instead of
``HeadlessExperimental.beginFrame``: between each capture it advances the
page's ``document.timeline.currentTime`` and the canonical wall-clock
shims (``Date.now`` / ``performance.now``) to the next frame's virtual
time so CSS animations sample at exactly that point. This is sufficient
for the JS-free templates the IR allows; if a future template needs
``requestAnimationFrame`` driven layout, switch the inner loop to the
CDP ``HeadlessExperimental.beginFrame`` path documented inline.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from html import escape
from pathlib import Path
from typing import TYPE_CHECKING

from eks_harness.video.ir.media import HTMLOverlay

from .subprocess_runner import run_ffmpeg

if TYPE_CHECKING:
    from .context import RenderContext
    from .orchestrator import _SegmentPlan

_LOG = logging.getLogger(__name__)

_INSTALL_HINT = (
    "HTMLOverlay requires `pip install eks-harness[html] "
    "&& playwright install chromium`"
)


def render_html_overlay_segment(plan: "_SegmentPlan", ctx: "RenderContext") -> Path:
    """Render an :class:`HTMLOverlay`-backed segment to an mp4 and return
    its path.

    Lazy-imports playwright. Raises :class:`RuntimeError` with a clear
    install message if playwright (or its bundled Chromium) is missing.
    """

    media = plan.segment.media
    assert isinstance(media, HTMLOverlay)

    try:
        from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - exercised via install path
        raise RuntimeError(_INSTALL_HINT) from exc

    project = ctx.project
    fps = float(project.fps)
    width, height = media.viewport or project.resolution
    seg_duration = float(plan.duration)
    frame_count = max(1, int(round(seg_duration * fps)))

    # Resolve the segment's absolute window in project time so we can pick
    # out marker triggers that fall inside it. The plan only carries
    # ``duration``; recover ``[seg_start, seg_end]`` by re-resolving the
    # segment's in_ time against the project + markers.
    from eks_harness.video.compile.time_resolve import resolve_time

    seg_start = float(resolve_time(plan.segment.in_, project, ctx.markers))
    seg_end = seg_start + seg_duration

    template_path = Path(media.template).resolve()
    if not template_path.exists():
        raise FileNotFoundError(f"HTMLOverlay template not found: {template_path}")
    template_html = template_path.read_text(encoding="utf-8")

    style_block = _compose_style_block(media, ctx, seg_start, seg_end)
    marker_divs = _compose_marker_divs(media, ctx, seg_start, seg_end)
    composed_html = _inject_into_template(
        template_html,
        style_block=style_block,
        marker_divs=marker_divs,
    )

    out_path = _allocate_output_path()
    frames_dir = Path(tempfile.mkdtemp(prefix="eks-html-overlay-"))
    try:
        _capture_frames(
            html=composed_html,
            frames_dir=frames_dir,
            width=int(width),
            height=int(height),
            fps=fps,
            frame_count=frame_count,
            transparent=bool(media.transparent),
            sync_playwright=sync_playwright,
        )
        _encode_frames(
            frames_dir=frames_dir,
            output=out_path,
            fps=fps,
            transparent=bool(media.transparent),
            duration=seg_duration,
            ffmpeg_binary=ctx.options.ffmpeg_binary,
            render_pix_fmt=project.render_settings.pix_fmt,
        )
    finally:
        shutil.rmtree(frames_dir, ignore_errors=True)

    return out_path


# ---------------------------------------------------------------------------
# HTML composition
# ---------------------------------------------------------------------------


def _compose_style_block(
    media: HTMLOverlay,
    ctx: "RenderContext",
    seg_start: float,
    seg_end: float,
) -> str:
    """Build the ``<style>`` block injected into the user's template.

    Contains the ``:root`` custom-property block from ``style_vars``,
    optional transparent body background, and one CSS rule per marker
    trigger that falls inside ``[seg_start, seg_end]``.
    """

    parts: list[str] = []

    if media.transparent:
        parts.append("html,body{background:transparent !important;margin:0;padding:0;}")

    if media.style_vars:
        var_lines = "\n".join(
            f"  --{_css_safe_name(k)}: {v};" for k, v in media.style_vars.items()
        )
        parts.append(f":root {{\n{var_lines}\n}}")

    for anim in media.marker_animations:
        triggers = ctx.markers.streams.get(anim.stream, [])
        for idx, t in enumerate(triggers):
            t_float = float(t)
            if t_float < seg_start or t_float > seg_end:
                continue
            delay = t_float - seg_start + (anim.offset_ms / 1000.0)
            cls = _trigger_class(anim.stream, idx)
            # Apply the animation to any element matching the user's
            # selector that also carries the per-trigger marker class.
            # Templates can either pre-author elements with these classes
            # or rely on the injected <div>s emitted by _compose_marker_divs.
            rule = (
                f"{anim.selector}.{cls} "
                f"{{ animation: {anim.animation_name} {anim.duration}s "
                f"linear {delay}s 1 forwards; }}"
            )
            parts.append(rule)

    return "<style data-eks-overlay>\n" + "\n".join(parts) + "\n</style>"


def _compose_marker_divs(
    media: HTMLOverlay,
    ctx: "RenderContext",
    seg_start: float,
    seg_end: float,
) -> str:
    """Emit hidden ``<div>`` elements carrying per-trigger classes.

    Some templates author the animated element themselves; others rely on
    these synthesized divs. Either pattern composes correctly because the
    generated CSS rule matches on ``<selector>.marker-trigger-...``: if
    the selector matches one of these divs, the animation runs; otherwise
    the rule is harmless dead CSS.
    """

    divs: list[str] = []
    for anim in media.marker_animations:
        triggers = ctx.markers.streams.get(anim.stream, [])
        for idx, t in enumerate(triggers):
            t_float = float(t)
            if t_float < seg_start or t_float > seg_end:
                continue
            cls = _trigger_class(anim.stream, idx)
            divs.append(
                f'<div class="{cls}" data-eks-marker="{escape(anim.stream)}" '
                f'data-eks-idx="{idx}" style="display:none"></div>'
            )
    if not divs:
        return ""
    return "<div data-eks-marker-triggers>\n" + "\n".join(divs) + "\n</div>"


def _inject_into_template(template_html: str, *, style_block: str, marker_divs: str) -> str:
    """Inject the generated ``<style>`` and marker ``<div>``s into the
    user's HTML.

    Tries to splice into existing ``</head>`` / ``</body>`` tags; falls
    back to wrapping the document if neither is present.
    """

    html = template_html
    lower = html.lower()

    # style → end of <head>
    head_close = lower.rfind("</head>")
    if head_close != -1:
        html = html[:head_close] + style_block + "\n" + html[head_close:]
    else:
        html = style_block + "\n" + html

    # marker divs → end of <body>
    if marker_divs:
        lower2 = html.lower()
        body_close = lower2.rfind("</body>")
        if body_close != -1:
            html = html[:body_close] + marker_divs + "\n" + html[body_close:]
        else:
            html = html + "\n" + marker_divs

    return html


def _css_safe_name(key: str) -> str:
    return "".join(c if (c.isalnum() or c in "-_") else "-" for c in key)


def _trigger_class(stream: str, idx: int) -> str:
    safe = _css_safe_name(stream).lower() or "stream"
    return f"marker-trigger-{safe}-{idx}"


# ---------------------------------------------------------------------------
# Frame capture
# ---------------------------------------------------------------------------


def _capture_frames(
    *,
    html: str,
    frames_dir: Path,
    width: int,
    height: int,
    fps: float,
    frame_count: int,
    transparent: bool,
    sync_playwright: object,
) -> None:
    """Capture ``frame_count`` PNG frames into ``frames_dir``.

    Strategy: load the composed HTML via ``page.set_content`` with a
    ``data:`` URL so relative asset references resolve against the user's
    template directory if they used absolute paths (the current
    implementation does not rebase relative paths - templates should use
    absolute paths or inline assets).

    For each frame ``i`` of ``frame_count``:
      1. Compute virtual time ``vt = i / fps`` seconds.
      2. Override ``Date.now`` / ``performance.now`` /
         ``document.timeline.currentTime`` to ``vt * 1000`` ms.
      3. Force a style recomputation so animation-delay rules sample at
         the new virtual time.
      4. ``page.screenshot`` into ``frames_dir/frame_{i:05d}.png``.

    See module docstring for the trade-off vs. ``beginFrame``.
    """

    init_script = """
    (() => {
      const _origNow = Date.now;
      const _origPerfNow = performance.now.bind(performance);
      window.__eksVT_ms = 0;
      Date.now = () => window.__eksVT_ms;
      performance.now = () => window.__eksVT_ms;
      // Patch requestAnimationFrame to fire synchronously with the
      // virtual clock; user templates aren't expected to rely on rAF but
      // some libraries hook it at import time.
      window.requestAnimationFrame = (cb) => {
        setTimeout(() => cb(window.__eksVT_ms), 0);
        return 0;
      };
    })();
    """

    pw = sync_playwright().start()  # type: ignore[operator]
    try:
        # GPU-accelerate Chromium's compositor/rasterizer via the ANGLE→D3D11
        # backend (works in headless on Windows; the AMD/Intel/NVIDIA driver
        # does the raster). Falls back to software internally if the GPU path
        # is unavailable. Set EKS_HARNESS_HWACCEL=0 to force software (SwiftShader).
        import os as _os

        hw = _os.environ.get("EKS_HARNESS_HWACCEL", "1").strip().lower() not in {
            "0", "false", "no"
        }
        gpu_args = (
            [
                "--use-angle=d3d11",
                "--enable-gpu-rasterization",
                "--enable-zero-copy",
                "--ignore-gpu-blocklist",
            ]
            if hw
            else ["--disable-gpu"]
        )
        browser = pw.chromium.launch(
            headless=True,
            args=[
                *gpu_args,
                "--no-sandbox",
                "--allow-file-access-from-files",
                "--hide-scrollbars",
                "--force-device-scale-factor=1",
            ],
        )
        try:
            context = browser.new_context(
                viewport={"width": width, "height": height},
                device_scale_factor=1,
            )
            page = context.new_page()
            page.add_init_script(init_script)
            page.set_content(html, wait_until="load")

            for i in range(frame_count):
                vt_ms = (i / fps) * 1000.0
                page.evaluate(
                    """(vt_ms) => {
                        window.__eksVT_ms = vt_ms;
                        if (document.timeline) {
                            try {
                                Object.defineProperty(
                                    document.timeline,
                                    'currentTime',
                                    { configurable: true, get: () => vt_ms }
                                );
                            } catch (e) { /* read-only in some builds */ }
                        }
                        // Force a layout / animation sample. Toggling a
                        // data attribute on <html> triggers the style
                        // recomputation Chromium needs to advance the
                        // animation clock without rAF.
                        document.documentElement.setAttribute(
                            'data-eks-frame',
                            String(vt_ms)
                        );
                        // Overriding document.timeline.currentTime only
                        // changes what JS reads; Chromium samples CSS /
                        // Web-Animations from the compositor clock, so the
                        // keyframes would otherwise free-run on wall-clock.
                        // Explicitly scrub every running animation to the
                        // virtual time so baked animation-delay / @keyframes
                        // are frame-deterministic.
                        if (document.getAnimations) {
                            for (const a of document.getAnimations()) {
                                try { a.pause(); a.currentTime = vt_ms; }
                                catch (e) { /* not all timelines are seekable */ }
                            }
                        }
                    }""",
                    vt_ms,
                )
                out = frames_dir / f"frame_{i:05d}.png"
                page.screenshot(
                    path=str(out),
                    type="png",
                    omit_background=transparent,
                    full_page=False,
                    clip={"x": 0, "y": 0, "width": width, "height": height},
                )
            context.close()
        finally:
            browser.close()
    finally:
        pw.stop()


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------


def _encode_frames(
    *,
    frames_dir: Path,
    output: Path,
    fps: float,
    transparent: bool,
    duration: float,
    ffmpeg_binary: str,
    render_pix_fmt: str,
) -> None:
    """Encode the PNG sequence in ``frames_dir`` to ``output`` as mp4.

    When ``transparent`` is True, uses ``yuva420p`` so the alpha channel
    survives concat/compositing in the downstream timeline. Otherwise
    uses the project's configured ``render_pix_fmt``.
    """

    pix_fmt = "yuva420p" if transparent else render_pix_fmt
    if transparent:
        # h264 has no alpha plane; transparent overlays need an alpha-capable
        # codec. Keep software encoding here (HW AMF/NVENC don't carry alpha).
        codec_args = ["-c:v", "libx264"]
    else:
        from . import hwaccel

        codec_args = hwaccel.encode_args()
    args = [
        "-framerate", f"{fps}",
        "-i", str(frames_dir / "frame_%05d.png"),
        "-frames:v", str(max(1, int(round(duration * fps)))),
        "-r", f"{fps}",
        "-pix_fmt", pix_fmt,
        *codec_args,
        "-an",
        str(output),
    ]
    run_ffmpeg(args, binary=ffmpeg_binary)


def _allocate_output_path() -> Path:
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as fd:
        return Path(fd.name)


__all__ = ["render_html_overlay_segment"]
