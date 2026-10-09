"""Built-in effect plugins.

Importing this package is enough to register every shipped effect when the
caller wants to skip entry-point discovery (e.g. tests). Production loaders
discover the same plugin classes through the ``eks_harness.video.effects`` entry
point group.
"""

from __future__ import annotations

from .auto_reframe import AutoReframePlugin
from .birefnet_remove import BiRefNetRemovePlugin
from .blur import BlurPlugin
from .brightness import BrightnessPlugin
from .captions import CaptionsPlugin
from .chromakey import ChromaKeyPlugin
from .colorgrade import ColorGradePlugin
from .contrast import ContrastPlugin
from .crop import CropPlugin
from .datamosh import DatamoshPlugin
from .fade import FadePlugin
from .film_grain import FilmGrainPlugin
from .flash import FlashPlugin
from .glitch import GlitchPlugin
from .invert import InvertPlugin
from .lower_third import LowerThirdPlugin
from .lut import LUTPlugin
from .matanyone_remove import MatAnyoneRemovePlugin
from .mirror import MirrorPlugin
from .pan import PanPlugin
from .posterize import PosterizePlugin
from .punch_in import PunchInPlugin
from .rgbsplit import RGBSplitPlugin
from .sam3_track import SAM3TrackPlugin
from .saturation import SaturationPlugin
from .shake import ShakePlugin
from .sharpen import SharpenPlugin
from .sticker import StickerOverlayPlugin
from .text_overlay import TextOverlayPlugin
from .vhs import VHSPlugin
from .vignette import VignettePlugin
from .watermark import WatermarkPlugin

__all__ = [
    "AutoReframePlugin",
    "BiRefNetRemovePlugin",
    "BlurPlugin",
    "BrightnessPlugin",
    "CaptionsPlugin",
    "ChromaKeyPlugin",
    "ColorGradePlugin",
    "ContrastPlugin",
    "CropPlugin",
    "DatamoshPlugin",
    "FadePlugin",
    "FilmGrainPlugin",
    "FlashPlugin",
    "GlitchPlugin",
    "InvertPlugin",
    "LUTPlugin",
    "LowerThirdPlugin",
    "MatAnyoneRemovePlugin",
    "MirrorPlugin",
    "PanPlugin",
    "PosterizePlugin",
    "PunchInPlugin",
    "RGBSplitPlugin",
    "SAM3TrackPlugin",
    "SaturationPlugin",
    "ShakePlugin",
    "SharpenPlugin",
    "StickerOverlayPlugin",
    "TextOverlayPlugin",
    "VHSPlugin",
    "VignettePlugin",
    "WatermarkPlugin",
]
