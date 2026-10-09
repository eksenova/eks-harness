"""Tests for the SVT-AV1 software encoder plugin."""

from __future__ import annotations

from eks_harness.video.ir.render_settings import RenderSettings
from eks_harness.video.plugins.builtin.encoders import (
    HardwareReport,
    Libx264Encoder,
    SvtAv1Encoder,
    select_encoder,
)


def test_name_is_libsvtav1() -> None:
    assert SvtAv1Encoder().name == "libsvtav1"


def test_output_args_default_settings_use_av1_crf_30() -> None:
    encoder = SvtAv1Encoder()
    args = encoder.output_args(RenderSettings())
    assert args == ["-c:v", "libsvtav1", "-preset", "8", "-crf", "30"]


def test_output_args_with_explicit_crf() -> None:
    encoder = SvtAv1Encoder()
    args = encoder.output_args(RenderSettings(crf=24))
    assert args == ["-c:v", "libsvtav1", "-preset", "8", "-crf", "24"]


def test_output_args_with_explicit_bitrate_appends_b_v() -> None:
    encoder = SvtAv1Encoder()
    args = encoder.output_args(RenderSettings(bitrate_kbps=4500))
    assert args == [
        "-c:v",
        "libsvtav1",
        "-preset",
        "8",
        "-crf",
        "30",
        "-b:v",
        "4500k",
    ]


def test_prefer_libsvtav1_returns_svtav1_encoder() -> None:
    report = HardwareReport(platform="Linux")
    assert isinstance(select_encoder(report, prefer="libsvtav1"), SvtAv1Encoder)


def test_default_selection_does_not_pick_av1_on_stock_report() -> None:
    for platform in ("Linux", "Darwin", "Windows"):
        report = HardwareReport(platform=platform)
        assert isinstance(select_encoder(report), Libx264Encoder)


def test_default_selection_does_not_pick_av1_even_with_hw_accelerators() -> None:
    report = HardwareReport(
        platform="Darwin",
        has_videotoolbox=True,
        has_nvenc=True,
        has_qsv=True,
    )
    assert not isinstance(select_encoder(report), SvtAv1Encoder)
