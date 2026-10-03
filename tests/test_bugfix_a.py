"""回归测试（bugfix-a）：

1. 撤销列级血缘 -> 删除对应列 -> 按原五元组重建：必须以与首次建边遇到
   列不存在时相同的原因码（column_not_found）拒绝，且任何快照都不得
   出现指向已不存在实体/列的活跃血缘。
2. 退役是终态：已退役实体不能被授权（entity_retired），且不再出现在
   任何可见性集合与发现结果里（对管理员同样生效），而不是只在某一个
   查询入口被临时过滤。
3. 拒绝优先级固定：not_found -> permission_denied -> entity_retired。
   无权用户查退役实体只能拿到 permission_denied，无法探测实体存在过；
   有权用户才看到 entity_retired，且与 not_found 继续区分。
4. 撤销/删列/重建/退役并发交错：任何快照都无悬空引用（实体级与列级）。
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from metadata_catalog.errors import (
    CatalogError,
    LineageError,
    NotFoundError,
    PermissionDeniedError,
    RetiredError,
)
from metadata_catalog.models import (
    EntityKind,
    FieldSpec,
    FieldType,
    LineageLevel,
    User,
)

from .conftest import log_judgment


def _table(svc, eid: str, owner: str = "alice", tags=None,
           cols=("a", "b")):
    return svc.catalog.register_entity(
        eid, eid.upper(), EntityKind.TABLE,
        [FieldSpec(c, FieldType.STRING) for c in cols],
        tags, owner,
    )


def _assert_no_dangling(view) -> list[str]:
    """快照不变量：活跃边的两端实体必须存在且未退役；
    列级边两端的列必须仍存在于该快照的实体定义里。"""
    problems = []
    for edge in view.edges:
        for eid in (edge.source_entity, edge.target_entity):
            if eid not in view.entities:
                problems.append(f"edge {edge.id} references missing {eid}")
            elif eid in view.retired_entities:
                problems.append(f"edge {edge.id} references retired {eid}")
        if edge.level is LineageLevel.COLUMN:
            for eid, col in ((edge.source_entity, edge.source_column),
                             (edge.target_entity, edge.target_column)):
                entity = view.entities.get(eid)
                if entity is not None and col not in entity.field_map():
                    problems.append(
                        f"edge {edge.id} references dropped column "
                        f"{eid}.{col}"
                    )
    return problems


# ---------- 1. 撤销 -> 删列 -> 重建被拒 ----------

def test_recreate_column_lineage_after_column_drop_rejected(svc):
    c, lg = svc.catalog, svc.lineage
    _table(svc, "s")
    _table(svc, "t")
    lg.add_lineage("s", "t", LineageLevel.COLUMN, "a", "a")
    snap_active = svc.store.current_snapshot()

    lg.revoke_lineage("s", "t", LineageLevel.COLUMN, "a", "a")
    c.drop_field("t", "a")  # 边已撤销，删列被允许
    snap_before_recreate = svc.store.current_snapshot()

    # 重建必须被拒绝，且理由与"首次建边时列不存在"完全一致
    with pytest.raises(LineageError) as exc_recreate:
        lg.add_lineage("s", "t", LineageLevel.COLUMN, "a", "a")
    assert exc_recreate.value.reason == "column_not_found"
    # 拒绝不得产生新提交
    assert svc.store.current_snapshot() == snap_before_recreate
    _table(svc, "s2")
    _table(svc, "t2")
    with pytest.raises(LineageError) as exc_first:
        lg.add_lineage("s2", "t2", LineageLevel.COLUMN, "ghost", "a")
    assert exc_first.value.reason == exc_recreate.value.reason, \
        "重建被拒的理由必须与首次遇到列不存在时一致"

    # 最新快照：不得存在指向已删列的活跃血缘
    view_now = svc.store.view_at(svc.store.current_snapshot())
    assert _assert_no_dangling(view_now) == []
    assert not svc.store.edge_active_at(
        "edge:COLUMN:s.a->t.a", svc.store.current_snapshot())
    # 下游影响分析不得把不存在的列当有效结果返回
    result = lg.impact_analysis("s", "downstream", column="a")
    assert result["nodes"] == []
    # 历史快照（边活跃、列尚在）结论原样复现
    view_then = svc.store.view_at(snap_active)
    assert _assert_no_dangling(view_then) == []
    assert [n.entity_id for n in lg.downstream("s", column="a",
                                               snapshot_id=snap_active)] \
        == ["t"]
    log_judgment("recreate-after-drop-rejected",
                 {"edge": "s.a->t.a", "ops": ["revoke", "drop t.a",
                                              "recreate"]},
                 svc.store.current_snapshot(),
                 f"REJECTED:{exc_recreate.value.reason}",
                 "recreate validated like first create; no dangling "
                 "column edge in any snapshot")


def test_recreate_after_revoke_still_works_when_column_kept(svc):
    """对照组：列未被删除时，撤销后重建仍然确定生效（既有语义不回归）。"""
    lg = svc.lineage
    _table(svc, "s")
    _table(svc, "t")
    lg.add_lineage("s", "t", LineageLevel.COLUMN, "a", "a")
    lg.revoke_lineage("s", "t", LineageLevel.COLUMN, "a", "a")
    edge, created = lg.add_lineage("s", "t", LineageLevel.COLUMN, "a", "a")
    assert created is True
    assert [n.entity_id for n in lg.downstream("s", column="a")] == ["t"]
    log_judgment("recreate-column-kept",
                 {"edge": edge.id}, svc.store.current_snapshot(),
                 "RECREATE_OK", "column intact => recreate still revives")


# ---------- 2. 退役终态：不可授权、不进任何可见性/发现结果 ----------

def test_retired_entity_cannot_be_granted(svc):
    c, v = svc.catalog, svc.visibility
    _table(svc, "t1")
    _, _, retire_snap = c.retire_entity("t1")
    snap_before_grant = svc.store.current_snapshot()
    with pytest.raises(RetiredError) as exc_info:
        v.grant("carol", "t1")
    assert exc_info.value.reason == "entity_retired"
    assert svc.store.current_snapshot() == snap_before_grant, \
        "被拒的授权不得产生新提交"
    log_judgment("grant-on-retired-rejected",
                 {"user": "carol", "entity": "t1",
                  "retire_snapshot": retire_snap},
                 svc.store.current_snapshot(),
                 f"REJECTED:{exc_info.value.reason}",
                 "retirement is terminal; grant must not resurrect it")


def test_retired_entity_absent_from_all_visibility_and_discovery(svc):
    c, v = svc.catalog, svc.visibility
    _table(svc, "t1", tags={"pii"})
    _table(svc, "t2", tags={"pii"})
    v.grant("carol", "t1")  # 退役前已授权：授权记录本身合法存在
    admin, alice, carol = User("root", is_admin=True), User("alice"), User("carol")
    snap_before = svc.store.current_snapshot()
    assert "t1" in v.visible_entities(carol)

    c.retire_entity("t1")

    # 每一个可见性入口都不得再出现 t1：管理员、owner、已被授权者
    for u in (admin, alice, carol):
        assert "t1" not in v.visible_entities(u), \
            f"retired entity leaked into visible set of {u.name}"
        page = svc.discovery.query(u, tags={"pii"})
        assert "t1" not in [e.id for e in page["items"]], \
            f"retired entity leaked into discovery of {u.name}"
    # 对本就能同时看到两者的用户，发现结果恰好只剩 t2
    for u in (admin, alice):
        page = svc.discovery.query(u, tags={"pii"})
        assert [e.id for e in page["items"]] == ["t2"]
    # 可见血缘边同样不含退役实体（先建边再撤销再退役，边定义仍在）
    assert all("t1" not in (e.source_entity, e.target_entity)
               for e in v.visible_edges(admin))
    # 历史快照仍按当时状态复现（carol 当时被授权、t1 当时未退役）
    assert "t1" in v.visible_entities(carol, snapshot_id=snap_before)
    replay = svc.discovery.query(admin, tags={"pii"}, snapshot_id=snap_before)
    assert [e.id for e in replay["items"]] == ["t1", "t2"]
    log_judgment("retired-invisible-everywhere",
                 {"retired": "t1", "users": ["admin", "alice", "carol"],
                  "history_snapshot": snap_before},
                 svc.store.current_snapshot(), "TERMINAL",
                 "retired excluded centrally from visible_entities; "
                 "discovery/edges inherit; history replays intact")


# ---------- 3. 拒绝优先级：无权者不得看到 entity_retired ----------

def test_retired_rejection_priority_pinned(svc):
    c, v = svc.catalog, svc.visibility
    _table(svc, "t1", owner="alice")
    v.grant("carol", "t1")
    c.retire_entity("t1")

    stranger = User("bob")          # 无权
    grantee = User("carol")         # 退役前被授权
    owner = User("alice")
    admin = User("root", is_admin=True)

    # 无权用户：只能得到 permission_denied，与实体是否退役无关
    with pytest.raises(PermissionDeniedError) as exc_denied:
        v.require_read(stranger, "t1")
    assert exc_denied.value.reason == "permission_denied"
    # 有权用户（owner/被授权者/管理员）：才看得到 entity_retired
    for u in (owner, grantee, admin):
        with pytest.raises(RetiredError) as exc_retired:
            v.require_read(u, "t1")
        assert exc_retired.value.reason == "entity_retired"
    # 从未登记：not_found，与 entity_retired 继续区分得开
    with pytest.raises(NotFoundError) as exc_missing:
        v.require_read(owner, "ghost")
    assert exc_missing.value.reason == "not_found"
    assert len({exc_denied.value.reason, exc_retired.value.reason,
                exc_missing.value.reason}) == 3
    # 带权限的影响分析入口遵守同一优先级
    with pytest.raises(PermissionDeniedError):
        svc.lineage.scoped_impact_analysis(stranger, "t1", "downstream")
    with pytest.raises(RetiredError):
        svc.lineage.scoped_impact_analysis(owner, "t1", "downstream")
    log_judgment("retired-rejection-priority",
                 {"stranger": "bob", "authorized": ["alice", "carol",
                                                    "root"]},
                 svc.store.current_snapshot(), "ORDER_PINNED",
                 "not_found -> permission_denied -> entity_retired; "
                 "unauthorized users cannot probe existence via retired")


# ---------- 4. 撤销/删列/重建/退役并发交错：任何快照无悬空引用 ----------

def test_concurrent_revoke_drop_recreate_retire_no_dangling(svc):
    n = 8
    for i in range(n):
        _table(svc, f"x{i:02d}")
    lg, c = svc.lineage, svc.catalog
    stop = threading.Event()
    violations: list[str] = []
    op_counts = {"add": 0, "revoke": 0, "drop": 0, "retire": 0}
    op_lock = threading.Lock()

    def bump(op: str) -> None:
        with op_lock:
            op_counts[op] += 1

    def writer(seed: int) -> None:
        i = seed
        while not stop.is_set():
            a, b = f"x{i % n:02d}", f"x{(i + 1) % n:02d}"
            col = "a" if (i // n) % 2 == 0 else "b"
            op = i % 4
            try:
                if op == 0:  # 建列级边（含撤销后的重建）
                    lg.add_lineage(a, b, LineageLevel.COLUMN, col, col)
                    bump("add")
                elif op == 1:  # 撤销上一轮（i-1）建的那条边
                    j = i - 1
                    pa, pb = f"x{j % n:02d}", f"x{(j + 1) % n:02d}"
                    pcol = "a" if (j // n) % 2 == 0 else "b"
                    lg.revoke_lineage(pa, pb, LineageLevel.COLUMN, pcol, pcol)
                    bump("revoke")
                elif op == 2:  # 删列（仍被活跃血缘引用时会被拒，属预期）
                    c.drop_field(a, col)
                    bump("drop")
                else:  # 退役（只针对末尾两个实体，保证仍有活跃实体
                    # 可供建边/撤销/删列持续交错；仍挂活跃血缘时被拒，属预期）
                    c.retire_entity(f"x{6 + (i % 2):02d}")
                    bump("retire")
            except CatalogError:
                pass  # 冲突方显式失败是预期行为
            i += 1

    def reader() -> None:
        while not stop.is_set():
            snap = svc.store.current_snapshot()
            view = svc.store.view_at(snap)
            for problem in _assert_no_dangling(view):
                violations.append(f"snapshot {snap}: {problem}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        wfutures = [pool.submit(writer, s) for s in range(4)]
        rfutures = [pool.submit(reader) for _ in range(4)]
        stop.wait(0.8)
        stop.set()
        for f in wfutures + rfutures:
            f.result()

    # 四类操作都必须真实执行过，否则对应交错分支未被覆盖
    assert all(count > 0 for count in op_counts.values()), op_counts
    # 终态快照同样不得有悬空引用
    final_view = svc.store.view_at(svc.store.current_snapshot())
    violations.extend(
        f"final: {p}" for p in _assert_no_dangling(final_view))
    log_judgment("concurrent-revoke-drop-recreate-retire",
                 {"writers": 4, "readers": 4, "ops": dict(op_counts),
                  "violations": len(violations)},
                 svc.store.current_snapshot(),
                 "ATOMIC" if not violations else "CORRUPTED",
                 "no snapshot shows active edge to missing/retired entity "
                 "or dropped column")
    assert not violations, violations
