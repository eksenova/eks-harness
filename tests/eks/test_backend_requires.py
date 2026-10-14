from __future__ import annotations

from typing import Any, cast

import pytest

import eks
from eks import t
from eks_harness.ir.registry import Registry, RegistryError


def test_backend_decorator_validates_requirements_identity_and_idle(eks_registry: Registry) -> None:
    @eks.backend("infra-x", identity="global", idle_timeout=t("600s"))
    class Infra:
        def describe(self): return {}
        def fingerprint(self, context): return ""
        def prepare(self, context): return None
        def spec(self, context): return {}
        def teardown(self, context, final=False): return None

    @eks.backend("api-x", requires=["infra-x", eks.requires("cache-x", key="k", params={"size": 1})])
    class Api(Infra):
        pass

    infra, api = eks_registry.backend("infra-x"), eks_registry.backend("api-x")
    assert infra.identity == "global" and infra.idle_timeout == 600.0
    assert [r.as_dict() for r in api.requires] == [{"name": "infra-x", "key": None, "params": {}},
                                                    {"name": "cache-x", "key": "k", "params": {"size": 1}}]
    with pytest.raises(RegistryError, match="identity"):
        eks.backend("bad", identity=cast(Any, "world"))
    with pytest.raises(RegistryError, match="itself"):
        eks.backend("self-x", requires=["self-x"])
    with pytest.raises(RegistryError, match="twice"):
        eks.backend("dup-x", requires=["a-x", "a-x"])
    with pytest.raises((RegistryError, TypeError)):
        eks.backend("idle-x", idle_timeout=cast(Any, 600))
