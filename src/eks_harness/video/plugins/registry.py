"""Process-wide registries of the video plugin categories.

The registries are keyed by plugin ``name``. Each registration is idempotent
on instance identity. Registering a new effect re-binds the IR-level
``Effect`` discriminated union via ``rebuild_effect_union``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
import threading
from pathlib import Path
from typing import Any

from .base import (
    Captioner,
    TransitionPlugin,
    EasingPlugin,
    Effect,
    Encoder,
    MarkerExtractor,
    MediaProvider,
    MediaRenderer,
)

_LOG = logging.getLogger(__name__)

_EFFECTS: dict[str, Effect] = {}
_CAPTIONERS: dict[str, Captioner] = {}
_EASINGS: dict[str, EasingPlugin] = {}
_MEDIA_PROVIDERS: dict[str, MediaProvider] = {}
_ENCODERS: dict[str, Encoder] = {}
_MARKER_EXTRACTORS: dict[str, MarkerExtractor] = {}
_MEDIA_RENDERERS: dict[str, MediaRenderer] = {}

_TRANSITIONS: dict[str, TransitionPlugin] = {}
_LOADED = False
_LOAD_LOCK = threading.Lock()


def register_effect(plugin: Effect) -> None:
    from eks_harness.video.ir.effects import register_effect_model

    _EFFECTS[plugin.name] = plugin
    register_effect_model(plugin.model)  # type: ignore[arg-type]


def register_captioner(plugin: Captioner) -> bool:
    """Register a captioner, honoring its :meth:`Captioner.register` opt-out.

    Returns ``True`` when the plugin was actually registered and ``False``
    when it declined (e.g. mlx-whisper on a non-Apple-Silicon host).
    """

    if not plugin.register():
        return False
    _CAPTIONERS[plugin.name] = plugin
    return True


def register_transition(plugin: TransitionPlugin) -> None:
    _TRANSITIONS[plugin.name] = plugin


def get_transition(name: str) -> TransitionPlugin | None:
    return _TRANSITIONS.get(name)


def get_captioner(name: str) -> Captioner | None:
    return _CAPTIONERS.get(name)


def list_captioners() -> list[str]:
    return sorted(_CAPTIONERS)


def register_easing(plugin: EasingPlugin) -> None:
    _EASINGS[plugin.name] = plugin


def register_media_provider(plugin: MediaProvider) -> None:
    _MEDIA_PROVIDERS[plugin.name] = plugin


def register_encoder(plugin: Encoder) -> None:
    _ENCODERS[plugin.name] = plugin


def register_marker_extractor(plugin: MarkerExtractor) -> None:
    _MARKER_EXTRACTORS[plugin.name] = plugin


def register_media_renderer(plugin: MediaRenderer) -> None:
    """Register a media renderer and add its IR model to the ``MediaSource`` union."""

    from eks_harness.video.ir.media import register_media_model

    kind = plugin.model.model_fields["kind"].default
    _MEDIA_RENDERERS[kind] = plugin
    register_media_model(plugin.model)


def get_media_renderer(kind: str | None) -> MediaRenderer | None:
    return _MEDIA_RENDERERS.get(kind) if kind else None


def media_renderers() -> list[MediaRenderer]:
    return list(_MEDIA_RENDERERS.values())


def resolve_easing_plugin(name: str) -> EasingPlugin:
    try:
        return _EASINGS[name]
    except KeyError as exc:
        raise LookupError(f"no EasingPlugin registered under name {name!r}") from exc


def resolve_marker_extractor(handles_kind: str) -> MarkerExtractor:
    for plugin in _MARKER_EXTRACTORS.values():
        if plugin.handles == handles_kind:
            return plugin
    raise LookupError(f"no MarkerExtractor registered for kind {handles_kind!r}")


def registry_snapshot() -> dict[str, list[str]]:
    """Return a snapshot of every registered plugin name per category."""

    return {
        "effects": sorted(_EFFECTS),
        "captioners": sorted(_CAPTIONERS),
        "easings": sorted(_EASINGS),
        "media_providers": sorted(_MEDIA_PROVIDERS),
        "encoders": sorted(_ENCODERS),
        "marker_extractors": sorted(_MARKER_EXTRACTORS),
        "media_renderers": sorted(p.name for p in _MEDIA_RENDERERS.values()),
        "transitions": sorted(_TRANSITIONS),
    }


_REGISTERS: dict[str, Callable[[Any], Any]] = {
    "effect": register_effect,
    "transition": register_transition,
    "captioner": register_captioner,
    "easing": register_easing,
    "media_provider": register_media_provider,
    "encoder": register_encoder,
    "marker_source": register_marker_extractor,
    "media_renderer": register_media_renderer,
}
CONTRIBUTION_TYPES: tuple[str, ...] = tuple(_REGISTERS)


def _register(kind: str, instance: Any, label: str, loaded: dict[str, list[str]]) -> None:
    result = _REGISTERS[kind](instance)
    if result is False:
        _LOG.debug("plugin %s declined registration", label)
        return
    loaded[kind].append(label)


def load_from_host(host: Any, tree: Path | None = None) -> dict[str, list[str]]:
    """Register every active plugin contribution of a video category from a harness ``PluginHost``.

    Returns category -> contribution ids. A contribution that fails to load is
    logged and skipped; one bad plugin does not break the rest.
    """

    global _LOADED
    loaded: dict[str, list[str]] = {kind: [] for kind in _REGISTERS}
    for kind in _REGISTERS:
        for contribution in host.contributions(kind, tree):
            label = f"{contribution.plugin_id}:{contribution.id}"
            try:
                _register(kind, host.load(contribution, tree), label, loaded)
            except Exception:
                _LOG.exception("failed to load %s contribution %s", kind, label)
    _LOADED = True
    return loaded


def load_builtins(*, include_optional: bool = True) -> dict[str, list[str]]:
    """Register the video contributions of the built-in plugins without reading any configuration.

    ``include_optional`` also loads built-ins that are off by default (Blender).
    """

    import importlib

    from eks_harness.plugins.discovery import BUILTIN_ROOT, plugin_dirs
    from eks_harness.plugins.manifest import load_manifest

    global _LOADED
    loaded: dict[str, list[str]] = {kind: [] for kind in _REGISTERS}
    for root in plugin_dirs(BUILTIN_ROOT):
        manifest = load_manifest(root)
        if not manifest.enabled_by_default and not include_optional:
            continue
        for contribution in manifest.contributions:
            if contribution.type not in _REGISTERS:
                continue
            label = f"{contribution.plugin_id}:{contribution.id}"
            try:
                module_name, _, attr = contribution.entry.partition(":")
                target: Any = importlib.import_module(module_name)
                for part in attr.split("."):
                    target = getattr(target, part)
                _register(contribution.type, target() if isinstance(target, type) else target, label, loaded)
            except Exception:
                _LOG.exception("failed to load %s contribution %s", contribution.type, label)
    _LOADED = True
    return loaded


def ensure_registry(tree: Path | None = None) -> None:
    """Load the video plugins once per process from the harness plugin host.

    ``tree`` is a repository whose ``.harness`` plugins and project config
    apply; by default the tree containing the working directory.
    """

    global _LOADED
    if _LOADED:
        return
    with _LOAD_LOCK:
        if _LOADED:
            return
        from eks_harness.config import Config
        from eks_harness.paths import resolve_paths
        from eks_harness.plugins.host import PluginHost
        from eks_harness.plugins.project import find_tree

        paths = resolve_paths()
        host = PluginHost(paths, Config(paths))
        if tree is None:
            tree = find_tree(Path.cwd())
        load_from_host(host, tree)


def registry_loaded() -> bool:
    return _LOADED


__all__ = [
    "get_captioner",
    "get_media_renderer",
    "media_renderers",
    "list_captioners",
    "CONTRIBUTION_TYPES",
    "ensure_registry",
    "get_transition",
    "load_builtins",
    "load_from_host",
    "register_transition",
    "registry_loaded",
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
