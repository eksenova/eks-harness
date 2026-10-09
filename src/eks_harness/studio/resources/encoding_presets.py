"""``eks-harness://video/encoding_presets/{name}`` - describe a built-in encoding preset.

Falls back to a hard-coded table when the SDK's ``builtin.encoders`` package
isn't yet shipped (Phase 2). Real encoder plugins can override entries by
registering under the ``eks_harness.video.encoders`` entry-point group.
"""

from __future__ import annotations

import json
from typing import Any

from ._spec import ResourceSpec

__all__ = ["BUILTIN_PRESETS", "RESOURCES", "read_preset"]


BUILTIN_PRESETS: dict[str, dict[str, Any]] = {
    "tiktok-1080-h264": {
        "container": "mp4",
        "video_codec": "h264",
        "resolution": [1080, 1920],
        "fps": 30,
        "bitrate_kbps": 8000,
        "color_primaries": "bt709",
        "audio_codec": "aac",
        "audio_bitrate_kbps": 192,
    },
    "reels-1080-h264": {
        "container": "mp4",
        "video_codec": "h264",
        "resolution": [1080, 1920],
        "fps": 30,
        "bitrate_kbps": 9000,
        "color_primaries": "bt709",
        "audio_codec": "aac",
        "audio_bitrate_kbps": 192,
    },
    "shorts-1080-h264": {
        "container": "mp4",
        "video_codec": "h264",
        "resolution": [1080, 1920],
        "fps": 30,
        "bitrate_kbps": 8000,
        "color_primaries": "bt709",
        "audio_codec": "aac",
        "audio_bitrate_kbps": 192,
    },
    "archival-h265": {
        "container": "mp4",
        "video_codec": "hevc",
        "crf": 18,
        "color_primaries": "bt709",
        "audio_codec": "aac",
        "audio_bitrate_kbps": 256,
    },
    "av1-experimental": {
        "container": "mp4",
        "video_codec": "av1",
        "crf": 30,
        "color_primaries": "bt709",
        "audio_codec": "opus",
        "audio_bitrate_kbps": 192,
    },
}


def read_preset(name: str) -> str:
    return json.dumps({"name": name, **_lookup_preset(name)}, indent=2, sort_keys=True)


def _lookup_preset(name: str) -> dict[str, Any]:
    try:
        from eks_harness.video.plugins.builtin import encoders as encoder_pkg

        getter = getattr(encoder_pkg, "preset", None)
        if getter is not None:
            preset = getter(name)
            if preset is not None:
                return dict(preset)
    except Exception:
        pass

    if name in BUILTIN_PRESETS:
        return BUILTIN_PRESETS[name]

    raise KeyError(f"unknown encoding preset {name!r}")


RESOURCES = [
    ResourceSpec(uri='eks-harness://video/encoding_presets/{name}', name='video_encoding_preset', title='Encoding preset',
                 description='Encoder profile (codec, bitrate, color) of the named preset.',
                 mime_type='application/json', read=read_preset, needs_roots=False, needs_registry=False),
]
