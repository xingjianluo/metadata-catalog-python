"""退役与撤销：幂等性、可区分拒绝理由、历史快照兼容、并发交错。"""
from __future__ import annotations

import threading

import pytest

from metadata_catalog.errors import (
    EntityRetiredError,
    NotFoundError,
    PermissionDeniedError,
    RetirementConflictError,
)
from metadata_catalog.models import EntityKind, FieldSpec, FieldType, LineageLevel, User

from .conftest import log_judgment

ADMIN = User("root", is_admin=True)


def _register(svc, entity_id: str, owner: str = "alice",
              tags: set[str] | None = None) -> None:
    svc.catalog.register_entity(
        entity_id, entity_id.upper(), EntityKind.TABLE,
        [FieldSpec("a", FieldType.STRING)], tags, owner,
    )


# ---------- 实体退役 ----------

def test_retire_is_idempotent_and_removes_from_discovery(svc):
    """退役幂等：重复退役不报错、不产生新版本；退役后退出标签/归属发现。"""
    c, d = svc.catalog, svc.discovery
    _register(svc, "t1", tags={"pii"})
    _register(svc, "t2", tags={"pii"})

    retired, changed1 = c.retire_entity("t1")
    snap_after_retire = svc.store.current_snapshot()
    assert changed1 is True and retired.retired
    assert retired.retired_snapshot == snap_after_retire

    again, changed2 = c.retire_entity("t1")
    assert changed2 is False, "repeat retire must be a no-op"
    assert again == retired, "repeat retire must not alter state"
    assert svc.store.current_snapshot() == snap_after_retire, \
        "idempotent retire must not commit a new snapshot"

    page = d.query(ADMIN, tags={"pii"})
    ids = [e.id for e in page["items"]]
    assert ids == ["t2"], "retired entity must leave tag discovery"
    page_by_owner = d.query(ADMIN, owner="alice")
    assert [e.id for e in page_by_owner["items"]] == ["t2"], \
        "retired entity must leave owner discovery"
    log_judgment(
        "retire-idempotent-discovery",
        {"retired": "t1", "second_call_changed": changed2},
        snap_after_retire, "RETIRED+IDEMPOTENT",
        "second retire returned changed=False with identical state and no "
        "new snapshot; discovery by tag/owner only lists t2",
    )


def test_retired_read_reasons_are_distinguishable(svc):
    """查已退役实体：entity_retired，区别于 not_found 与 permission_denied。"""
    c, v = svc.catalog, svc.visibility
    _register(svc, "gone", owner="alice")
    _register(svc, "private", owner="alice")
    c.retire_entity("gone")
    bob = User("bob")

    with pytest.raises(EntityRetiredError) as retired_exc:
        v.require_read(ADMIN, "gone")
    with pytest.raises(NotFoundError) as missing_exc:
        v.require_read(ADMIN, "never-registered")
    with pytest.raises(PermissionDeniedError) as denied_exc:
        v.require_read(bob, "private")

    reasons = {retired_exc.value.reason, missing_exc.value.reason,
               denied_exc.value.reason}
    assert reasons == {"entity_retired", "not_found", "permission_denied"}
    # 无权用户查已退役实体：仍只能得到 permission_denied，不能探测退役状态
    with pytest.raises(PermissionDeniedError):
        v.require_read(bob, "gone")
    log_judgment(
        "retired-read-reasons",
        {"retired": "gone", "missing": "never-registered",
         "denied": ("bob", "private")},
        svc.store.current_snapshot(), "DISTINGUISHABLE",
        "reasons are entity_retired / not_found / permission_denied; "
        "unauthorized user still gets permission_denied on retired entity",
    )


