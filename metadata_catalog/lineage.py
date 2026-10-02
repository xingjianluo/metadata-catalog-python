"""血缘建立、撤销、展开与影响分析。

- 表级边：source_entity -> target_entity（列名为 None）。
- 列级边：source_entity.column -> target_entity.column，
  两端实体与列必须存在，且级别与列参数必须匹配，否则以可区分原因拒绝。
- 重复登记同一五元组且边仍活跃：幂等返回（created=False）。
- 撤销（``revoke_lineage``）只追加生命周期事件：新快照不再参与展开，
  历史快照结论逐位保留；重复撤销幂等。撤销后按同一五元组重建
  **确定生效**（自新快照起再次活跃），每个快照各有确定答案。
- 已退役实体不可作为建边端点（``entity_retired``），也不参与展开。
- 向上游/下游展开与影响分析都基于固定快照视图，结果可复现；
  邻居按 (实体 id, 列名) 排序后遍历，保证顺序稳定。
- 循环引用不会被静默截断：遍历时用路径栈检测回到祖先的边，
  在结果中以 ``cycle=True`` 与 ``cycle_path`` 显式标记。
"""
from __future__ import annotations

from dataclasses import dataclass

from .errors import LineageError, NotFoundError, RetiredError
from .models import LineageEdge, LineageLevel
from .store import InMemoryStore, SnapshotView


@dataclass(frozen=True)
class NodeRef:
    entity_id: str
    column: str | None = None


@dataclass(frozen=True)
class ImpactNode:
    """影响分析结果中的一个可达节点。"""

    node: NodeRef
    depth: int
    path: tuple[NodeRef, ...]
    via_edge_id: str | None
    cycle: bool = False
    cycle_path: tuple[NodeRef, ...] = ()


