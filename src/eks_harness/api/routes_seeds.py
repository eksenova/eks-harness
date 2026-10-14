from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from starlette.concurrency import run_in_threadpool

from eks_harness.api.deps import current_principal, get_ctx, require_admin
from eks_harness.api.errors import not_found
from eks_harness.auth.core import Principal
from eks_harness.daemon.context import AppContext
from eks_harness.data.context import redact
from eks_harness.data.store import Manifest
from eks_harness.engine.runs import engine
from eks_harness.pools.base import BackendError

router = APIRouter(tags=["seeds"])
CurrentPrincipal = Annotated[Principal, Depends(current_principal)]
AdminPrincipal = Annotated[Principal, Depends(require_admin)]


def summary(manifest: Manifest) -> dict[str, Any]:
    return {"key": manifest.key, "seed": manifest.seed, "definition": manifest.definition,
            "migrations": manifest.migrations, "snapshot": manifest.snapshot, "digest": manifest.digest,
            "outputsDigest": manifest.outputs_digest, "resources": manifest.resources,
            "personas": sorted(manifest.personas), "bases": manifest.bases, "created": manifest.created,
            "buildMs": manifest.build_ms, "runs": manifest.runs, "deterministic": manifest.deterministic,
            "divergence": manifest.divergence.model_dump(mode="json") if manifest.divergence else None,
            "replay": [entry.path for entry in manifest.replay]}


def detail(manifest: Manifest) -> dict[str, Any]:
    return {**summary(manifest), "outputs": redact(manifest.outputs), "personaDetail": redact(manifest.personas)}


def found(ctx: AppContext, key: str) -> Manifest:
    manifest = engine(ctx).seed_engine.store.find(key)
    if manifest is None:
        raise not_found(f"No seed snapshot {key}.", error="seed_not_found")
    return manifest


@router.get("/api/seeds")
def list_seeds(request: Request, _: CurrentPrincipal,
               definition: Annotated[str | None, Query()] = None) -> dict[str, Any]:
    store = engine(get_ctx(request)).seed_engine.store
    items = sorted((summary(m) for m in store.all(definition)), key=lambda item: item["created"], reverse=True)
    return {"items": items}


@router.get("/api/seeds/{key}")
def show_seed(key: str, request: Request, _: CurrentPrincipal) -> dict[str, Any]:
    return detail(found(get_ctx(request), key))


@router.delete("/api/seeds/{key}")
async def drop_seed(key: str, request: Request, _: AdminPrincipal) -> dict[str, Any]:
    ctx = get_ctx(request)
    manifest = found(ctx, key)
    dropped_on: str | None = None
    note = ""
    if manifest.snapshot:
        running = [record for record in ctx.pools.backends.records.values()
                   if record.get("definition") == manifest.definition and record.get("status") == "running"]
        for record in running:
            try:
                answer = await run_in_threadpool(ctx.pools.backends.step, record["id"], "drop_snapshot",
                                                 name=manifest.snapshot)
            except BackendError as error:
                note = f"the drop_snapshot hook of {record['id']} failed: {str(error).splitlines()[0]}"
                continue
            if not answer.get("missing"):
                dropped_on = record["id"]
                break
        if dropped_on is None and not note:
            note = (f"no running {manifest.definition} instance dropped the snapshot {manifest.snapshot}; its "
                    "files stay with the backend's state")
    engine(ctx).seed_engine.store.drop(manifest.definition, manifest.key)
    return {"dropped": manifest.key, "seed": manifest.seed, "snapshotDroppedBy": dropped_on, "note": note or None}