def test_retire_blocked_by_active_lineage_then_allowed_after_revoke(svc):
    """挂着未撤销血缘的实体退役必须拒绝；撤销血缘后退役放行。"""
    c, lg = svc.catalog, svc.lineage
    _register(svc, "up")
    _register(svc, "down")
    lg.add_lineage("up", "down", LineageLevel.TABLE)

    with pytest.raises(RetirementConflictError) as exc:
        c.retire_entity("down")
    assert exc.value.reason == "entity_in_use_by_lineage"
    # 拒绝不得改变状态：实体仍在役、边仍生效
    assert not c.get_entity("down").retired
    assert lg.downstream("up") and lg.downstream("up")[0].entity_id == "down"

    _, revoked = lg.revoke_lineage("up", "down", LineageLevel.TABLE)
    assert revoked is True
    _, changed = c.retire_entity("down")
    assert changed is True
    log_judgment(
        "retire-blocked-by-lineage",
        {"entity": "down", "blocking_edge": "edge:TABLE:up->down"},
        svc.store.current_snapshot(), "REJECTED-THEN-RETIRED",
        "retire with active lineage raised entity_in_use_by_lineage and "
        "left state untouched; after revoke, retire succeeded",
    )


def test_retired_entity_excluded_from_lineage_expansion(svc):
    """退役后不再作为可用的上游/下游参与展开；历史快照展开结论不变。"""
    c, lg = svc.catalog, svc.lineage
    for eid in ("a", "b", "c"):
        _register(svc, eid)
    lg.add_lineage("a", "b", LineageLevel.TABLE)
    lg.add_lineage("b", "c", LineageLevel.TABLE)
    snap_before = svc.store.current_snapshot()
    before = lg.impact_analysis("a", "downstream", snapshot_id=snap_before)
    assert [n.node.entity_id for n in before["nodes"]] == ["b", "c"]

    # 撤销 b->c 后退役 c；a 的下游应只剩 b
    lg.revoke_lineage("b", "c", LineageLevel.TABLE)
    c.retire_entity("c")
    after = lg.impact_analysis("a", "downstream")
    assert [n.node.entity_id for n in after["nodes"]] == ["b"], \
        "retired entity must not participate in expansion"
    assert lg.upstream("b") == lg.upstream("b", snapshot_id=None)  # 稳定
    with pytest.raises(EntityRetiredError):
        lg.impact_analysis("c", "downstream")

    # 历史快照逐位复现：退役/撤销前的结论一个字都不变
    replay = lg.impact_analysis("a", "downstream", snapshot_id=snap_before)
    sig = lambda r: [(n.node.entity_id, n.depth, n.via_edge_id)
                     for n in r["all_hits"]]
    assert sig(replay) == sig(before)
    log_judgment(
        "retire-expansion-history",
        {"start": "a", "snap_before": snap_before},
        svc.store.current_snapshot(), "EXCLUDED+REPRODUCIBLE",
        "current expansion drops retired c; replaying snap_before yields "
        "identical node/depth/edge sequence",
    )


def test_mutations_on_retired_entity_rejected(svc):
    """已退役实体：重登记/加列/建血缘均以 entity_retired 拒绝。"""
    c, lg = svc.catalog, svc.lineage
    _register(svc, "old")
    _register(svc, "peer")
    c.retire_entity("old")

    with pytest.raises(EntityRetiredError):
        c.register_entity("old", "OLD", EntityKind.TABLE,
                          [FieldSpec("a", FieldType.STRING)], None, "alice")
    with pytest.raises(EntityRetiredError):
        c.add_field("old", FieldSpec("b", FieldType.STRING))
    with pytest.raises(EntityRetiredError) as exc:
        lg.add_lineage("peer", "old", LineageLevel.TABLE)
    assert exc.value.reason == "entity_retired"
    # 对照：从未登记的端点仍是 not_found
    with pytest.raises(NotFoundError):
        lg.add_lineage("peer", "ghost", LineageLevel.TABLE)
    log_judgment(
        "retired-mutations-rejected",
        {"entity": "old"},
        svc.store.current_snapshot(), "REJECTED",
        "re-register/add_field/add_lineage on retired entity all raise "
        "entity_retired; unregistered endpoint still raises not_found",
    )


# ---------- 血缘撤销 ----------

