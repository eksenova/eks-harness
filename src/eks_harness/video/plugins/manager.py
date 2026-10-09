"""Process-wide pluggy ``PluginManager`` singleton."""

from __future__ import annotations

import pluggy

from .hooks import HOOK_NAMESPACE, VideoHookSpec


class PluginManager:
    """Thin wrapper exposing the pluggy hook caller and plugin registration."""

    def __init__(self) -> None:
        self._pm: pluggy.PluginManager = pluggy.PluginManager(HOOK_NAMESPACE)
        self._pm.add_hookspecs(VideoHookSpec)

    @property
    def hook(self) -> pluggy.HookRelay:
        return self._pm.hook

    def register(self, plugin: object, name: str | None = None) -> None:
        self._pm.register(plugin, name=name)

    def unregister(self, plugin: object | None = None, name: str | None = None) -> None:
        self._pm.unregister(plugin=plugin, name=name)

    def is_registered(self, plugin: object) -> bool:
        return self._pm.is_registered(plugin)

    def list_plugins(self) -> list[tuple[str, object]]:
        return [(name, plugin) for name, plugin in self._pm.list_name_plugin()]


_INSTANCE: PluginManager | None = None


def get_plugin_manager() -> PluginManager:
    """Return the process-wide :class:`PluginManager`."""

    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = PluginManager()
    return _INSTANCE


__all__ = ["PluginManager", "get_plugin_manager"]
