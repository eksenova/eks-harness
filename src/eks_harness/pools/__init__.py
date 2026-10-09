from __future__ import annotations

import importlib

from eks_harness.config import Config
from eks_harness.paths import Paths
from eks_harness.pools.base import (
    BackendError,
    BackendManager,
    BrowserPool,
    DevicePool,
    LiveSource,
    PoolError,
    PoolHost,
    Pools,
)
from eks_harness.pools.fake import FAKE_ENV, FakeBackendManager, FakeBrowserPool, FakeDevicePool, fake_enabled

REAL_IMPLEMENTATIONS = {
    "browsers": ("eks_harness.pools.browsers", "BrowserPool"),
    "devices": ("eks_harness.pools.devices", "DevicePool"),
    "backends": ("eks_harness.pools.backends", "BackendManager"),
}


def _real(area: str):
    module_name, class_name = REAL_IMPLEMENTATIONS[area]
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as error:
        if error.name == module_name:
            raise PoolError(
                f"{module_name} is not available; run with {FAKE_ENV}=1 or install a complete eks-harness") from error
        raise
    return getattr(module, class_name)


def create_pools(config: Config, paths: Paths, fake: bool | None = None) -> Pools:
    use_fake = fake_enabled() if fake is None else fake
    host = PoolHost(config, paths, fake=use_fake)
    host.load()
    if use_fake:
        pools = Pools(host, FakeBrowserPool(host), FakeDevicePool(host), FakeBackendManager(host))
    else:
        pools = Pools(host, _real("browsers")(host), _real("devices")(host), _real("backends")(host))
    pools.devices.define()
    return pools


__all__ = [
    "BackendError",
    "BackendManager",
    "BrowserPool",
    "DevicePool",
    "FAKE_ENV",
    "LiveSource",
    "PoolError",
    "PoolHost",
    "Pools",
    "create_pools",
    "fake_enabled",
]