def test_revoke_is_idempotent_and_never_registered_is_not_found(svc):
    """撤销幂等；撤销从未建立的边是 not_found，与"已撤销"可区分。"""
    lg = svc.lineage
    _register(svc, "s")
    _register(svc, "t")
    lg.add_lineage("s", "t", LineageLevel.TABLE)

    edge, changed1 = lg.revoke_lineage("s", "t", LineageLevel.TABLE)
    snap_after = svc.store.current_snapshot()
    assert changed1 is True
    edge2, changed2 = lg.revoke_lineage("s", "t", LineageLevel.TABLE)
    assert changed2 is False and edge2.id == edge.id
    assert svc.store.current_snapshot() == snap_after, \
        "idempotent revoke must not commit a new snapshot"

    with pytest.raises(NotFoundError) as exc:
        lg.revoke_lineage("s", "t", LineageLevel.COLUMN, "a", "a")
    assert exc.value.reason == "not_found"
    log_judgment(
        "revoke-idempotent",
        {"edge": edge.id},
        snap_after, "REVOKED+IDEMPOTENT",
        "second revoke returned changed=False with no new snapshot; "
        "revoking a never-registered edge raised not_found",
    )


def test_revoke_excludes_edge_from_new_analysis_but_keeps_history(svc):
    """撤销后新分析不再经过该边；撤销前快照上的结论逐位复现。"""
    lg = svc.lineage
    for eid in ("x", "y", "z"):
        _register(svc, eid)
    lg.add_lineage("x", "y", LineageLevel.TABLE)
    lg.add_lineage("y", "z", LineageLevel.TABLE)
    snap_before = svc.store.current_snapshot()
    before = lg.impact_analysis("x", "downstream", snapshot_id=snap_before)

    lg.revoke_lineage("y", "z", LineageLevel.TABLE)
    after = lg.impact_analysis("x", "downstream")
    assert [n.node.entity_id for n in after["nodes"]] == ["y"], \
        "revoked edge must leave new impact analysis"

    replay = lg.impact_analysis("x", "downstream", snapshot_id=snap_before)
    sig = lambda r: [(n.node.entity_id, n.depth, n.via_edge_id)
                     for n in r["all_hits"]]
    assert sig(replay) == sig(before), \
        "pre-revocation snapshot must replay bit-identically"
    assert [n.node.entity_id for n in replay["nodes"]] == ["y", "z"]
    log_judgment(
        "revoke-history-compat",
        {"revoked": "edge:TABLE:y->z", "snap_before": snap_before},
        svc.store.current_snapshot(), "HISTORY-PRESERVED",
        "new analysis stops at y; replaying snap_before still reaches z "
        "with identical node/depth/edge sequence",
    )


def test_recreate_after_revoke_is_deterministic(svc):
    """撤销后按同一五元组重建：确定地算重建生效，无摇摆。

    判定：同一快照重复查询结果一致；撤销与重建之间的快照上该边
    确定不存在；重建快照起确定存在。
    """
    lg = svc.lineage
    _register(svc, "m")
    _register(svc, "n")
    edge, created1 = lg.add_lineage("m", "n", LineageLevel.TABLE)
    assert created1 is True
    snap_active = svc.store.current_snapshot()

    lg.revoke_lineage("m", "n", LineageLevel.TABLE)
    snap_revoked = svc.store.current_snapshot()
    assert lg.downstream("m") == [], "edge must be gone after revocation"

    edge2, created2 = lg.add_lineage("m", "n", LineageLevel.TABLE)
    assert created2 is True, "re-create after revoke must take effect"
    assert edge2.id == edge.id and edge2.created_snapshot > snap_revoked
    snap_recreated = svc.store.current_snapshot()

    # 三个快照三种确定状态，且各自重复查询稳定一致
    assert [n.entity_id for n in lg.downstream("m", snapshot_id=snap_active)] == ["n"]
    assert lg.downstream("m", snapshot_id=snap_revoked) == []
    assert [n.entity_id for n in lg.downstream("m", snapshot_id=snap_recreated)] == ["n"]
    for snap, expect in ((snap_active, ["n"]), (snap_revoked, []),
                         (snap_recreated, ["n"])):
        for _ in range(3):
            got = [x.entity_id for x in lg.downstream("m", snapshot_id=snap)]
            assert got == expect, f"snapshot {snap} flipped: {got} != {expect}"
    log_judgment(
        "revoke-recreate-deterministic",
        {"edge": edge.id, "snap_active": snap_active,
         "snap_revoked": snap_revoked, "snap_recreated": snap_recreated},
        snap_recreated, "RECREATE-EFFECTIVE",
        "edge is present/absent/present across the three snapshots; "
        "repeated queries per snapshot never flip",
    )


