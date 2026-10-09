"""``eks-harness://video/image_lib/{path}`` - list/describe image assets under the media root."""

from __future__ import annotations


from ..roots import RootsManager, get_roots_manager
from ._spec import ResourceSpec
from ._media_lib import IMAGE_EXT, describe_media

__all__ = ["RESOURCES", "image_asset", "image_root"]


def image_root(*, roots: RootsManager | None = None) -> str:
    return describe_media(roots=roots or get_roots_manager(), rel_path="", extensions=IMAGE_EXT, category="image")


def image_asset(path: str, *, roots: RootsManager | None = None) -> str:
    return describe_media(roots=roots or get_roots_manager(), rel_path=path, extensions=IMAGE_EXT, category="image")


RESOURCES = [
    ResourceSpec(uri='eks-harness://video/image_lib', name='video_image_lib_root', title='Image library', description='List of image assets under the media library root.', mime_type='application/json', read=image_root, needs_roots=True),
    ResourceSpec(uri='eks-harness://video/image_lib/{path}', name='video_image_lib', title='Image asset', description='Image asset under the media library root.', mime_type='application/json', read=image_asset, needs_roots=True),
]
