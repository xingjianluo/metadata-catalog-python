"""FastAPI HTTP 适配层。

约定：
- 每个使用者通过 ``X-User`` 请求头标识；``X-User-Admin: true`` 表示管理员
  （仅用于本地演示的轻量身份模型）。
- 所有错误映射为带 ``reason`` 原因码的 4xx JSON，原因码与领域异常一一对应，
  不吞掉错误、不以空结果冒充越权。
- 列表/血缘响应都携带 ``snapshot_id``，调用方可凭其复现同一结论。
"""
from __future__ import annotations

from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .errors import (
    CatalogError,
    EntityRetiredError,
    NotFoundError,
    PermissionDeniedError,
)
from .models import EntityKind, FieldType, LineageLevel, User
from .service import MetadataCatalog

app = FastAPI(title="metadata-catalog")
catalog = MetadataCatalog()


# ---------- 请求/响应模型 ----------
class FieldIn(BaseModel):
    name: str
    type: FieldType


class RegisterEntityIn(BaseModel):
    name: str
    kind: EntityKind
    fields: list[FieldIn] = Field(default_factory=list)
    tags: set[str] | None = None
    owner: str


class AddFieldIn(BaseModel):
    type: FieldType


class LineageIn(BaseModel):
    source_entity: str
    target_entity: str
    level: LineageLevel
    source_column: str | None = None
    target_column: str | None = None
    transformation: str = "IDENTITY"


class RevokeLineageIn(BaseModel):
    source_entity: str
    target_entity: str
    level: LineageLevel
    source_column: str | None = None
    target_column: str | None = None


class GrantIn(BaseModel):
    user: str


class QueryIn(BaseModel):
    tags: set[str] | None = None
    owner: str | None = None
    limit: int = 10
    page_token: str | None = None
    snapshot_id: int | None = None


# ---------- 辅助 ----------
def _user(x_user: str | None, x_user_admin: str | None) -> User:
    if not x_user:
        from .errors import ValidationError
        raise ValidationError("missing X-User header", reason="missing_user")
    return User(name=x_user, is_admin=(x_user_admin or "").lower() == "true")


def _entity_dict(entity) -> dict:
    return {
        "id": entity.id,
        "name": entity.name,
        "kind": entity.kind.value,
        "fields": [{"name": f.name, "type": f.type.value} for f in entity.fields],
        "tags": sorted(entity.tags),
        "owner": entity.owner,
        "version": entity.version,
        "created_snapshot": entity.created_snapshot,
        "updated_snapshot": entity.updated_snapshot,
        "retired": entity.retired,
        "retired_snapshot": entity.retired_snapshot,
    }


def _impact_dict(result: dict) -> dict:
    def node(n):
        return {
            "entity_id": n.node.entity_id,
            "column": n.node.column,
            "depth": n.depth,
            "via_edge_id": n.via_edge_id,
            "cycle": n.cycle,
            "cycle_path": (
                [{"entity_id": x.entity_id, "column": x.column}
                 for x in n.cycle_path]
                if n.cycle else []
            ),
        }
    return {
        "snapshot_id": result["snapshot_id"],
        "direction": result["direction"],
        "start": result["start"],
        "nodes": [node(n) for n in result["nodes"]],
        "cycles_detected": [node(n) for n in result["cycles_detected"]],
    }


# ---------- 错误映射 ----------
@app.exception_handler(CatalogError)
async def catalog_error_handler(request, exc: CatalogError) -> JSONResponse:
    status = 404
    if isinstance(exc, PermissionDeniedError):
        status = 403
    elif isinstance(exc, EntityRetiredError):
        status = 410  # Gone：已退役，与 404/403 明确区分
    elif isinstance(exc, NotFoundError):
        status = 404
    elif exc.reason in ("validation_error", "invalid_page_token",
                        "invalid_limit", "missing_user"):
        status = 400
    else:
        status = 409  # 冲突/演进拒绝/血缘拒绝
    return JSONResponse(
        status_code=status,
        content={"error": type(exc).__name__, "reason": exc.reason,
                 "message": str(exc)},
    )


# ---------- 路由 ----------
@app.get("/")
def read_root() -> dict:
    return {"service": "metadata-catalog",
            "snapshot": catalog.store.current_snapshot()}


