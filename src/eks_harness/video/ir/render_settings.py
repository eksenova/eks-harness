"""Render settings (encoder, preset, color)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

__all__ = ["RenderSettings"]


class RenderSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    encoder: str | None = None
    preset: str = "tiktok-1080-h264"
    quality_profile: Literal["preview", "draft", "final"] = "final"
    color_primaries: str = "bt709"
    color_trc: str = "bt709"
    colorspace: str = "bt709"
    pix_fmt: str = "yuv420p"
    bitrate_kbps: int | None = None
    crf: int | None = None
