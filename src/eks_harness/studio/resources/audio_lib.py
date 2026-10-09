"""``eks-harness://video/audio_lib/{path}`` - list/describe audio assets under the media root."""

from __future__ import annotations


from ..roots import RootsManager, get_roots_manager
from ._spec import ResourceSpec
from ._media_lib import AUDIO_EXT, describe_media

__all__ = ["RESOURCES", "audio_asset", "audio_root"]


def audio_root(*, roots: RootsManager | None = None) -> str:
    return describe_media(roots=roots or get_roots_manager(), rel_path="", extensions=AUDIO_EXT, category="audio")


def audio_asset(path: str, *, roots: RootsManager | None = None) -> str:
    return describe_media(roots=roots or get_roots_manager(), rel_path=path, extensions=AUDIO_EXT, category="audio")


RESOURCES = [
    ResourceSpec(uri='eks-harness://video/audio_lib', name='video_audio_lib_root', title='Audio library', description='List of audio assets under the media library root.', mime_type='application/json', read=audio_root, needs_roots=True),
    ResourceSpec(uri='eks-harness://video/audio_lib/{path}', name='video_audio_lib', title='Audio asset', description='Audio asset under the media library root.', mime_type='application/json', read=audio_asset, needs_roots=True),
]
