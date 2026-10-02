"""线程安全的本地状态存储：实体、血缘与快照视图。

并发模型（MVCC 的本地简化版）：
- 一把可重入锁保证一次"提交"（可能包含一次实体写或一次边写）的原子性，
  并发写入整体串行提交，因此任何快照都不会出现半更新状态。
- 每次提交前由服务层 ``begin()`` 取新快照号；``put_entity/add_edge``
  在该快照号下落盘新的不可变对象。
- ``view_at(s)`` 返回快照 s 下的不可变只读视图（惰性构建并缓存），
  后续写入不会改变既有视图，影响分析因此可复现。

退役与撤销的存储语义：
- 实体退役是一个带 ``retired_snapshot`` 的新版本；快照视图中只包含
  "在该快照仍在役"的实体，历史快照不受影响。
- 血缘边按"生命周期区间"存放：``[(created_snapshot, revoked_snapshot|None)]``。
  快照 s 下边可见当且仅当 ``created <= s < revoked``（revoked 为 None 表示
  仍生效）。撤销后按同一五元组重建会开启一个**新的区间**，因此任一快照下
  边的存在性都是唯一确定的，不会摇摆。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

from .clock import LogicalClock
from .models import Entity, LineageEdge


@dataclass(frozen=True)
class EdgeLifecycle:
    """一条血缘边的一段生命周期：[created, revoked) 区间内生效。"""

    edge: LineageEdge
    revoked_snapshot: int | None = None

    def active_at(self, snapshot_id: int) -> bool:
        return (
            self.edge.created_snapshot <= snapshot_id
            and (
                self.revoked_snapshot is None
                or snapshot_id < self.revoked_snapshot
            )
        )


@dataclass(frozen=True)
class SnapshotView:
    """某一快照号下的不可变只读视图。

    实体按 id 字典存放；边按确定性顺序（边 id 升序）存放，
    保证同一快照的遍历顺序稳定可复现。
    """

    snapshot_id: int
    entities: dict[str, Entity]
    edges: tuple[LineageEdge, ...]


class InMemoryStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._clock = LogicalClock(start=0)
        # 每个实体的全部已提交版本：entity_id -> {snapshot_id: Entity}
        self._entity_versions: dict[str, dict[int, Entity]] = {}
        # 边 id -> 生命周期区间列表（按 created_snapshot 升序追加；
        # 至多最后一个区间的 revoked_snapshot 为 None）
        self._edge_lifecycles: dict[str, list[EdgeLifecycle]] = {}
        # 授权：user_name -> {entity_id -> 授权生效快照号}
        self._grants: dict[str, dict[str, int]] = {}
        # 所有者读全部：user_name -> 生效快照号
        self._owner_access: dict[str, int] = {}
        # 快照视图缓存：snapshot_id -> SnapshotView
        self._views: dict[int, SnapshotView] = {}
        # 已提交快照号列表（升序）
        self._snapshots: list[int] = [0]
        self._views[0] = SnapshotView(0, {}, ())

    def current_snapshot(self) -> int:
        with self._lock:
            return self._clock.current()

    class _Transaction:
        def __init__(self, lock: threading.RLock) -> None:
            self._lock = lock

        def __enter__(self) -> "InMemoryStore._Transaction":
            self._lock.acquire()
            return self

        def __exit__(self, *exc: object) -> None:
            self._lock.release()

    def transaction(self) -> "_Transaction":
        """串行化一段"检查-判断-写入"，使其整体原子可见。"""
        return InMemoryStore._Transaction(self._lock)

    def begin(self) -> int:
        """分配新快照号（调用方持有锁时也安全，RLock 可重入）。"""
        with self._lock:
            return self._clock.tick()

    def put_entity(self, snapshot_id: int, entity: Entity) -> None:
        with self._lock:
            versions = self._entity_versions.setdefault(entity.id, {})
            versions[snapshot_id] = entity
            self._snapshots.append(snapshot_id)

    def add_edge(self, snapshot_id: int, edge: LineageEdge) -> None:
        """落盘一条新边（开启一段新的生命周期区间）。"""
        with self._lock:
            self._edge_lifecycles.setdefault(edge.id, []).append(
                EdgeLifecycle(edge=edge)
            )
            self._snapshots.append(snapshot_id)

    def revoke_edge(self, snapshot_id: int, edge_id: str) -> None:
        """关闭 edge_id 当前生效的生命周期区间（调用方须先确认其生效）。"""
        with self._lock:
            lifecycles = self._edge_lifecycles[edge_id]
            last = lifecycles[-1]
            assert last.revoked_snapshot is None, "no active lifecycle to close"
            lifecycles[-1] = EdgeLifecycle(
                edge=last.edge, revoked_snapshot=snapshot_id
            )
            self._snapshots.append(snapshot_id)

    def edge_state_at(self, edge_id: str,
                      snapshot_id: int) -> tuple[str, LineageEdge | None]:
        """返回 ("active"|"revoked"|"absent", 边或 None)。

        - active：该快照下有生效区间；
        - revoked：曾经建立过但该快照下所有区间均已关闭；
        - absent：从未建立过。
        """
        with self._lock:
            lifecycles = self._edge_lifecycles.get(edge_id)
            if not lifecycles:
                return "absent", None
            latest: LineageEdge | None = None
            for lc in lifecycles:
                if lc.edge.created_snapshot > snapshot_id:
                    break
                latest = lc.edge
                if lc.active_at(snapshot_id):
                    return "active", lc.edge
            if latest is None:
                return "absent", None
            return "revoked", latest

    def grant(self, snapshot_id: int, user_name: str, entity_id: str) -> None:
        with self._lock:
            self._grants.setdefault(user_name, {})[entity_id] = snapshot_id
            self._snapshots.append(snapshot_id)

    def grant_owner_access(self, snapshot_id: int, user_name: str) -> None:
        with self._lock:
            self._owner_access[user_name] = snapshot_id
            self._snapshots.append(snapshot_id)

    def grants_at(self, snapshot_id: int) -> tuple[dict[str, set[str]], set[str]]:
        """返回该快照下生效的 (逐实体授权, 拥有所有者读权限的用户集合)。"""
        with self._lock:
            per_entity: dict[str, set[str]] = {}
            for user_name, mapping in self._grants.items():
                visible = {eid for eid, s in mapping.items() if s <= snapshot_id}
                if visible:
                    per_entity[user_name] = visible
            owner_readers = {
                u for u, s in self._owner_access.items() if s <= snapshot_id
            }
            return per_entity, owner_readers

    def get_entity_at(self, entity_id: str, snapshot_id: int) -> Entity | None:
        """读取快照 snapshot_id 下该实体的已提交版本（二分最近版本）。"""
        from bisect import bisect_right

        with self._lock:
            versions = self._entity_versions.get(entity_id)
            if not versions:
                return None
            snaps = sorted(versions)
            idx = bisect_right(snaps, snapshot_id) - 1
            if idx < 0:
                return None
            return versions[snaps[idx]]

    def view_at(self, snapshot_id: int | None = None) -> SnapshotView:
        with self._lock:
            if snapshot_id is None:
                snapshot_id = self._clock.current()
            cached = self._views.get(snapshot_id)
            if cached is not None:
                return cached

            from bisect import bisect_right

            committed = sorted(self._snapshots)
            upper = bisect_right(committed, snapshot_id) - 1
            if upper < 0:
                view = SnapshotView(snapshot_id, {}, ())
                self._views[snapshot_id] = view
                return view

            entities: dict[str, Entity] = {}
            for entity_id, versions in self._entity_versions.items():
                snaps = sorted(versions)
                idx = bisect_right(snaps, snapshot_id) - 1
                if idx >= 0:
                    entity = versions[snaps[idx]]
                    # 已退役实体不出现在快照视图中：发现与血缘展开都基于
                    # 视图，因此退役后自动退出发现结果与上/下游展开；
                    # 历史快照（snapshot_id < retired_snapshot）不受影响。
                    if not entity.retired:
                        entities[entity_id] = entity

            edges = tuple(
                sorted(
                    (lc.edge
                     for lifecycles in self._edge_lifecycles.values()
                     for lc in lifecycles
                     if lc.active_at(snapshot_id)),
                    key=lambda e: e.id,
                )
            )
            view = SnapshotView(snapshot_id, entities, edges)
            self._views[snapshot_id] = view
            return view