@app.put("/entities/{entity_id}")
def register_entity(entity_id: str, body: RegisterEntityIn):
    from .models import FieldSpec
    fields = [FieldSpec(name=f.name, type=f.type) for f in body.fields]
    entity, created = catalog.catalog.register_entity(
        entity_id, body.name, body.kind, fields, body.tags, body.owner
    )
    return {"entity": _entity_dict(entity), "created": created,
            "snapshot_id": entity.updated_snapshot}


@app.post("/entities/{entity_id}/fields/{field_name}")
def add_field(entity_id: str, field_name: str, body: AddFieldIn):
    from .models import FieldSpec
    entity, changed = catalog.catalog.add_field(
        entity_id, FieldSpec(field_name, body.type)
    )
    return {"entity": _entity_dict(entity), "changed": changed,
            "snapshot_id": entity.updated_snapshot}


@app.delete("/entities/{entity_id}/fields/{field_name}")
def drop_field(entity_id: str, field_name: str):
    entity = catalog.catalog.drop_field(entity_id, field_name)
    return {"entity": _entity_dict(entity), "snapshot_id": entity.updated_snapshot}


@app.get("/entities/{entity_id}")
def get_entity(entity_id: str, snapshot_id: int | None = None,
               x_user: str | None = Header(default=None),
               x_user_admin: str | None = Header(default=None)):
    user = _user(x_user, x_user_admin)
    snap = catalog.visibility.require_read(user, entity_id, snapshot_id)
    entity = catalog.catalog.get_entity(entity_id, snap)
    return {"entity": _entity_dict(entity), "snapshot_id": snap}


@app.put("/lineage")
def add_lineage(body: LineageIn):
    edge, created = catalog.lineage.add_lineage(
        body.source_entity, body.target_entity, body.level,
        body.source_column, body.target_column, body.transformation,
    )
    return {"edge_id": edge.id, "created": created,
            "snapshot_id": edge.created_snapshot}


@app.post("/lineage/revoke")
def revoke_lineage(body: RevokeLineageIn):
    edge, revoked = catalog.lineage.revoke_lineage(
        body.source_entity, body.target_entity, body.level,
        body.source_column, body.target_column,
    )
    return {"edge_id": edge.id, "revoked": revoked,
            "snapshot_id": catalog.store.current_snapshot()}


@app.post("/entities/{entity_id}/retire")
def retire_entity(entity_id: str):
    entity, retired = catalog.catalog.retire_entity(entity_id)
    return {"entity": _entity_dict(entity), "retired": retired,
            "snapshot_id": entity.updated_snapshot}


@app.get("/entities/{entity_id}/impact")
def impact(entity_id: str, direction: str = "downstream",
           column: str | None = None, snapshot_id: int | None = None,
           x_user: str | None = Header(default=None),
           x_user_admin: str | None = Header(default=None)):
    user = _user(x_user, x_user_admin)
    result = catalog.lineage.scoped_impact_analysis(
        user, entity_id, direction, column, snapshot_id
    )
    return _impact_dict(result)


@app.put("/grants/{entity_id}")
def grant(entity_id: str, body: GrantIn):
    snap = catalog.visibility.grant(body.user, entity_id)
    return {"granted": True, "entity_id": entity_id,
            "user": body.user, "snapshot_id": snap}


@app.put("/owner-access/{user_name}")
def grant_owner_access(user_name: str):
    snap = catalog.visibility.grant_owner_access(user_name)
    return {"granted": True, "user": user_name, "snapshot_id": snap}


@app.post("/entities/query")
def query_entities(body: QueryIn,
                   x_user: str | None = Header(default=None),
                   x_user_admin: str | None = Header(default=None)):
    user = _user(x_user, x_user_admin)
    result = catalog.discovery.query(
        user, tags=body.tags, owner=body.owner, limit=body.limit,
        page_token=body.page_token, snapshot_id=body.snapshot_id,
    )
    return {
        "snapshot_id": result["snapshot_id"],
        "total_matched": result["total_matched"],
        "next_page_token": result["next_page_token"],
        "items": [_entity_dict(e) for e in result["items"]],
    }
