"""Render driver subprocess.

Run as ``python -m eks_harness.studio.render_driver``. Loads a ``project.py``,
applies the requested mode/quality profile, drives the SDK's
:class:`eks_harness.video.render.Renderer`, and emits one JSON-line per progress
event on stdout. The parent ``render_project`` tool forwards those events.

Exit codes per failure class are defined in
:class:`eks_harness.video.render.ExitCode`. Every non-zero exit is preceded by a
terminal ``{"event":"error",...}`` (or ``{"event":"cancelled",...}``) event
so callers can surface the structured failure category to users without
parsing stderr.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import signal as _signal
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from eks_harness.video.render import (
    ExitCode,
    JsonProgressReporter,
    ProgressEvent,
    Renderer,
    RenderOptions,
    build_default_reporter,
    category_for,
)

__all__ = ["main"]

_LOG = logging.getLogger("eks_harness.studio.render_driver")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eks_harness.studio.render_driver")
    parser.add_argument("--project-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--mode", choices=["preview", "final"], default="preview")
    parser.add_argument("--preview-frames-dir", type=Path, default=None)
    parser.add_argument("--range-start", type=float, default=None)
    parser.add_argument("--range-end", type=float, default=None)
    parser.add_argument("--settings", type=str, default=None)
    parser.add_argument(
        "--no-tqdm",
        action="store_true",
        help="disable the tqdm bar even when stderr is a tty",
    )
    args = parser.parse_args(argv)

    inner = build_default_reporter(enable_tqdm=False if args.no_tqdm else None)
    reporter = _StepTrackingReporter(inner)
    started = time.monotonic()

    _install_signal_handlers(reporter)

    try:
        project = _load_project(args.project_dir / "project.py")
    except FileNotFoundError as exc:
        return _terminal_error(reporter, ExitCode.PROJECT_LOAD_ERROR, exc)
    except (ImportError, SyntaxError) as exc:
        return _terminal_error(reporter, ExitCode.PROJECT_LOAD_ERROR, exc)
    except ValidationError as exc:
        return _terminal_error(reporter, ExitCode.VALIDATION_ERROR, exc)
    except Exception as exc:
        return _terminal_error(reporter, ExitCode.PROJECT_LOAD_ERROR, exc)

    settings_overrides = json.loads(args.settings) if args.settings else {}
    _apply_quality_profile(project, mode=args.mode, overrides=settings_overrides)

    try:
        options = RenderOptions(
            output=args.output,
            mode=args.mode,
            range_start=args.range_start,
            range_end=args.range_end,
            workspace=args.project_dir,
            progress_reporter=reporter,
        )
        renderer = Renderer(project, options)
        final_path = renderer.render(args.output)
    except _CancelledFromSignal:
        return _terminal_cancelled(reporter)
    except NotImplementedError as exc:
        return _terminal_error(reporter, ExitCode.NOT_IMPLEMENTED, exc)
    except ValidationError as exc:
        return _terminal_error(reporter, ExitCode.VALIDATION_ERROR, exc)
    except KeyboardInterrupt:
        return _terminal_cancelled(reporter)
    except Exception as exc:
        category = _classify_render_exception(exc, current_step=reporter.current_step)
        return _terminal_error(reporter, category, exc)

    elapsed = round(time.monotonic() - started, 3)
    reporter.emit(
        ProgressEvent(
            "done",
            payload={
                "output": str(final_path),
                "total_elapsed_s": elapsed,
                "exit_code": int(ExitCode.SUCCESS),
            },
        )
    )
    reporter.close()
    return int(ExitCode.SUCCESS)


def _load_project(path: Path) -> Any:
    from eks_harness.video.loader import load_project_file

    return load_project_file(path, module_name="__eks_video_render_project__")


def _apply_quality_profile(project: Any, *, mode: str, overrides: dict[str, Any]) -> None:
    """Per plan §9.4 - preview swaps heavy plugins for light variants."""

    settings = getattr(project, "render_settings", None)
    if settings is None:
        return

    # ``resolution`` is the one override that lives on the Project rather than
    # on render_settings (which forbids extras). Pop it from the override map
    # and apply it directly, before the preview downscale so the long-axis cap
    # still keeps preview renders fast. Dimensions are rounded down to the
    # nearest even number to keep the yuv420p mux happy.
    overrides = dict(overrides)
    res_override = overrides.pop("resolution", None)
    if res_override is not None:
        with contextlib.suppress(Exception):
            req_w, req_h = res_override
            req_w = int(req_w)
            req_h = int(req_h)
            req_w -= req_w % 2
            req_h -= req_h % 2
            project.resolution = (req_w, req_h)

    if mode == "preview":
        with contextlib.suppress(Exception):
            settings.quality_profile = "preview"
        # Downscale the project's resolution proportionally so preview
        # renders stay fast without forcing landscape projects into a
        # vertical frame (or vice versa). Cap the long axis at 960; round
        # both dimensions down to the nearest even number so yuv420p mux
        # stays happy.
        with contextlib.suppress(Exception):
            width, height = project.resolution
            long_axis = max(width, height)
            preview_cap = 960
            if long_axis > preview_cap:
                scale = preview_cap / long_axis
                new_w = int(round(width * scale))
                new_h = int(round(height * scale))
                new_w -= new_w % 2
                new_h -= new_h % 2
                project.resolution = (new_w, new_h)

    for key, value in overrides.items():
        try:
            setattr(settings, key, value)
        except Exception:
            continue


class _CancelledFromSignal(BaseException):
    """Raised from a signal handler to unwind the render cleanly."""


def _install_signal_handlers(reporter: Any) -> None:
    """Translate SIGINT / CTRL_BREAK_EVENT into a structured cancellation.

    The handler raises :class:`_CancelledFromSignal` so the active
    :class:`Renderer.render()` call unwinds, finally-blocks fire (cleaning
    up ffmpeg children + caches), and ``main`` can emit the canonical
    ``cancelled`` event before exiting with code 130.
    """

    def _raise(*_args: Any) -> None:
        raise _CancelledFromSignal()

    with contextlib.suppress(Exception):
        _signal.signal(_signal.SIGINT, _raise)
    if sys.platform.startswith("win"):
        sigbreak = getattr(_signal, "SIGBREAK", None)
        if sigbreak is not None:
            with contextlib.suppress(Exception):
                _signal.signal(sigbreak, _raise)
    else:
        with contextlib.suppress(Exception):
            _signal.signal(_signal.SIGTERM, _raise)


def _classify_render_exception(
    exc: BaseException, *, current_step: str | None
) -> ExitCode:
    """Heuristic mapping from exception to a domain :class:`ExitCode`.

    The driver tracks the most recent ``step_start`` via
    :class:`_StepTrackingReporter`, so when an FFmpegError surfaces we can
    distinguish a failure during ``finalize`` (the segment→deliverable mux)
    from one during per-segment encode or audio stem mux.
    """

    name = type(exc).__name__
    if name == "FFmpegError" and current_step == "finalize":
        return ExitCode.MUX_ERROR
    return ExitCode.RENDER_PIPELINE_ERROR


class _StepTrackingReporter:
    """Decorator that records the most recent ``step_start`` for the driver.

    Exposes :attr:`current_step` so :func:`_classify_render_exception` can
    map a generic FFmpegError to the right exit-code bucket based on which
    phase was active when the error surfaced.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.current_step: str | None = None

    def emit(self, event: ProgressEvent) -> None:
        if event.event == "step_start":
            step = event.payload.get("step")
            if isinstance(step, str):
                self.current_step = step
        self._inner.emit(event)

    def close(self) -> None:
        try:
            self._inner.close()
        except Exception:
            _LOG.debug("inner reporter close raised", exc_info=True)


def _terminal_error(reporter: Any, code: ExitCode, exc: BaseException) -> int:
    """Emit a structured ``error`` event and return the matching exit code."""

    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    payload: dict[str, Any] = {
        "exit_code": int(code),
        "category": category_for(code),
        "message": str(exc) or type(exc).__name__,
        "exception": type(exc).__name__,
        "traceback": tb,
    }
    try:
        reporter.emit(ProgressEvent("error", payload=payload))
    except Exception:
        # Last-ditch fallback so the parent always sees something.
        fallback = JsonProgressReporter()
        fallback.emit(ProgressEvent("error", payload=payload))
    # Mirror the traceback to stderr so direct CLI users see it without
    # parsing the JSON event stream.
    sys.stderr.write(tb)
    with contextlib.suppress(Exception):
        reporter.close()
    return int(code)


def _terminal_cancelled(reporter: Any) -> int:
    payload = {"exit_code": int(ExitCode.CANCELLED), "category": "cancelled"}
    with contextlib.suppress(Exception):
        reporter.emit(ProgressEvent("cancelled", payload=payload))
    with contextlib.suppress(Exception):
        reporter.close()
    return int(ExitCode.CANCELLED)


if __name__ == "__main__":
    raise SystemExit(main())
