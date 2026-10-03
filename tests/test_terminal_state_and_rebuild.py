"""终态语义与撤销后重建的校验边界：

1. 撤销列级血缘 -> 删除对应列 -> 按原五元组重建：必须以与首次建边
   相同的 ``column_not_found`` 拒绝，任何快照不得出现指向已不存在
   列的活跃血缘；
2. 退役是终态：已退役实体不可被授权（``entity_retired``），且在
   可见实体集合、发现结果、可见血缘等所有入口统一消失；
3. 拒绝优先级固定：无权限用户查询退役实体只得 ``permission_denied``
   （不泄露"存在过"），有权用户才看到 ``entity_retired``，
   与"从未登记"的 ``not_found`` 始终可区分；
4. 撤销/删列/重建/退役并发交错：任何快照都无悬空引用。
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from metadata_catalog.errors import (
    ConflictError,
    LineageError,
    NotFoundError,
    PermissionDeniedError,
    RetiredError,
    SchemaEvolutionError,
)
from metadata_catalog.models import (
    EntityKind,
    FieldSpec,
    FieldType,
    LineageLevel,
    User,
)

from .conftest import log_judgment


def _table(svc, eid: str, owner: str = "alice", tags=None):
    return svc.catalog.register_entity(
        eid, eid.upper(), EntityKind.TABLE,
        [FieldSpec("a", FieldType.STRING),
         FieldSpec("b", FieldType.INTEGER)],
        tags, owner,
    )


def _assert_no_dangling(svc, snap: int) -> None:
    """判定：快照 snap 下不存在指向已不存在实体/列或已退役实体的活跃边。"""
    view = svc.store.view_at(snap)
    for edge in view.edges:
        for eid, col in ((edge.source_entity, edge.source_column),
                         (edge.target_entity, edge.target_column)):
            assert eid in view.entities, \
                f"snapshot {snap}: edge {edge.id} -> missing entity {eid}"
            assert eid not in view.retired_entities, \
                f"snapshot {snap}: edge {edge.id} -> retired entity {eid}"
            if edge.level is LineageLevel.COLUMN:
                assert col in view.entities[eid].field_map(), \
                    f"snapshot {snap}: edge {edge.id} -> missing column " \
                    f"{eid}.{col}"


# ---------- 1. 撤销 -> 删列 -> 重建被拒 ----------

def test_recreate_after_column_drop_rejected_like_first_time(svc):
    _table(svc, "t1")
    _table(svc, "t2")
    lg = svc.lineage

    # 基线：首次对不存在的列建边 => column_not_found
    with pytest.raises(LineageError) as exc_first:
        lg.add_lineage("t1", "t2", LineageLevel.COLUMN, "a", "ghost")
    assert exc_first.value.reason == "column_not_found"

    lg.add_lineage("t1", "t2", LineageLevel.COLUMN, "a", "a")
    lg.revoke_lineage("t1", "t2", LineageLevel.COLUMN, "a", "a")
    # 血缘已撤销，删列被允许
    svc.catalog.drop_field("t2", "a")
    snap_after_drop = svc.store.current_snapshot()

    # 按原五元组重建：必须与首次遇到列不存在时完全一致的理由
    with pytest.raises(LineageError) as exc_rebuild:
        lg.add_lineage("t1", "t2", LineageLevel.COLUMN, "a", "a")
    assert exc_rebuild.value.reason == "column_not_found"
    assert exc_rebuild.value.reason == exc_first.value.reason

    # 拒绝不得产生副作用：边在当前快照仍不活跃
    edge_id = "edge:COLUMN:t1.a->t2.a"
    assert not svc.store.edge_active_at(edge_id, snap_after_drop)
    assert not svc.store.edge_active_at(
        edge_id, svc.store.current_snapshot())
    # 下游影响分析不得把已删除的列当作有效结果返回
    impact = lg.impact_analysis("t1", "downstream")
    assert impact["nodes"] == []
    assert lg.downstream("t1", "a") == []
    # 全部历史快照逐一核验：无任何悬空引用
    for snap in svc.store._snapshots:
        _assert_no_dangling(svc, snap)
    log_judgment("revoke-drop-recreate-rejected",
                 {"edge": edge_id, "dropped_column": "t2.a",
                  "first_reason": exc_first.value.reason,
                  "rebuild_reason": exc_rebuild.value.reason},
                 snap_after_drop, "REJECTED:column_not_found",
                 "rebuild revalidates endpoints; reason identical to "
                 "first-time column_not_found; no dangling edge in any "
                 "snapshot")


def test_recreate_after_source_column_drop_also_rejected(svc):
    _table(svc, "t1")
    _table(svc, "t2")
    lg = svc.lineage
    lg.add_lineage("t1", "t2", LineageLevel.COLUMN, "b", "b")
    lg.revoke_lineage("t1", "t2", LineageLevel.COLUMN, "b", "b")
    svc.catalog.drop_field("t1", "b")
    with pytest.raises(LineageError) as exc_info:
        lg.add_lineage("t1", "t2", LineageLevel.COLUMN, "b", "b")
    assert exc_info.value.reason == "column_not_found"
    for snap in svc.store._snapshots:
        _assert_no_dangling(svc, snap)
    log_judgment("revoke-drop-source-recreate-rejected",
                 {"edge": "edge:COLUMN:t1.b->t2.b",
                  "dropped_column": "t1.b"},
                 svc.store.current_snapshot(), "REJECTED:column_not_found",
                 "source-side column drop also blocks rebuild")


def test_recreate_still_works_when_column_kept(svc):
    """对照组：列未被删除时，撤销后重建依旧确定生效（语义未收紧过度）。"""
    _table(svc, "t1")
    _table(svc, "t2")
    lg = svc.lineage
    lg.add_lineage("t1", "t2", LineageLevel.COLUMN, "a", "a")
    lg.revoke_lineage("t1", "t2", LineageLevel.COLUMN, "a", "a")
    edge, created = lg.add_lineage("t1", "t2", LineageLevel.COLUMN, "a", "a")
    assert created is True
    snap = svc.store.current_snapshot()
    assert svc.store.edge_active_at(edge.id, snap)
    assert [n.entity_id for n in lg.downstream("t1", "a")] == ["t2"]
    log_judgment("recreate-with-column-kept",
                 {"edge": edge.id}, snap, "RECREATE_OK",
                 "rebuild still revives when endpoints intact")


# ---------- 2. 退役终态：不可授权、全入口不可见 ----------

def test_retired_entity_cannot_be_granted(svc):
    _table(svc, "t1")
    svc.catalog.retire_entity("t1")
    with pytest.raises(RetiredError) as exc_info:
        svc.visibility.grant("bob", "t1")
    assert exc_info.value.reason == "entity_retired"
    # 授权被拒绝后，被授权人依旧看不到该实体
    assert "t1" not in svc.visibility.visible_entities(User("bob"))
    log_judgment("grant-on-retired-rejected",
                 {"entity": "t1", "grantee": "bob"},
                 svc.store.current_snapshot(), "REJECTED:entity_retired",
                 "retirement is terminal; grant cannot resurrect visibility")


def test_retired_entity_absent_from_every_visibility_entry(svc):
    """退役前已持有的授权也不能让退役实体重新出现在任何入口。"""
    _table(svc, "t1", tags={"pii"})
    _table(svc, "t2", tags={"pii"})
    lg = svc.lineage
    lg.add_lineage("t1", "t2", LineageLevel.TABLE)
    svc.visibility.grant("bob", "t1")
    svc.visibility.grant("bob", "t2")
    bob = User("bob")
    admin = User("root", is_admin=True)
    snap_before = svc.store.current_snapshot()
    assert "t1" in svc.visibility.visible_entities(bob, snap_before)

    # 退役要求先撤销血缘
    lg.revoke_lineage("t1", "t2", LineageLevel.TABLE)
    svc.catalog.retire_entity("t1")

    # 入口一：可见实体集合（普通用户与管理员 alike）
    assert "t1" not in svc.visibility.visible_entities(bob)
    assert "t1" not in svc.visibility.visible_entities(admin)
    assert "t2" in svc.visibility.visible_entities(bob)
    # 入口二：发现结果（按标签与按归属）
    assert "t1" not in {e.id for e in svc.discovery.query(bob)["items"]}
    assert "t1" not in {e.id for e in svc.discovery.query(admin,
                                                        tags={"pii"})["items"]}
    # 入口三：可见血缘（另建一条活跃边 t2->t3 作对照，
    # 任何触及 t1 的边都不得出现）
    _table(svc, "t3", tags={"pii"})
    svc.visibility.grant("bob", "t3")
    lg.add_lineage("t2", "t3", LineageLevel.TABLE)
    edges_bob = svc.visibility.visible_edges(bob)
    assert [e.id for e in edges_bob] == ["edge:TABLE:t2->t3"]
    assert all(e.source_entity != "t1" and e.target_entity != "t1"
               for e in svc.visibility.visible_edges(admin))
    # 历史快照不受影响：退役前 bob 确实可见 t1
    assert "t1" in svc.visibility.visible_entities(bob, snap_before)
    log_judgment("retired-absent-everywhere",
                 {"retired": "t1", "entries": ["visible_entities",
                                               "discovery", "visible_edges"],
                  "history_snapshot": snap_before},
                 svc.store.current_snapshot(), "TERMINAL",
                 "retired entity excluded centrally; prior grants do not "
                 "resurrect it; history replays intact")


# ---------- 3. 拒绝优先级：先权限，后退役 ----------

def test_retired_rejection_priority_permission_before_retired(svc):
    _table(svc, "t1", owner="alice")
    svc.visibility.grant("carol", "t1")
    svc.catalog.retire_entity("t1")

    stranger = User("bob")       # 从未被授权
    owner = User("alice")
    granted = User("carol")      # 退役前已被授权
    admin = User("root", is_admin=True)

    # 无权用户：与"存在但未退役且无权限"完全相同的拒绝，不泄露存在过
    _table(svc, "t2", owner="alice")
    with pytest.raises(PermissionDeniedError) as exc_retired_denied:
        svc.visibility.require_read(stranger, "t1")
    with pytest.raises(PermissionDeniedError) as exc_active_denied:
        svc.visibility.require_read(stranger, "t2")
    assert exc_retired_denied.value.reason == \
        exc_active_denied.value.reason == "permission_denied"
    assert type(exc_retired_denied.value) is type(exc_active_denied.value)

    # 有权用户（owner / 退役前被授权者 / 管理员）：看到 entity_retired
    for user in (owner, granted, admin):
        with pytest.raises(RetiredError) as exc_info:
            svc.visibility.require_read(user, "t1")
        assert exc_info.value.reason == "entity_retired"

    # "从未登记"依旧是独立的第三类理由
    with pytest.raises(NotFoundError) as exc_missing:
        svc.visibility.require_read(stranger, "ghost")
    assert exc_missing.value.reason == "not_found"
    assert len({"permission_denied", "entity_retired", "not_found"}) == 3
    log_judgment("retired-rejection-priority",
                 {"retired": "t1", "stranger": "permission_denied",
                  "owner/granted/admin": "entity_retired",
                  "ghost": "not_found"},
                 svc.store.current_snapshot(), "PRIORITY_FIXED",
                 "permission checked before retirement; no existence leak; "
                 "three reasons pairwise distinct")


# ---------- 4. 并发交错：撤销/删列/重建/退役 ----------

def test_concurrent_revoke_drop_recreate_retire_no_dangling(svc):
    """撤销、删列、重建、退役并发交错：任何快照都不得出现
    指向已不存在实体/列或已退役实体的活跃血缘。"""
    n = 8
    for i in range(n):
        _table(svc, f"c{i:02d}")
    lg = svc.lineage
    stop = threading.Event()
    violations: list[str] = []

    def writer(seed: int) -> None:
        i = seed
        while not stop.is_set():
            a, b = f"c{i % n:02d}", f"c{(i + 1) % n:02d}"
            op = i % 5
            try:
                if op == 0:
                    lg.add_lineage(a, b, LineageLevel.COLUMN, "a", "a")
                elif op == 1:
                    lg.revoke_lineage(a, b, LineageLevel.COLUMN, "a", "a")
                elif op == 2:
                    svc.catalog.drop_field(b, "a")
                elif op == 3:
                    svc.catalog.retire_entity(b)
                else:
                    lg.impact_analysis(a, "downstream")
            except (ConflictError, RetiredError, NotFoundError,
                    LineageError, SchemaEvolutionError):
                pass  # 校验拒绝是预期行为；关键是拒绝后状态仍一致
            i += 1

    def reader() -> None:
        while not stop.is_set():
            snap = svc.store.current_snapshot()
            view = svc.store.view_at(snap)
            for edge in view.edges:
                for eid, col in ((edge.source_entity, edge.source_column),
                                 (edge.target_entity, edge.target_column)):
                    if eid not in view.entities:
                        violations.append(
                            f"snapshot {snap}: edge {edge.id} -> "
                            f"missing entity {eid}")
                    elif eid in view.retired_entities:
                        violations.append(
                            f"snapshot {snap}: edge {edge.id} -> "
                            f"retired entity {eid}")
                    elif edge.level is LineageLevel.COLUMN and \
                            col not in view.entities[eid].field_map():
                        violations.append(
                            f"snapshot {snap}: edge {edge.id} -> "
                            f"missing column {eid}.{col}")

    with ThreadPoolExecutor(max_workers=6) as pool:
        wfutures = [pool.submit(writer, s) for s in range(3)]
        rfutures = [pool.submit(reader) for _ in range(3)]
        stop.wait(0.6)
        stop.set()
        for f in wfutures + rfutures:
            f.result()

    # 压测结束后再逐一核验所有已提交快照
    for snap in svc.store._snapshots:
        view = svc.store.view_at(snap)
        for edge in view.edges:
            for eid, col in ((edge.source_entity, edge.source_column),
                             (edge.target_entity, edge.target_column)):
                ok = (
                    eid in view.entities
                    and eid not in view.retired_entities
                    and (edge.level is not LineageLevel.COLUMN
                         or col in view.entities[eid].field_map())
                )
                if not ok:
                    violations.append(
                        f"snapshot {snap}: dangling edge {edge.id}")

    log_judgment("concurrent-revoke-drop-recreate-retire",
                 {"writers": 3, "readers": 3,
                  "ops": ["add_column_lineage", "revoke", "drop_field",
                          "retire", "impact_analysis"],
                  "violations": len(violations)},
                 svc.store.current_snapshot(),
                 "ATOMIC" if not violations else "CORRUPTED",
                 "no snapshot shows active edge to missing/retired entity "
                 "or dropped column")
    assert not violations, violations
