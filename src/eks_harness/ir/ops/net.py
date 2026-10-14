from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from eks_harness.ir.catalog.durations import Duration
from eks_harness.ir.ops.ui import LEASE, UI
from eks_harness.ir.presets import NetProfile
from eks_harness.ir.refs import RJson, RStr
from eks_harness.ir.registry import Args, op

CassetteMode = Literal["record", "replay", "auto"]
HttpMethod = Literal["*", "GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


class _Action(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", validate_by_name=True, validate_by_alias=True,
                              serialize_by_alias=True)


class Delay(_Action):
    kind: Literal["delay"] = Field(default="delay", description="Delay matching requests")
    by: Duration = Field(description="How long each matching request is held")


class Respond(_Action):
    kind: Literal["respond"] = Field(default="respond", description="Answer matching requests with a canned response")
    status: int = Field(default=200, ge=100, le=599, description="HTTP status of the response")
    body_json: RJson = Field(default=None, alias="json", description="JSON body (written json= in the builder)")
    body: RStr | None = Field(default=None, description="Text body")
    headers: dict[str, RStr] | None = Field(default=None, description="Response headers")


class PatchJson(_Action):
    kind: Literal["patch"] = Field(default="patch", description="Patch the JSON of the real response")
    path: str = Field(min_length=1, description="Path into the response JSON such as items[0].rate")
    value: RJson = Field(description="Value written at the path")

    @field_validator("value")
    @classmethod
    def _value_not_null(cls, value: RJson) -> RJson:
        if value is None:
            raise ValueError(
                "a patch value cannot be JSON null; patch the field with a concrete value or respond with a full body"
            )
        return value


class Abort(_Action):
    kind: Literal["abort"] = Field(default="abort", description="Fail matching requests")
    reason: Literal["failed", "aborted", "timedout", "connectionrefused", "internetdisconnected"] = Field(default="failed", description="Network error the request fails with")


class Passthrough(_Action):
    kind: Literal["passthrough"] = Field(default="passthrough", description="Let matching requests through unchanged, overriding broader routes")


RouteAction = Annotated[Delay | Respond | PatchJson | Abort | Passthrough, Field(discriminator="kind")]


@op("net", name="route", platforms=UI, resources=LEASE, location="worker", pinnable=True,
    description="Intercept matching requests: delay, respond, patch the JSON response, abort or pass through",
    examples=[{"method": "GET", "pattern": "/api/bank-transactions*", "action": {"kind": "delay", "by": "800ms"}},
              {"method": "POST", "pattern": "/api/invoices", "action": {"kind": "respond", "status": 500, "json": {"error": "x"}}}])
class Route(Args):
    method: HttpMethod = Field(default="*", description="HTTP method to match, or * for any")
    pattern: RStr = Field(description="URL glob such as /api/invoices*")
    action: RouteAction = Field(description="What happens to matching requests")
    times: int | None = Field(default=None, ge=1, description="Only the first N matching requests; every one when unset")


@op("net", name="unroute", platforms=UI, resources=LEASE, location="worker", pinnable=True,
    description="Remove routes for a pattern, or every route",
    examples=[{}, {"pattern": "/api/rates"}])
class Unroute(Args):
    pattern: RStr | None = Field(default=None, description="Pattern of the route to remove; every route when unset")


@op("net", name="shape", platforms=UI, resources=LEASE, location="worker", pinnable=True,
    description="Apply a deterministic network profile (latency, bandwidth, loss, offline)",
    examples=[{"profile": "3g"}])
class Shape(Args):
    profile: str | NetProfile = Field(description="Network profile preset name (wifi, 4g, 3g, slow-3g, offline) or an inline profile")


@op("net", name="cassette", platforms=UI, resources=LEASE, location="worker",
    container=True, branches=("body",), exports_body=True,
    description="Record or replay the body's network traffic as a cassette keyed by plan and backend fingerprint",
    examples=[{"name": "banka-arama", "mode": "replay"}])
class Cassette(Args):
    name: str = Field(min_length=1, max_length=120, description="Cassette artifact name")
    mode: CassetteMode = Field(default="auto", description="record, replay, or auto (replay when the cassette exists, else record)")


__all__ = [
    "Abort",
    "Cassette",
    "CassetteMode",
    "Delay",
    "HttpMethod",
    "Passthrough",
    "PatchJson",
    "Respond",
    "Route",
    "RouteAction",
    "Shape",
    "Unroute",
]
