"""HTTP surface for catalog registration, merge, retraction and lookup."""

from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import HTMLResponse

from app.web import page
from . import service

router = APIRouter()

ROWS = [
    {"key": "2026-11-01|01", "status": "已合并", "value": "→ 2026-11-01|01#2"},
    {"key": "2026-11-01|01#2", "status": "待裁决", "value": "1 页"},
    {"key": "2026-11-02|02", "status": "正常", "value": "8 页"},
]


@router.get("/catalogs", response_class=HTMLResponse)
def view(request: Request):
    return page(request, "m7.html", "目录合并", "/api/catalogs/merge", ROWS)


# ---------------------------------------------------------------------------
# registration / merge / retraction


@router.post("/api/catalogs/register")
def register(payload: dict = Body(default={})):
    return service.register_catalog(payload)


@router.post("/api/catalogs/merge")
def merge(payload: dict = Body(default={})):
    return service.merge_catalogs(payload)


@router.post("/api/catalogs/retract")
def retract(payload: dict = Body(default={})):
    return service.retract_merge(payload)


@router.post("/api/catalogs/merges/{merge_id}/retract")
def retract_by_id(merge_id: str, payload: dict = Body(default={})):
    payload = {**payload, "merge_id": merge_id}
    return service.retract_merge(payload)


# ---------------------------------------------------------------------------
# lookups


@router.get("/api/catalogs")
def catalogs():
    return {"catalogs": service.list_catalogs()}


@router.get("/api/catalogs/merges")
def merges(
    source_catalog_id: str | None = Query(default=None),
    target_catalog_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
):
    return {"merges": service.list_merges(source_catalog_id, target_catalog_id, status)}


@router.get("/api/catalogs/merges/{merge_id}")
def merge_detail(merge_id: str):
    return service.get_merge(merge_id)


@router.get("/api/catalogs/conflicts")
def conflicts(
    catalog_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
):
    return {"conflicts": service.list_conflicts(catalog_id, status)}


@router.get("/api/catalogs/conflicts/{conflict_id}")
def conflict_detail(conflict_id: str):
    return service.get_conflict(conflict_id)


@router.post("/api/catalogs/conflicts/{conflict_id}/resolve")
def resolve_conflict(conflict_id: str, payload: dict = Body(default={})):
    return service.resolve_conflict(conflict_id, payload)


@router.post("/api/catalogs/verify")
def verify(payload: dict = Body(default={})):
    return service.verify_credential(payload)


@router.get("/api/catalogs/{catalog_id}")
def catalog_detail(catalog_id: str):
    return service.catalog_detail(catalog_id)


@router.get("/api/catalogs/{catalog_id}/account")
def catalog_account(catalog_id: str):
    return service.catalog_account(catalog_id)


@router.get("/api/catalogs/{catalog_id}/scans")
def catalog_scans(catalog_id: str, view: str = Query(default="current")):
    return service.list_scans(catalog_id, view)


@router.get("/api/catalogs/{catalog_id}/quarantine")
def catalog_quarantine(catalog_id: str):
    return service.list_quarantine(catalog_id)


# ``directory`` aliases keep older callers working; the model is identical.
directory_router = APIRouter()


@directory_router.post("/api/directories/register")
def register_alias(payload: dict = Body(default={})):
    return service.register_catalog(payload)


@directory_router.post("/api/directories/merge")
def merge_alias(payload: dict = Body(default={})):
    return service.merge_catalogs(payload)


@directory_router.post("/api/directories/retract")
def retract_alias(payload: dict = Body(default={})):
    return service.retract_merge(payload)
