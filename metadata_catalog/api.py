"""FastAPI HTTP 层：请求模型、序列化、异常映射与端点。

鉴权约定（本地模拟）：所有端点用 ``X-User`` 请求头标识调用者；
缺省为 ``anonymous``。实体 owner 与被授权用户可见，越权返回 403
（而不是空结果）。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .errors import CatalogError, RejectReason
from .models import ColumnDef, ColumnType, ImpactResult, LineageEdge, Page
from .store import CatalogStore

_HTTP_STATUS = {
    RejectReason.ENTITY_NOT_FOUND: 404,
    RejectReason.COLUMN_NOT_FOUND: 404,
    RejectReason.SNAPSHOT_NOT_FOUND: 404,
    RejectReason.UNAUTHORIZED_ENTITY: 403,
    RejectReason.UNAUTHORIZED_LINEAGE: 403,
    RejectReason.INVALID_CURSOR: 400,
}


class ColumnIn(BaseModel):
    name: str
    type: str
    nullable: bool = True
    default: Any | None = None


class EntityIn(BaseModel):
    id: str
    name: str
    owner: str
    columns: list[ColumnIn]
    tags: list[str] = Field(default_factory=list)


class LineageIn(BaseModel):
    source_entity: str
    target_entity: str
    source_col: str | None = None
    target_col: str | None = None


class GrantIn(BaseModel):
    user: str


def _parse_column_type(value: str) -> ColumnType:
    try:
        return ColumnType(value)
    except ValueError as exc:
        raise CatalogError(
            RejectReason.SCHEMA_TYPE_MISMATCH,
            f"unknown column type {value!r}",
            submitted_type=value,
            allowed=[t.value for t in ColumnType],
        ) from exc


def _column_in_to_def(col: ColumnIn) -> ColumnDef:
    return ColumnDef(
        name=col.name,
        type=_parse_column_type(col.type),
        nullable=col.nullable,
        default=col.default,
    )


def _entity_to_dict(entity: Entity) -> dict[str, Any]:
    return {
        "id": entity.id,
        "name": entity.name,
        "owner": entity.owner,
        "tags": sorted(entity.tags),
        "created_snapshot": entity.created_snapshot,
        "updated_snapshot": entity.updated_snapshot,
        "columns": [
            {
                "name": c.name,
                "type": c.type.value,
                "nullable": c.nullable,
                "default": c.default,
                "effective_default": c.effective_default(),
            }
            for c in entity.columns
        ],
    }


def _edge_to_dict(edge: LineageEdge) -> dict[str, Any]:
    return {
        "source_entity": edge.source_entity,
        "target_entity": edge.target_entity,
        "source_col": edge.source_col,
        "target_col": edge.target_col,
        "is_column_level": edge.is_column_level,
        "created_snapshot": edge.created_snapshot,
    }


def create_app(store: CatalogStore | None = None) -> FastAPI:
    store = store or CatalogStore()
    app = FastAPI(title="Local Metadata Catalog", version="0.1.0")

    @app.exception_handler(CatalogError)
    async def _catalog_error_handler(_request, exc: CatalogError) -> JSONResponse:
        status = _HTTP_STATUS.get(exc.reason, 422)
        return JSONResponse(status_code=status, content={"error": exc.to_dict()})

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "snapshot": store.current_snapshot().version}

    @app.post("/entities", status_code=201)
    def register_entity(payload: EntityIn) -> dict[str, Any]:
        result = store.register_entity(
            entity_id=payload.id,
            name=payload.name,
            owner=payload.owner,
            columns=[_column_in_to_def(c) for c in payload.columns],
            tags=payload.tags,
        )
        return {
            "entity_id": result.entity_id,
            "snapshot_version": result.snapshot_version,
            "evolved": result.evolved,
        }

    @app.post("/entities/{entity_id}/grants", status_code=201)
    def grant(entity_id: str, payload: GrantIn) -> dict[str, Any]:
        version = store.grant(entity_id, payload.user)
        return {"entity_id": entity_id, "user": payload.user, "snapshot_version": version}

    @app.post("/lineage", status_code=201)
    def add_lineage(payload: LineageIn) -> dict[str, Any]:
        version = store.add_lineage(
            source_entity=payload.source_entity,
            target_entity=payload.target_entity,
            source_col=payload.source_col,
            target_col=payload.target_col,
        )
        return {"snapshot_version": version}

    @app.get("/lineage")
    def lineage_between(
        source_entity: str,
        target_entity: str,
        x_user: str = Header(default="anonymous", alias="X-User"),
    ) -> dict[str, Any]:
        edges = store.lineage_between(source_entity, target_entity, x_user)
        snap = store.current_snapshot().version
        return {
            "snapshot_version": snap,
            "edges": [_edge_to_dict(e) for e in edges],
        }

    @app.get("/entities/{entity_id}/impact")
    def impact(
        entity_id: str,
        direction: str,
        x_user: str = Header(default="anonymous", alias="X-User"),
    ) -> dict[str, Any]:
        result: ImpactResult = store.impact(entity_id, direction, x_user)
        return {
            "root": result.root,
            "direction": result.direction,
            "snapshot_version": result.snapshot_version,
            "nodes": list(result.nodes),
            "edges": [_edge_to_dict(e) for e in result.edges],
            "cycles": [list(c) for c in result.cycles],
        }

    @app.get("/entities")
    def list_entities(
        tag: str | None = None,
        owner: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
        snapshot_version: int | None = None,
        x_user: str = Header(default="anonymous", alias="X-User"),
    ) -> dict[str, Any]:
        page: Page = store.list_entities(
            user=x_user,
            tag=tag,
            owner=owner,
            limit=limit,
            cursor=cursor,
            snapshot_version=snapshot_version,
        )
        return {
            "snapshot_version": page.snapshot_version,
            "next_cursor": page.next_cursor,
            "items": [_entity_to_dict(e) for e in page.items],
        }

    return app
