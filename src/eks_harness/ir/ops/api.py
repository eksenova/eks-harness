from __future__ import annotations

from typing import Annotated

from pydantic import Field

from eks_harness.ir.refs import RJson, RStr
from eks_harness.ir.registry import Args, op

BACKEND = ("backend",)
StatusCode = Annotated[int, Field(ge=100, le=599)]


class Request(Args):
    path: RStr = Field(description="API path such as /api/tenant-branches, relative to the backend's base URL")
    query: dict[str, RJson] | None = Field(default=None, description="Query parameters")
    headers: dict[str, RStr] | None = Field(default=None, description="Extra request headers")
    persona: str | None = Field(default=None, min_length=1, description="Persona to call as; the session's persona when unset")
    expect_status: tuple[StatusCode, ...] | None = Field(default=None, min_length=1, description="Status codes that count as success; any 2xx when unset")


class BodyRequest(Request):
    body: RJson = Field(default=None, alias="json", description="JSON request body (written json= in the builder)")


@op("api", name="get", out="json", resources=BACKEND, idempotent=True,
    description="GET a backend API path as the session persona and return the JSON body",
    examples=[{"path": "/api/tenant-branches", "query": {"MaxResultCount": 50}}])
class Get(Request):
    pass


@op("api", name="delete", out="json", resources=BACKEND,
    description="DELETE a backend API path as the session persona",
    examples=[
        {"path": {"$fmt": ["/api/tenant-branches/", {"$ref": "n0000000001", "path": ["id"]}]}, "expect_status": [204]},
    ])
class Delete(Request):
    pass


@op("api", name="post", out="json", resources=BACKEND, description="POST JSON to a backend API path",
    examples=[{"path": "/api/tenant-branches", "json": {"name": "Moda"}, "expect_status": [200, 201]}])
class Post(BodyRequest):
    pass


@op("api", name="put", out="json", resources=BACKEND, description="PUT JSON to a backend API path",
    examples=[
        {
            "path": {"$fmt": ["/api/tenant-branches/", {"$ref": "n0000000001", "path": ["id"]}]},
            "json": {"name": "Moda Şubesi"},
        },
    ])
class Put(BodyRequest):
    pass


@op("api", name="patch", out="json", resources=BACKEND, description="PATCH JSON to a backend API path",
    examples=[{"path": "/api/tenant-settings", "json": {"invoicePrefix": "EKS"}, "persona": "harman-admin"}])
class PatchRequest(BodyRequest):
    pass


@op("session", name="as", location="worker", resources=("lease", "backend"), pinnable=True,
    description="Act as a persona from the seed: log the app in and use the persona for API calls",
    examples=[{"persona": "harman-admin"}])
class AsPersona(Args):
    persona: str = Field(min_length=1, description="Persona key from the seed, such as harman-admin")


@op("backend", name="info", out="json", resources=BACKEND, idempotent=True,
    description="URLs, versions and metadata of the backend lease",
    examples=[{}])
class BackendInfo(Args):
    pass


__all__ = ["BACKEND", "AsPersona", "BackendInfo", "BodyRequest", "Delete", "Get", "PatchRequest", "Post", "Put",
           "Request", "StatusCode"]