class LineageService:
    def __init__(self, store: InMemoryStore, visibility=None) -> None:
        self.store = store
        self._visibility = visibility

    # ---------- 建立 ----------
    def add_lineage(
        self,
        source_entity: str,
        target_entity: str,
        level: LineageLevel,
        source_column: str | None = None,
        target_column: str | None = None,
        transformation: str = "IDENTITY",
    ) -> tuple[LineageEdge, bool]:
        if not isinstance(level, LineageLevel):
            raise LineageError(f"invalid lineage level: {level!r}",
                               reason="invalid_level")
        if level is LineageLevel.TABLE:
            if source_column is not None or target_column is not None:
                raise LineageError(
                    "table-level lineage must not carry column names",
                    reason="level_column_mismatch",
                )
        else:
            if not source_column or not target_column:
                raise LineageError(
                    "column-level lineage requires both source_column "
                    "and target_column",
                    reason="level_column_mismatch",
                )
        if source_entity == target_entity and source_column == target_column:
            raise LineageError(
                f"self lineage is not allowed: {source_entity!r}",
                reason="self_lineage",
            )

        edge_id = LineageEdge.make_id(
            level, source_entity, source_column, target_entity, target_column
        )
        with self.store.transaction():
            snap = self.store.current_snapshot()
            view = self.store.view_at(snap)
            defined = self.store.edge_definition(edge_id)
            if defined is not None:
                if self.store.edge_active_at(edge_id, snap):
                    # 五元组重复且仍活跃：幂等返回，不产生新提交
                    return defined, False
                # 确定且稳定的重建语义：撤销后按同一五元组重建 => 重建生效，
                # 自新快照起再次参与展开；撤销与重建之间的历史快照
                # 仍视其为已撤销，结论一字不变。
                for eid in (source_entity, target_entity):
                    self._require_active_entity(view, eid)
                new_snap = self.store.begin()
                self.store.add_edge(new_snap, defined)
                return defined, True

            for eid, col in ((source_entity, source_column),
                             (target_entity, target_column)):
                entity = self._require_active_entity(view, eid)
                if level is LineageLevel.COLUMN and col not in entity.field_map():
                    raise LineageError(
                        f"cannot create lineage: column {col!r} does not exist "
                        f"on entity {eid!r}",
                        reason="column_not_found",
                    )

            new_snap = self.store.begin()
            edge = LineageEdge(
                id=edge_id,
                level=level,
                source_entity=source_entity,
                source_column=source_column,
                target_entity=target_entity,
                target_column=target_column,
                transformation=transformation,
                created_snapshot=new_snap,
            )
            self.store.add_edge(new_snap, edge)
            return edge, True

    @staticmethod
    def _require_active_entity(view: SnapshotView, entity_id: str):
        """实体不存在 -> not_found；已退役 -> entity_retired（可区分）。"""
        entity = view.entities.get(entity_id)
        if entity is None:
            raise NotFoundError(
                f"cannot create lineage: entity not found: {entity_id!r}"
            )
        if entity_id in view.retired_entities:
            raise RetiredError(
                f"cannot create lineage: entity retired: {entity_id!r}"
            )
        return entity

    # ---------- 撤销 ----------
    def revoke_lineage(
        self,
        source_entity: str,
        target_entity: str,
        level: LineageLevel,
        source_column: str | None = None,
        target_column: str | None = None,
    ) -> tuple[LineageEdge, bool, int]:
        """撤销一条血缘。返回 (边, 本次是否新撤销, 生效快照号)。

        - 边从未登记：``not_found``；
        - 已撤销：幂等返回 (edge, False, 原撤销快照号)，不产生新提交；
        - 撤销只追加生命周期事件，不删定义：撤销前的历史快照结论
          原样保留，撤销后的新快照不再参与上游/下游展开。
        """
        if not isinstance(level, LineageLevel):
            raise LineageError(f"invalid lineage level: {level!r}",
                               reason="invalid_level")
        edge_id = LineageEdge.make_id(
            level, source_entity, source_column, target_entity, target_column
        )
        with self.store.transaction():
            snap = self.store.current_snapshot()
            edge = self.store.edge_definition(edge_id)
            if edge is None:
                raise NotFoundError(f"lineage not found: {edge_id!r}")
            if not self.store.edge_active_at(edge_id, snap):
                revoked_snap = self.store.last_edge_event_snapshot(edge_id)
                return edge, False, revoked_snap
            new_snap = self.store.begin()
            self.store.revoke_edge(new_snap, edge_id)
            return edge, True, new_snap

    # ---------- 邻接构造 ----------
    @staticmethod
    def _adjacency(view: SnapshotView) -> tuple[dict, dict]:
        """返回 (下游邻接, 上游邻接)，邻居列表均按确定性键排序。"""
        downstream: dict[NodeRef, list[tuple[NodeRef, str]]] = {}
        upstream: dict[NodeRef, list[tuple[NodeRef, str]]] = {}
        for e in view.edges:
            src = NodeRef(e.source_entity, e.source_column)
            dst = NodeRef(e.target_entity, e.target_column)
            downstream.setdefault(src, []).append((dst, e.id))
            upstream.setdefault(dst, []).append((src, e.id))
        for table in (downstream, upstream):
            for neighbors in table.values():
                neighbors.sort(key=lambda item: (item[0].entity_id,
                                                 item[0].column or ""))
        return downstream, upstream

    def _direct(self, node: NodeRef, neighbors_of: str,
                snapshot_id: int | None) -> list[NodeRef]:
        snap = self.store.current_snapshot() if snapshot_id is None \
            else snapshot_id
        view = self.store.view_at(snap)
        if node.entity_id not in view.entities:
            raise NotFoundError(f"entity not found: {node.entity_id!r}")
        if node.entity_id in view.retired_entities:
            # 已退役实体不再作为可用的上游/下游参与展开
            raise RetiredError(f"entity retired: {node.entity_id!r}")
        downstream, upstream = self._adjacency(view)
        table = downstream if neighbors_of == "down" else upstream
        return [n for n, _ in table.get(node, [])
                if n.entity_id not in view.retired_entities]

    def upstream(self, entity_id: str, column: str | None = None,
                 snapshot_id: int | None = None) -> list[NodeRef]:
        return self._direct(NodeRef(entity_id, column), "up", snapshot_id)

    def downstream(self, entity_id: str, column: str | None = None,
                   snapshot_id: int | None = None) -> list[NodeRef]:
        return self._direct(NodeRef(entity_id, column), "down", snapshot_id)

    # ---------- 影响分析 ----------
    def impact_analysis(self, entity_id: str, direction: str = "downstream",
                        column: str | None = None,
                        snapshot_id: int | None = None) -> dict:
        if direction not in ("upstream", "downstream"):
            raise LineageError(
                f"direction must be upstream/downstream, got {direction!r}",
                reason="invalid_direction",
            )
        snap = self.store.current_snapshot() if snapshot_id is None \
            else snapshot_id
        view = self.store.view_at(snap)
        if entity_id not in view.entities:
            raise NotFoundError(f"entity not found: {entity_id!r}")
        if entity_id in view.retired_entities:
            raise RetiredError(f"entity retired: {entity_id!r}")
        if column is not None and column not in view.entities[entity_id].field_map():
            raise NotFoundError(
                f"column {column!r} not found on entity {entity_id!r}"
            )
        return self._impact_in_scope(
            snap, entity_id, direction, column, dict(view.entities)
        )

    def scoped_impact_analysis(
        self,
        user,
        entity_id: str,
        direction: str = "downstream",
        column: str | None = None,
        snapshot_id: int | None = None,
    ) -> dict:
        """带可见性校验与裁剪的影响分析。

        - 起点实体无权限或不存在：分别抛 permission_denied / not_found；
        - 仅沿"两端均对该用户可见"的边展开，因此结果不会泄露无权实体；
        - 同样在固定快照上计算，顺序稳定、可复现。
        """
        from .visibility import VisibilityService  # 避免顶层循环依赖

        if not isinstance(self._visibility, VisibilityService):
            raise LineageError("visibility service not wired",
                               reason="visibility_unavailable")
        snap = self._visibility.require_read(user, entity_id, snapshot_id)
        return self._impact_in_scope(
            snap, entity_id, direction, column,
            self._visibility.visible_entities(user, snap),
        )

    def _impact_in_scope(self, snap: int, entity_id: str, direction: str,
                         column: str | None, visible_entities: dict) -> dict:
        if direction not in ("upstream", "downstream"):
            raise LineageError(
                f"direction must be upstream/downstream, got {direction!r}",
                reason="invalid_direction",
            )
        view = self.store.view_at(snap)
        if entity_id not in visible_entities:
            # 到达这里只可能是起点实体在该快照不存在
            raise NotFoundError(f"entity not found: {entity_id!r}")
        if entity_id in view.retired_entities:
            raise RetiredError(f"entity retired: {entity_id!r}")
        if column is not None and \
                column not in visible_entities[entity_id].field_map():
            raise NotFoundError(
                f"column {column!r} not found on entity {entity_id!r}"
            )
        downstream, upstream = self._adjacency(view)
        full = downstream if direction == "downstream" else upstream
        # 仅保留两端均可见、且均未退役的边
        retired = view.retired_entities
        scoped: dict[NodeRef, list[tuple[NodeRef, str]]] = {}
        for src, neighbors in full.items():
            if src.entity_id not in visible_entities \
                    or src.entity_id in retired:
                continue
            kept = [(n, eid) for n, eid in neighbors
                    if n.entity_id in visible_entities
                    and n.entity_id not in retired]
            if kept:
                scoped[src] = kept

        start = NodeRef(entity_id, column)
        results: list[ImpactNode] = []
        emitted: set[NodeRef] = set()

        def walk(node: NodeRef, depth: int,
                 path: tuple[NodeRef, ...]) -> None:
            for nxt, edge_id in scoped.get(node, []):
                new_path = path + (nxt,)
                if nxt in path:
                    results.append(ImpactNode(
                        node=nxt, depth=depth + 1, path=new_path,
                        via_edge_id=edge_id, cycle=True,
                        cycle_path=new_path[path.index(nxt):],
                    ))
                    continue
                if nxt not in emitted:
                    emitted.add(nxt)
                    results.append(ImpactNode(
                        node=nxt, depth=depth + 1, path=new_path,
                        via_edge_id=edge_id,
                    ))
                walk(nxt, depth + 1, new_path)

        walk(start, 0, (start,))
        return {
            "snapshot_id": snap,
            "direction": direction,
            "start": {"entity_id": entity_id, "column": column},
            "cycles_detected": [r for r in results if r.cycle],
            "nodes": [r for r in results if not r.cycle],
            "all_hits": results,
        }
