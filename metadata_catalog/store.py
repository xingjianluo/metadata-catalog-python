"""本地内存目录存储：带版本快照、读写并发控制、结构演进与可见性裁剪。

并发模型
========
* 所有写操作在同一把 ``RLock`` 内完成校验与提交，提交动作只是用一份新的
  不可变 ``Snapshot`` 替换 ``_head``，因此原子且有序。
* 读操作进入时获取当前（或指定版本的）快照引用，之后只对该快照做纯函数
  计算。写入与查询交错时，查询要么看到提交前、要么看到提交后的完整状态，
  绝不会看到半更新状态。
* 列表分页的 cursor 内嵌快照版本与偏移，翻页始终锁定首次查询时的快照，
  故并发写入不会造成翻页重复或遗漏。
"""

from __future__ import annotations

import base64
import json
import threading
from typing import Any
from collections.abc import Iterable

from .errors import CatalogError, RejectReason
from .models import (
    ColumnDef,
    ColumnType,
    Entity,
    ImpactResult,
    LineageEdge,
    Page,
    RegisterResult,
    Snapshot,
)


class CatalogStore:
    """单进程内存元数据目录。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        empty = Snapshot(version=0, entities={}, edges=(), acl={})
        self._snapshots: list[Snapshot] = [empty]
        self._head_version: int = 0

    # ------------------------------------------------------------------
    # 内部：快照提交
    # ------------------------------------------------------------------
    def _commit(
        self,
        entities: dict[str, Entity],
        edges: tuple[LineageEdge, ...],
        acl: dict[str, frozenset[str]],
    ) -> Snapshot:
        """在锁内原子提交新版本快照。"""
        new_version = self._head_version + 1
        snap = Snapshot(
            version=new_version,
            entities=dict(entities),
            edges=tuple(edges),
            acl={u: frozenset(v) for u, v in acl.items()},
        )
        self._snapshots.append(snap)
        self._head_version = new_version
        return snap

    def _head(self) -> Snapshot:
        return self._snapshots[self._head_version]

    # ------------------------------------------------------------------
    # 快照与可见性
    # ------------------------------------------------------------------
    def current_snapshot(self) -> Snapshot:
        """返回当前已提交快照（读操作的一致性基准）。"""
        with self._lock:
            return self._head()

    def snapshot_at(self, version: int) -> Snapshot:
        with self._lock:
            if version < 0 or version > self._head_version:
                raise CatalogError(
                    RejectReason.SNAPSHOT_NOT_FOUND,
                    f"snapshot {version} does not exist",
                    requested=version,
                    head=self._head_version,
                )
            return self._snapshots[version]

    def _visible_entity_ids(self, snap: Snapshot, user: str) -> frozenset[str]:
        """使用者可见的实体集合：owner 隐式可见 + 显式 ACL 授权。"""
        granted = snap.acl.get(user, frozenset())
        owned = {e.id for e in snap.entities.values() if e.owner == user}
        return frozenset(granted) | owned

    def _require_visible(self, snap: Snapshot, user: str, entity_id: str) -> Entity:
        entity = snap.entities.get(entity_id)
        if entity is None:
            raise CatalogError(
                RejectReason.ENTITY_NOT_FOUND,
                f"entity {entity_id!r} not found",
                entity_id=entity_id,
            )
        if entity_id not in self._visible_entity_ids(snap, user):
            raise CatalogError(
                RejectReason.UNAUTHORIZED_ENTITY,
                f"user {user!r} is not authorized for entity {entity_id!r}",
                user=user,
                entity_id=entity_id,
            )
        return entity

    # ------------------------------------------------------------------
    # 实体登记与结构演进
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_columns(columns: Iterable[ColumnDef]) -> tuple[ColumnDef, ...]:
        result: list[ColumnDef] = []
        seen: set[str] = set()
        for col in columns:
            if not isinstance(col, ColumnDef):
                raise CatalogError(
                    RejectReason.SCHEMA_TYPE_MISMATCH,
                    "column definition has wrong type",
                    received=type(col).__name__,
                )
            if col.name in seen:
                raise CatalogError(
                    RejectReason.SCHEMA_DUPLICATE_FIELD,
                    f"duplicate column {col.name!r} in registration",
                    column=col.name,
                )
            seen.add(col.name)
            result.append(col)
        return tuple(result)

    def register_entity(
        self,
        entity_id: str,
        name: str,
        owner: str,
        columns: Iterable[ColumnDef],
        tags: Iterable[str] = (),
    ) -> RegisterResult:
        """登记实体；同一 id 重复登记幂等并按确定规则演进结构。

        拒绝原因（不静默跳过任何问题）：

        * ``SCHEMA_DUPLICATE_FIELD``：同一次登记中字段名重复。
        * ``SCHEMA_IDENTITY_CONFLICT``：重复登记时 name/owner 与既有不同。
        * ``SCHEMA_MISSING_FIELD``：重复登记遗漏了既有字段（不允许删字段）。
        * ``SCHEMA_TYPE_MISMATCH``：同名字段类型或可空性发生变化。
        * ``NEW_COLUMN_NO_DEFAULT``：新增的 NOT NULL 字段没有显式缺省值。
        """
        new_cols = self._normalize_columns(columns)
        new_tags = frozenset(tags)

        with self._lock:
            snap = self._head()
            existing = snap.entities.get(entity_id)

            if existing is None:
                # 新实体：NOT NULL 列在新实体上无需默认值（登记即完整结构）。
                entity = Entity(
                    id=entity_id,
                    name=name,
                    owner=owner,
                    columns=new_cols,
                    tags=new_tags,
                    created_snapshot=snap.version + 1,
                    updated_snapshot=snap.version + 1,
                )
                entities = dict(snap.entities)
                entities[entity_id] = entity
                acl = {u: set(v) for u, v in snap.acl.items()}
                acl.setdefault(owner, set()).add(entity_id)
                committed = self._commit(entities, snap.edges, acl)
                return RegisterResult(entity_id, committed.version, evolved=False)

            # 重复登记：身份属性必须一致。
            if existing.name != name:
                raise CatalogError(
                    RejectReason.SCHEMA_IDENTITY_CONFLICT,
                    f"entity {entity_id!r} name conflict",
                    entity_id=entity_id,
                    existing_name=existing.name,
                    submitted_name=name,
                )
            if existing.owner != owner:
                raise CatalogError(
                    RejectReason.SCHEMA_IDENTITY_CONFLICT,
                    f"entity {entity_id!r} owner conflict",
                    entity_id=entity_id,
                    existing_owner=existing.owner,
                    submitted_owner=owner,
                )

            old_by_name = {c.name: c for c in existing.columns}
            evolved_cols: list[ColumnDef] = []
            evolved = False

            # 前缀必须与既有列逐一对齐：只允许尾部追加，禁止删列与改类型。
            for idx, submitted in enumerate(new_cols):
                old = old_by_name.get(submitted.name)
                if idx < len(existing.columns):
                    expected = existing.columns[idx]
                    if submitted.name != expected.name:
                        if submitted.name in old_by_name:
                            raise CatalogError(
                                RejectReason.SCHEMA_MISSING_FIELD,
                                (
                                    f"column {expected.name!r} missing at its "
                                    f"stable position {idx}"
                                ),
                                entity_id=entity_id,
                                missing=expected.name,
                                submitted=submitted.name,
                            )
                        raise CatalogError(
                            RejectReason.SCHEMA_DROP_FORBIDDEN,
                            f"dropping or reordering column {expected.name!r} is forbidden",
                            entity_id=entity_id,
                            column=expected.name,
                        )
                    if submitted.type != expected.type:
                        raise CatalogError(
                            RejectReason.SCHEMA_TYPE_MISMATCH,
                            f"column {submitted.name!r} type changed",
                            entity_id=entity_id,
                            column=submitted.name,
                            existing_type=expected.type.value,
                            submitted_type=submitted.type.value,
                        )
                    if submitted.nullable != expected.nullable:
                        raise CatalogError(
                            RejectReason.SCHEMA_TYPE_MISMATCH,
                            f"column {submitted.name!r} nullability changed",
                            entity_id=entity_id,
                            column=submitted.name,
                            existing_nullable=expected.nullable,
                            submitted_nullable=submitted.nullable,
                        )
                    evolved_cols.append(expected)
                else:
                    # 尾部新字段：必须有确定缺省语义。
                    if old is not None:
                        raise CatalogError(
                            RejectReason.SCHEMA_DUPLICATE_FIELD,
                            f"column {submitted.name!r} already exists",
                            entity_id=entity_id,
                            column=submitted.name,
                        )
                    if not submitted.nullable and submitted.default is None:
                        raise CatalogError(
                            RejectReason.NEW_COLUMN_NO_DEFAULT,
                            (
                                f"new NOT NULL column {submitted.name!r} requires "
                                "an explicit default"
                            ),
                            entity_id=entity_id,
                            column=submitted.name,
                        )
                    evolved_cols.append(submitted)
                    evolved = True

            if len(new_cols) < len(existing.columns):
                missing = tuple(
                    c.name for c in existing.columns[len(new_cols):]
                )
                raise CatalogError(
                    RejectReason.SCHEMA_MISSING_FIELD,
                    "re-registration omitted existing columns",
                    entity_id=entity_id,
                    missing=missing,
                )

            merged_tags = existing.tags | new_tags
            if not evolved and merged_tags == existing.tags:
                # 完全一致的重复登记：幂等，不产生新版本。
                return RegisterResult(entity_id, snap.version, evolved=False)

            entity = Entity(
                id=existing.id,
                name=existing.name,
                owner=existing.owner,
                columns=tuple(evolved_cols),
                tags=merged_tags,
                created_snapshot=existing.created_snapshot,
                updated_snapshot=snap.version + 1,
            )
            entities = dict(snap.entities)
            entities[entity_id] = entity
            committed = self._commit(entities, snap.edges, snap.acl)
            return RegisterResult(entity_id, committed.version, evolved=True)

    def grant(self, entity_id: str, user: str) -> int:
        """授予某使用者对实体的可见权限（幂等）。"""
        with self._lock:
            snap = self._head()
            if entity_id not in snap.entities:
                raise CatalogError(
                    RejectReason.ENTITY_NOT_FOUND,
                    f"entity {entity_id!r} not found",
                    entity_id=entity_id,
                )
            acl = {u: set(v) for u, v in snap.acl.items()}
            granted = acl.setdefault(user, set())
            if entity_id in granted:
                return snap.version
            granted.add(entity_id)
            committed = self._commit(dict(snap.entities), snap.edges, acl)
            return committed.version

    # ------------------------------------------------------------------
    # 血缘
    # ------------------------------------------------------------------
    @staticmethod
    def _validate_col(entity: Entity, col: str | None, side: str) -> None:
        if col is None:
            return
        if entity.column(col) is None:
            raise CatalogError(
                RejectReason.COLUMN_NOT_FOUND,
                f"{side} column {col!r} does not exist on {entity.id!r}",
                entity_id=entity.id,
                column=col,
            )

    def add_lineage(
        self,
        source_entity: str,
        target_entity: str,
        source_col: str | None = None,
        target_col: str | None = None,
    ) -> int:
        """登记一条表级或列级血缘边（同一 key 重复登记幂等）。

        * 列级血缘要求两端的列名在当前快照中均存在。
        * 同一对端点不能同时存在表级与列级血缘（粒度冲突，明确拒绝）。
        * 表级自环允许登记（环路必须能被分析明确标记，而非写入时截断）。
        """
        if (source_col is None) != (target_col is None):
            raise CatalogError(
                RejectReason.SCHEMA_TYPE_MISMATCH,
                "column-level lineage requires both source_col and target_col",
                source_col=source_col,
                target_col=target_col,
            )
        with self._lock:
            snap = self._head()
            src = snap.entities.get(source_entity)
            if src is None:
                raise CatalogError(
                    RejectReason.ENTITY_NOT_FOUND,
                    f"source entity {source_entity!r} not found",
                    entity_id=source_entity,
                )
            tgt = snap.entities.get(target_entity)
            if tgt is None:
                raise CatalogError(
                    RejectReason.ENTITY_NOT_FOUND,
                    f"target entity {target_entity!r} not found",
                    entity_id=target_entity,
                )
            self._validate_col(src, source_col, "source")
            self._validate_col(tgt, target_col, "target")

            new_edge = LineageEdge(
                source_entity=source_entity,
                target_entity=target_entity,
                source_col=source_col,
                target_col=target_col,
                created_snapshot=snap.version + 1,
            )
            for edge in snap.edges:
                if edge.key == new_edge.key:
                    return snap.version  # 幂等：完全相同的边
                if edge.table_key == new_edge.table_key and (
                    edge.is_column_level != new_edge.is_column_level
                ):
                    raise CatalogError(
                        RejectReason.LINEAGE_DUPLICATE_CONFLICT,
                        (
                            f"table {source_entity!r} -> {target_entity!r} already "
                            f"has {'column' if edge.is_column_level else 'table'}"
                            "-level lineage; cannot add the other granularity"
                        ),
                        source_entity=source_entity,
                        target_entity=target_entity,
                    )
            edges = snap.edges + (new_edge,)
            committed = self._commit(dict(snap.entities), edges, snap.acl)
            return committed.version

    def impact(self, entity_id: str, direction: str, user: str) -> ImpactResult:
        """在同一快照上做可复现的上/下游影响分析。

        * 根实体不可见时抛 ``UNAUTHORIZED_ENTITY``（越权被拒绝，不返回空）。
        * 遍历只经过调用者两端均可见的边；不可见端点不进入可见子图。
        * 环路被明确收集在 ``cycles`` 中（自环为单元素环），不会被截断，
          节点、边、环路均按确定顺序排序，同一快照结论可复现。
        """
        if direction not in ("upstream", "downstream"):
            raise CatalogError(
                RejectReason.SCHEMA_TYPE_MISMATCH,
                "direction must be 'upstream' or 'downstream'",
                direction=direction,
            )
        snap = self.current_snapshot()
        self._require_visible(snap, user, entity_id)

        visible = self._visible_entity_ids(snap, user)

        # 表级投影（表级 + 列级边都参与表级展开），去重后形成确定邻接表。
        pairs = {
            (e.source_entity, e.target_entity)
            for e in snap.edges
            if e.source_entity in visible and e.target_entity in visible
        }
        adjacency: dict[str, set[str]] = {}
        for src, tgt in pairs:
            if direction == "downstream":
                adjacency.setdefault(src, set()).add(tgt)
            else:
                adjacency.setdefault(tgt, set()).add(src)

        # 迭代式 DFS：white=0 未访问, gray=1 在当前递归栈, black=2 已完成。
        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[str, int] = {entity_id: WHITE}
        stack: list[tuple[str, Any]] = [(entity_id, iter(sorted(adjacency.get(entity_id, ()))))]
        cycles: set[tuple[str, ...]] = set()
        while stack:
            node, it = stack[-1]
            color[node] = GRAY
            advanced = False
            for nxt in it:
                if color.get(nxt, WHITE) == GRAY:
                    # 回边 => 环路：取出栈中从 nxt 到当前节点的路径。
                    path = [s for s, _ in stack]
                    if nxt in path:
                        ring = tuple(path[path.index(nxt):])
                        cycles.add(self._canonical_cycle(ring))
                    else:  # 自环
                        cycles.add((nxt,))
                    continue
                if color.get(nxt, WHITE) == BLACK:
                    continue
                color[nxt] = WHITE
                stack.append((nxt, iter(sorted(adjacency.get(nxt, ())))))
                advanced = True
                break
            if not advanced:
                color[node] = BLACK
                stack.pop()

        visited = frozenset(n for n, c in color.items() if c == BLACK)
        nodes = tuple(sorted(visited))
        edges = tuple(
            sorted(
                (
                    e
                    for e in snap.edges
                    if e.source_entity in visited and e.target_entity in visited
                ),
                key=lambda e: e.key,
            )
        )
        return ImpactResult(
            root=entity_id,
            direction=direction,
            nodes=nodes,
            edges=edges,
            cycles=tuple(sorted(cycles)),
            snapshot_version=snap.version,
        )

    @staticmethod
    def _canonical_cycle(ring: tuple[str, ...]) -> tuple[str, ...]:
        """把环路旋转到字典序最小的起点，使同一环只有一种表示。"""
        if len(ring) <= 1:
            return ring
        start = min(range(len(ring)), key=lambda i: ring[i])
        return tuple(ring[start:] + ring[:start])

    def lineage_between(
        self, source_entity: str, target_entity: str, user: str
    ) -> tuple[LineageEdge, ...]:
        """显式查询两端点之间的血缘；任一端点越权即拒绝（不返回空）。"""
        snap = self.current_snapshot()
        visible = self._visible_entity_ids(snap, user)
        if source_entity not in snap.entities:
            raise CatalogError(
                RejectReason.ENTITY_NOT_FOUND,
                f"entity {source_entity!r} not found",
                entity_id=source_entity,
            )
        if target_entity not in snap.entities:
            raise CatalogError(
                RejectReason.ENTITY_NOT_FOUND,
                f"entity {target_entity!r} not found",
                entity_id=target_entity,
            )
        if source_entity not in visible or target_entity not in visible:
            raise CatalogError(
                RejectReason.UNAUTHORIZED_LINEAGE,
                (
                    f"user {user!r} not authorized to view lineage "
                    f"{source_entity!r} -> {target_entity!r}"
                ),
                user=user,
                source_entity=source_entity,
                target_entity=target_entity,
            )
        return tuple(
            sorted(
                (
                    e
                    for e in snap.edges
                    if e.source_entity == source_entity
                    and e.target_entity == target_entity
                ),
                key=lambda e: e.key,
            )
        )

    # ------------------------------------------------------------------
    # 发现
    # ------------------------------------------------------------------
    @staticmethod
    def _encode_cursor(version: int, offset: int, tag: str | None, owner: str | None) -> str:
        payload = json.dumps(
            {"v": version, "o": offset, "t": tag, "w": owner},
            separators=(",", ":"),
            sort_keys=True,
        )
        return base64.urlsafe_b64encode(payload.encode()).decode()

    @staticmethod
    def _decode_cursor(cursor: str) -> dict[str, Any]:
        try:
            raw = base64.urlsafe_b64decode(cursor.encode()).decode()
            data = json.loads(raw)
            if not isinstance(data, dict) or not isinstance(data.get("v"), int) or not isinstance(
                data.get("o"), int
            ):
                raise ValueError
            return data
        except (ValueError, UnicodeDecodeError) as exc:
            raise CatalogError(
                RejectReason.INVALID_CURSOR, "malformed pagination cursor", cursor=cursor
            ) from exc

    def list_entities(
        self,
        user: str,
        tag: str | None = None,
        owner: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
        snapshot_version: int | None = None,
    ) -> Page:
        """按可见性 + 标签 + 归属过滤并稳定分页。

        首页取当前快照（或指定版本），cursor 内嵌快照版本、过滤条件与偏移；
        翻页锁定同一快照，因此并发写入不会造成重复或遗漏。
        """
        limit = max(1, min(limit, 200))
        if cursor is not None:
            token = self._decode_cursor(cursor)
            if token.get("t") != tag or token.get("w") != owner:
                raise CatalogError(
                    RejectReason.INVALID_CURSOR,
                    "cursor does not match the supplied filters",
                    cursor_tag=token.get("t"),
                    requested_tag=tag,
                )
            version, offset = token["v"], token["o"]
        else:
            version = snapshot_version if snapshot_version is not None else self.current_snapshot().version
            offset = 0

        snap = self.snapshot_at(version)
        visible = self._visible_entity_ids(snap, user)
        matched = sorted(
            (
                e
                for e in snap.entities.values()
                if e.id in visible
                and (tag is None or tag in e.tags)
                and (owner is None or e.owner == owner)
            ),
            key=lambda e: e.id,
        )

        window = matched[offset:offset + limit]
        next_offset = offset + len(window)
        next_cursor = (
            self._encode_cursor(version, next_offset, tag, owner)
            if next_offset < len(matched)
            else None
        )
        return Page(
            items=tuple(window),
            next_cursor=next_cursor,
            snapshot_version=version,
        )