def test_add_lineage_on_active_edge_stays_idempotent(svc):
    """生效中的边重复登记仍是幂等返回，不会被误判为重建。"""
    lg = svc.lineage
    _register(svc, "p")
    _register(svc, "q")
    edge, created1 = lg.add_lineage("p", "q", LineageLevel.TABLE)
    snap = svc.store.current_snapshot()
    edge2, created2 = lg.add_lineage("p", "q", LineageLevel.TABLE)
    assert created1 is True and created2 is False
    assert edge2.created_snapshot == edge.created_snapshot
    assert svc.store.current_snapshot() == snap, \
        "idempotent re-add must not commit a new snapshot"
    log_judgment(
        "active-edge-idempotent",
        {"edge": edge.id}, snap, "IDEMPOTENT",
        "re-adding an active edge returned created=False, original "
        "created_snapshot, and committed no new snapshot",
    )


# ---------- 退役与血缘并发交错 ----------

def test_concurrent_retire_and_lineage_never_leave_dangling_edges(svc):
    """退役、撤销、建边并发交错：

    判定依据：任一快照视图中，生效边的两端实体在该快照必须仍在役
    （无悬空引用）；退役与建边冲突时，失败方的原因码必须可区分
    （entity_in_use_by_lineage / entity_retired），不允许静默成功。
    """
    c, lg = svc.catalog, svc.lineage
    for i in range(6):
        _register(svc, f"e{i}")

    stop = threading.Event()
    violations: list[str] = []
    outcomes: dict[str, int] = {}

    def record(key: str) -> None:
        with lock:
            outcomes[key] = outcomes.get(key, 0) + 1

    lock = threading.Lock()

    def churn_lineage() -> None:
        i = 0
        while not stop.is_set():
            src, dst = f"e{i % 6}", f"e{(i + 1) % 6}"
            try:
                lg.add_lineage(src, dst, LineageLevel.TABLE)
                record("add_ok")
            except EntityRetiredError:
                record("add_rejected_retired")  # 端点已退役：合法拒绝
            try:
                lg.revoke_lineage(src, dst, LineageLevel.TABLE)
                record("revoke_ok")
            except NotFoundError:
                record("revoke_not_found")  # 从未建立：合法拒绝
            i += 1

    def churn_retire() -> None:
        i = 0
        while not stop.is_set():
            try:
                c.retire_entity(f"e{i % 6}")
                record("retire_ok")
            except RetirementConflictError:
                record("retire_rejected_in_use")  # 仍有生效血缘：合法拒绝
            except NotFoundError:
                record("retire_not_found")
            i += 1

    def watcher() -> None:
        while not stop.is_set():
            snap = svc.store.current_snapshot()
            view = svc.store.view_at(snap)
            for edge in view.edges:
                for eid in (edge.source_entity, edge.target_entity):
                    committed = svc.store.get_entity_at(eid, snap)
                    if committed is None or committed.retired:
                        violations.append(
                            f"snapshot {snap}: active edge {edge.id} "
                            f"references retired/missing entity {eid}"
                        )

    threads = [threading.Thread(target=churn_lineage) for _ in range(2)]
    threads += [threading.Thread(target=churn_retire) for _ in range(2)]
    threads += [threading.Thread(target=watcher) for _ in range(2)]
    for t in threads:
        t.start()
    stop.wait(0.6)
    stop.set()
    for t in threads:
        t.join()

    assert not violations, violations
    # 并发压力下系统必须有进展（两类操作都成功过）；具体的冲突拒绝
    # 是否命中取决于时序，其确定性覆盖见下一个用例与
    # test_retire_blocked_by_active_lineage_then_allowed_after_revoke。
    assert outcomes.get("retire_ok", 0) > 0
    assert outcomes.get("add_ok", 0) > 0
    log_judgment(
        "concurrent-retire-lineage",
        {"outcomes": outcomes, "violations": len(violations)},
        svc.store.current_snapshot(),
        "CONSISTENT" if not violations else "CORRUPTED",
        "no snapshot ever showed an active edge referencing a retired "
        "entity; rejections only carried distinguishable reason codes",
    )


