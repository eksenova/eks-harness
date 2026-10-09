"""Encoder negotiation tests (fake hardware reports)."""

from __future__ import annotations

import pytest

from eks_harness.video.plugins.builtin.encoders import (
    HardwareReport,
    Libx264Encoder,
    NvencEncoder,
    QsvEncoder,
    VideotoolboxEncoder,
    select_encoder,
)


def test_macos_with_videotoolbox_picked_first() -> None:
    report = HardwareReport(platform="Darwin", has_videotoolbox=True)
    assert isinstance(select_encoder(report), VideotoolboxEncoder)


def test_linux_with_nvenc_picked_first() -> None:
    report = HardwareReport(platform="Linux", has_nvenc=True)
    assert isinstance(select_encoder(report), NvencEncoder)


def test_linux_with_qsv_only() -> None:
    report = HardwareReport(platform="Linux", has_qsv=True)
    assert isinstance(select_encoder(report), QsvEncoder)


def test_falls_back_to_libx264() -> None:
    report = HardwareReport(platform="Linux")
    assert isinstance(select_encoder(report), Libx264Encoder)


def test_explicit_prefer_overrides() -> None:
    report = HardwareReport(platform="Linux", has_nvenc=True)
    assert isinstance(select_encoder(report, prefer="libx264"), Libx264Encoder)


def test_prefer_unknown_raises() -> None:
    report = HardwareReport(platform="Linux")
    with pytest.raises(LookupError):
        select_encoder(report, prefer="nvenc")
