"""``eks-harness://video/video_lib/{path}`` - list/describe video assets under the media root."""

from __future__ import annotations


from ..roots import RootsManager, get_roots_manager
from ._spec import ResourceSpec
from ._media_lib import VIDEO_EXT, describe_media

__all__ = ["RESOURCES", "video_asset", "video_root"]


def video_root(*, roots: RootsManager | None = None) -> str:
    return describe_media(roots=roots or get_roots_manager(), rel_path="", extensions=VIDEO_EXT, category="video")


def video_asset(path: str, *, roots: RootsManager | None = None) -> str:
    return describe_media(roots=roots or get_roots_manager(), rel_path=path, extensions=VIDEO_EXT, category="video")


RESOURCES = [
    ResourceSpec(uri='eks-harness://video/video_lib', name='video_video_lib_root', title='Video library', description='List of video assets under the media library root.', mime_type='application/json', read=video_root, needs_roots=True),
    ResourceSpec(uri='eks-harness://video/video_lib/{path}', name='video_video_lib', title='Video asset', description='Video asset under the media library root.', mime_type='application/json', read=video_asset, needs_roots=True),
]
