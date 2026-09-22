"""线程安全的本地状态存储：实体、血缘与快照视图。

并发模型（MVCC 的本地简化版）：
- 一把可重入锁保证一次"提交"（可能包含一次实体写或一次边写）的原子性，
  并发写入整体串行提交，因此任何快照都不会出现半更新状态。
- 每次提交前由服务层 ``begin()`` 取新快照号；``put_entity/add_edge``
  在该快照号下落盘新的不可变对象。
- ``view_at(s)`` 返回快照 s 下的不可变只读视图（惰性构建并缓存），
  后续写入不会改变既有视图，影响分析因此可复现。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

from .clock import LogicalClock
from .models import Entity, LineageEdge


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
        # 边 id -> 边（边不可变，仅允许幂等重复登记）
        self._edges: dict[str, LineageEdge] = {}
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
        with self._lock:
            self._edges[edge.id] = edge
            self._snapshots.append(snapshot_id)

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
                    entities[entity_id] = versions[snaps[idx]]

            edges = tuple(
                sorted(
                    (e for e in self._edges.values()
                     if e.created_snapshot <= snapshot_id),
                    key=lambda e: e.id,
                )
            )
            view = SnapshotView(snapshot_id, entities, edges)
            self._views[snapshot_id] = view
            return view
