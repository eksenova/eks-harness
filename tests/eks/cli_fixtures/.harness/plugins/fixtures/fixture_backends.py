from collections.abc import Mapping
from typing import Any

import eks
from eks_harness.plugins.sdk import BackendInfo, BackendSpec


@eks.backend("eksenapi", description="The API the CLI fixtures run against")
class FixtureApi:
    url = "http://127.0.0.1:7122"

    def describe(self) -> dict[str, Any]:
        return {"name": "eksenapi", "url": self.url}

    def fingerprint(self, context: Mapping[str, Any]) -> str:
        return self.url

    def prepare(self, context: Mapping[str, Any]) -> None:
        return None

    def spec(self, context: Mapping[str, Any]) -> BackendSpec:
        return BackendSpec(info=BackendInfo(url=self.url))

    def teardown(self, context: Mapping[str, Any], final: bool = False) -> None:
        return None
