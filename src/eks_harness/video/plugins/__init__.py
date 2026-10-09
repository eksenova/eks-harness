"""Plugin ABCs, registry and lifecycle hooks."""

from __future__ import annotations

from .base import (
    Captioner,
    EasingPlugin,
    Effect,
    Encoder,
    FrameProcessor,
    MarkerExtractor,
    MediaProvider,
    MediaRenderer,
    MediaRenderRequest,
    TransitionPlugin,
)
from .hooks import HOOK_NAMESPACE, hookimpl, hookspec
from .manager import PluginManager, get_plugin_manager
from .registry import (
    ensure_registry,
    get_transition,
    load_builtins,
    load_from_host,
    register_transition,
    register_captioner,
    register_easing,
    register_effect,
    register_encoder,
    register_marker_extractor,
    register_media_provider,
    register_media_renderer,
    registry_snapshot,
    resolve_easing_plugin,
    resolve_marker_extractor,
)

__all__ = [
    "HOOK_NAMESPACE",
    "Captioner",
    "EasingPlugin",
    "Effect",
    "Encoder",
    "FrameProcessor",
    "MarkerExtractor",
    "MediaProvider",
    "MediaRenderRequest",
    "MediaRenderer",
    "PluginManager",
    "get_plugin_manager",
    "hookimpl",
    "hookspec",
    "TransitionPlugin",
    "ensure_registry",
    "get_transition",
    "load_builtins",
    "load_from_host",
    "register_transition",
    "register_captioner",
    "register_easing",
    "register_effect",
    "register_encoder",
    "register_marker_extractor",
    "register_media_provider",
    "register_media_renderer",
    "registry_snapshot",
    "resolve_easing_plugin",
    "resolve_marker_extractor",
]