def test_retire_vs_add_lineage_conflict_loser_gets_distinguishable_reason(svc):
    """退役与建边正面冲突：无论谁赢，失败方的原因码确定可区分。

    两个操作语义上互斥（建边成功则退役必须被拒绝；退役成功则建边
    必须被拒绝），因此无论时序如何，结果必然落在两种确定形态之一，
    绝不出现双方都成功（悬空边）或双方都失败（死锁/误拒）。
    """
    c, lg = svc.catalog, svc.lineage
    _register(svc, "src")
    _register(svc, "dst")

    barrier = threading.Barrier(2)
    results: dict[str, object] = {}

    def do_retire() -> None:
        barrier.wait()
        try:
            c.retire_entity("dst")
            results["retire"] = "ok"
        except RetirementConflictError as exc:
            results["retire"] = exc.reason

    def do_add() -> None:
        barrier.wait()
        try:
            lg.add_lineage("src", "dst", LineageLevel.TABLE)
            results["add"] = "ok"
        except EntityRetiredError as exc:
            results["add"] = exc.reason

    t1, t2 = threading.Thread(target=do_retire), threading.Thread(target=do_add)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    outcome_a = results == {"retire": "entity_in_use_by_lineage", "add": "ok"}
    outcome_b = results == {"retire": "ok", "add": "entity_retired"}
    assert outcome_a or outcome_b, f"impossible interleaving: {results}"

    # 终态与胜者一致：无论哪种形态，快照里都不存在悬空引用
    view = svc.store.view_at(svc.store.current_snapshot())
    for edge in view.edges:
        assert edge.source_entity in view.entities
        assert edge.target_entity in view.entities
    log_judgment(
        "retire-vs-add-conflict",
        {"results": results},
        svc.store.current_snapshot(),
        "ADD-WON" if outcome_a else "RETIRE-WON",
        "exactly one side committed; the loser failed with "
        "entity_in_use_by_lineage or entity_retired respectively",
    )


def test_retire_then_query_old_snapshot_still_lists_entity(svc):
    """退役前的发现结果（旧快照 + 分页 token）不受退役影响。"""
    c, d = svc.catalog, svc.discovery
    _register(svc, "k1", tags={"keep"})
    _register(svc, "k2", tags={"keep"})
    first = d.query(ADMIN, tags={"keep"}, limit=1)
    snap_before = first["snapshot_id"]
    assert first["next_page_token"] is not None

    c.retire_entity("k1")
    # 旧快照整轮翻页仍看到两个实体：首页显式钉在退役前的快照，
    # 后续页由 token 内嵌的同一快照号接续
    seen, token = [], None
    while True:
        page = d.query(ADMIN, tags={"keep"}, limit=1, page_token=token,
                       snapshot_id=snap_before if token is None else None)
        seen.extend(e.id for e in page["items"])
        token = page["next_page_token"]
        if token is None:
            break
    assert sorted(seen) == ["k1", "k2"], \
        "pagination pinned to pre-retirement snapshot must be unaffected"
    # 新快照下 k1 退出
    now = d.query(ADMIN, tags={"keep"})
    assert [e.id for e in now["items"]] == ["k2"]
    log_judgment(
        "retire-old-snapshot-discovery",
        {"snap_before": snap_before, "seen": sorted(seen)},
        svc.store.current_snapshot(), "HISTORY-PRESERVED",
        "paginating on the pre-retirement snapshot still returns k1,k2; "
        "current snapshot returns only k2",
    )
