"""实体退役与血缘撤销：幂等、可区分拒绝、历史快照不变、并发交错无悬空。"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from metadata_catalog.errors import (
    ConflictError,
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


def _table(svc, eid: str, owner: str = "alice", tags=None):
    return svc.catalog.register_entity(
        eid, eid.upper(), EntityKind.TABLE,
        [FieldSpec("a", FieldType.STRING),
         FieldSpec("b", FieldType.INTEGER)],
        tags, owner,
    )


# ---------- 退役 ----------

def test_retire_is_idempotent(svc):
    _table(svc, "t1")
    e1, retired1, snap1 = svc.catalog.retire_entity("t1")
    snap_after_first = svc.store.current_snapshot()
    e2, retired2, snap2 = svc.catalog.retire_entity("t1")
    assert retired1 is True and retired2 is False
    assert snap1 == snap2, "重复退役不得产生新快照"
    assert svc.store.current_snapshot() == snap_after_first, \
        "幂等退役不得推进时钟"
    assert e1 == e2
    log_judgment("retire-idempotent", {"entity": "t1", "calls": 2},
                 snap1, "RETIRED_IDEMPOTENT",
                 "second retire returns same snapshot, no new commit")


def test_retire_unknown_entity_rejected(svc):
    with pytest.raises(NotFoundError) as exc_info:
        svc.catalog.retire_entity("ghost")
    assert exc_info.value.reason == "not_found"
    log_judgment("retire-unknown", {"entity": "ghost"},
                 svc.store.current_snapshot(),
                 f"REJECTED:{exc_info.value.reason}",
                 "never registered => not_found")


def test_retired_entity_hidden_from_discovery_but_history_kept(svc):
    _table(svc, "t1", tags={"pii"})
    _table(svc, "t2", tags={"pii"})
    admin = User("root", is_admin=True)
    before = svc.discovery.query(admin, tags={"pii"})
    snap_before = before["snapshot_id"]
    assert [e.id for e in before["items"]] == ["t1", "t2"]

    _, _, retire_snap = svc.catalog.retire_entity("t1")

    after = svc.discovery.query(admin, tags={"pii"})
    assert [e.id for e in after["items"]] == ["t2"], \
        "退役实体不得出现在按标签的发现结果里"
    by_owner = svc.discovery.query(admin, owner="alice")
    assert [e.id for e in by_owner["items"]] == ["t2"], \
        "退役实体不得出现在按归属的发现结果里"
    replay = svc.discovery.query(admin, tags={"pii"}, snapshot_id=snap_before)
    assert [e.id for e in replay["items"]] == ["t1", "t2"], \
        "退役前的历史快照必须原样复现"
    log_judgment("retire-discovery",
                 {"retired": "t1", "history_snapshot": snap_before},
                 retire_snap, "HIDDEN_HISTORY_INTACT",
                 "retired excluded from new discovery; old snapshot replays")


def test_retired_get_distinguishes_three_rejection_reasons(svc):
    _table(svc, "t1", owner="alice")
    svc.catalog.retire_entity("t1")
    owner = User("alice")
    stranger = User("bob")

    # 1) 已退役 => entity_retired（owner 与陌生人都一样）
    with pytest.raises(RetiredError) as exc_retired:
        svc.visibility.require_read(owner, "t1")
    assert exc_retired.value.reason == "entity_retired"
    with pytest.raises(RetiredError):
        svc.visibility.require_read(stranger, "t1")
    # 2) 从未登记 => not_found
    with pytest.raises(NotFoundError) as exc_missing:
        svc.visibility.require_read(owner, "ghost")
    assert exc_missing.value.reason == "not_found"
    # 3) 存在且未退役但无权限 => permission_denied
    _table(svc, "t2", owner="alice")
    with pytest.raises(PermissionDeniedError) as exc_denied:
        svc.visibility.require_read(stranger, "t2")
    assert exc_denied.value.reason == "permission_denied"
    # 三者原因码两两不同
    reasons = {exc_retired.value.reason, exc_missing.value.reason,
               exc_denied.value.reason}
    assert len(reasons) == 3
    # get_entity 直查同样区分
    with pytest.raises(RetiredError):
        svc.catalog.get_entity("t1")
    with pytest.raises(NotFoundError):
        svc.catalog.get_entity("ghost")
    log_judgment("retire-distinct-reasons",
                 {"retired": "t1", "missing": "ghost", "denied": "t2"},
                 svc.store.current_snapshot(), "DISTINCT",
                 "entity_retired / not_found / permission_denied 三分")


def test_retire_with_active_lineage_rejected_then_allowed_after_revoke(svc):
    _table(svc, "t1")
    _table(svc, "t2")
    svc.lineage.add_lineage("t1", "t2", LineageLevel.TABLE)
    with pytest.raises(ConflictError) as exc_info:
        svc.catalog.retire_entity("t1")
    assert exc_info.value.reason == "entity_has_active_lineage"
    # 拒绝不得改变状态：t1 仍未退役
    assert svc.catalog.get_entity("t1").id == "t1"

    svc.lineage.revoke_lineage("t1", "t2", LineageLevel.TABLE)
    _, retired, snap = svc.catalog.retire_entity("t1")
    assert retired is True
    log_judgment("retire-blocked-by-lineage",
                 {"entity": "t1", "first": exc_info.value.reason},
                 snap, "CONFLICT_THEN_OK",
                 "active lineage blocks retire; after revoke it succeeds")


def test_retired_entity_not_usable_in_expansion(svc):
    for tid in ("t1", "t2", "t3"):
        _table(svc, tid)
    lg = svc.lineage
    lg.add_lineage("t1", "t2", LineageLevel.TABLE)
    lg.add_lineage("t2", "t3", LineageLevel.TABLE)
    lg.revoke_lineage("t2", "t3", LineageLevel.TABLE)
    svc.catalog.retire_entity("t3")

    # 退役实体自身不再可作为展开起点
    with pytest.raises(RetiredError) as exc_up:
        lg.upstream("t3")
    assert exc_up.value.reason == "entity_retired"
    with pytest.raises(RetiredError):
        lg.downstream("t3")
    with pytest.raises(RetiredError):
        lg.impact_analysis("t3", "upstream")
    # 也不会作为邻居出现在别人的展开里（其入边已撤销）
    assert lg.downstream("t2") == []
    result = lg.impact_analysis("t1", "downstream")
    assert [n.node.entity_id for n in result["nodes"]] == ["t2"]
    log_judgment("retire-expansion",
                 {"retired": "t3", "t1_downstream": ["t2"]},
                 svc.store.current_snapshot(), "EXCLUDED",
                 "retired entity rejected as start and absent as neighbor")


def test_retired_entity_cannot_reregister_or_evolve(svc):
    _table(svc, "t1")
    svc.catalog.retire_entity("t1")
    # 同 id 重新登记（即使定义完全一致）一律拒绝：退役是终态
    with pytest.raises(RetiredError) as exc_reg:
        _table(svc, "t1")
    assert exc_reg.value.reason == "entity_retired"
    with pytest.raises(RetiredError):
        svc.catalog.add_field("t1", FieldSpec("c", FieldType.STRING))
    with pytest.raises(RetiredError):
        svc.catalog.drop_field("t1", "a")
    log_judgment("retire-terminal",
                 {"entity": "t1", "ops": ["register", "add_field",
                                          "drop_field"]},
                 svc.store.current_snapshot(), "TERMINAL",
                 "retirement is final; no resurrection via register/evolve")


# ---------- 撤销 ----------

def test_revoke_is_idempotent_and_unknown_rejected(svc):
    _table(svc, "t1")
    _table(svc, "t2")
    svc.lineage.add_lineage("t1", "t2", LineageLevel.TABLE)
    e1, revoked1, snap1 = svc.lineage.revoke_lineage(
        "t1", "t2", LineageLevel.TABLE)
    snap_after_first = svc.store.current_snapshot()
    e2, revoked2, snap2 = svc.lineage.revoke_lineage(
        "t1", "t2", LineageLevel.TABLE)
    assert revoked1 is True and revoked2 is False
    assert snap1 == snap2, "重复撤销不得产生新快照"
    assert svc.store.current_snapshot() == snap_after_first
    assert e1 == e2
    with pytest.raises(NotFoundError) as exc_info:
        svc.lineage.revoke_lineage("t1", "t3", LineageLevel.TABLE)
    assert exc_info.value.reason == "not_found"
    log_judgment("revoke-idempotent",
                 {"edge": e1.id, "unknown": "t1->t3"},
                 snap1, "REVOKED_IDEMPOTENT",
                 "second revoke no-op; unknown edge => not_found")


def test_revoke_excludes_from_new_impact_but_history_unchanged(svc):
    for tid in ("t1", "t2", "t3"):
        _table(svc, tid)
    lg = svc.lineage
    lg.add_lineage("t1", "t2", LineageLevel.TABLE)
    lg.add_lineage("t2", "t3", LineageLevel.TABLE)
    snap_before = svc.store.current_snapshot()
    sig_before = [(n.node.entity_id, n.depth)
                  for n in lg.impact_analysis("t1", "downstream",
                                              snapshot_id=snap_before)["nodes"]]
    assert [e for e, _ in sig_before] == ["t2", "t3"]

    _, _, revoke_snap = lg.revoke_lineage("t2", "t3", LineageLevel.TABLE)

    # 新快照：被撤销的边不再参与展开
    after = lg.impact_analysis("t1", "downstream")
    assert [n.node.entity_id for n in after["nodes"]] == ["t2"]
    assert lg.downstream("t2") == []
    assert lg.upstream("t3") == []
    # 撤销前的历史快照：结论逐位复现，一个字都不变
    replay = lg.impact_analysis("t1", "downstream", snapshot_id=snap_before)
    sig_replay = [(n.node.entity_id, n.depth) for n in replay["nodes"]]
    assert sig_replay == sig_before
    assert [n.entity_id for n in lg.downstream("t2", snapshot_id=snap_before)] \
        == ["t3"]
    log_judgment("revoke-history-intact",
                 {"revoked": "t2->t3", "history_snapshot": snap_before,
                  "before": sig_before, "after": ["t2"]},
                 revoke_snap, "HISTORY_IMMUTABLE",
                 "old snapshot replays byte-identical; new snapshot excludes")


def test_recreate_after_revoke_revives_deterministically(svc):
    """撤销后按同一五元组重建：确定语义 = 重建生效（自新快照起活跃）。

    撤销与重建之间的快照仍视其为已撤销，三种状态各归各的快照，
    不存在"一会儿算存在、一会儿不算"的摇摆。
    """
    _table(svc, "t1")
    _table(svc, "t2")
    lg = svc.lineage
    e1, created1 = lg.add_lineage("t1", "t2", LineageLevel.TABLE)
    snap_created = svc.store.current_snapshot()
    lg.revoke_lineage("t1", "t2", LineageLevel.TABLE)
    snap_revoked = svc.store.current_snapshot()
    e2, created2 = lg.add_lineage("t1", "t2", LineageLevel.TABLE)
    snap_recreated = svc.store.current_snapshot()

    assert created1 is True and created2 is True, "重建是一次新的生效提交"
    assert e1 == e2, "边定义不变（同一五元组）"
    # 三个快照三种确定结论
    assert [n.entity_id for n in lg.downstream("t1", snapshot_id=snap_created)] \
        == ["t2"]
    assert lg.downstream("t1", snapshot_id=snap_revoked) == []
    assert [n.entity_id for n in lg.downstream("t1",
                                               snapshot_id=snap_recreated)] \
        == ["t2"]
    # 再撤销一次，语义依旧稳定
    _, revoked_again, _ = lg.revoke_lineage("t1", "t2", LineageLevel.TABLE)
    assert revoked_again is True
    assert lg.downstream("t1") == []
    assert [n.entity_id for n in lg.downstream("t1", snapshot_id=snap_created)] \
        == ["t2"], "最早快照结论仍不受后续撤销影响"
    log_judgment("revoke-recreate-revive",
                 {"edge": e1.id, "created": snap_created,
                  "revoked": snap_revoked, "recreated": snap_recreated},
                 svc.store.current_snapshot(), "RECREATE_REVIVES",
                 "re-create after revoke is active from its own snapshot; "
                 "each snapshot keeps its own deterministic answer")


def test_add_lineage_to_retired_entity_rejected(svc):
    _table(svc, "t1")
    _table(svc, "t2")
    svc.catalog.retire_entity("t2")
    with pytest.raises(RetiredError) as exc_info:
        svc.lineage.add_lineage("t1", "t2", LineageLevel.TABLE)
    assert exc_info.value.reason == "entity_retired"
    with pytest.raises(RetiredError):
        svc.lineage.add_lineage("t2", "t1", LineageLevel.TABLE)
    log_judgment("lineage-to-retired",
                 {"src": "t1", "dst": "t2(retired)"},
                 svc.store.current_snapshot(),
                 f"REJECTED:{exc_info.value.reason}",
                 "retired endpoint rejected, distinct from not_found")


# ---------- 退役与血缘并发交错 ----------

def test_concurrent_retire_vs_add_lineage_exactly_one_wins(svc):
    """同一实体上并发"退役"与"建边"：恰好一方成功，失败原因可区分。

    判定依据：两个操作在同一事务锁内串行提交——
    若建边先提交，退役必须以 entity_has_active_lineage 失败；
    若退役先提交，建边必须以 entity_retired 失败。
    终态只能是 (边活跃且未退役) 或 (无边且已退役)，无中间态。
    """
    rounds = 40
    outcomes = {"retire_first": 0, "lineage_first": 0}
    for r in range(rounds):
        src, dst = f"cr{r}a", f"cr{r}b"
        _table(svc, src)
        _table(svc, dst)
        results: dict[str, object] = {}

        def do_retire() -> None:
            try:
                svc.catalog.retire_entity(dst)
                results["retire"] = "ok"
            except ConflictError as exc:
                results["retire"] = exc.reason

        def do_add() -> None:
            try:
                svc.lineage.add_lineage(src, dst, LineageLevel.TABLE)
                results["add"] = "ok"
            except RetiredError as exc:
                results["add"] = exc.reason

        t1 = threading.Thread(target=do_retire)
        t2 = threading.Thread(target=do_add)
        # 交替启动顺序，确保两种赢面都被真实覆盖
        if r % 2 == 0:
            t1.start()
            t2.start()
        else:
            t2.start()
            t1.start()
        t1.join()
        t2.join()

        snap = svc.store.current_snapshot()
        view = svc.store.view_at(snap)
        retired = dst in view.retired_entities
        edge_active = svc.store.edge_active_at(
            f"edge:TABLE:{src}->{dst}", snap)
        if results["retire"] == "ok":
            # 退役赢 => 建边必须因 entity_retired 失败，且无边
            assert results["add"] == "entity_retired", results
            assert retired and not edge_active
            outcomes["retire_first"] += 1
        else:
            # 建边赢 => 退役必须因 entity_has_active_lineage 失败
            assert results["retire"] == "entity_has_active_lineage", results
            assert results["add"] == "ok"
            assert edge_active and not retired
            outcomes["lineage_first"] += 1
    assert outcomes["retire_first"] + outcomes["lineage_first"] == rounds
    # 两种赢面都必须真实出现，否则对应分支的判定依据未被覆盖
    assert outcomes["retire_first"] > 0 and outcomes["lineage_first"] > 0, \
        outcomes
    log_judgment("concurrent-retire-vs-lineage",
                 {"rounds": rounds, **outcomes},
                 svc.store.current_snapshot(), "EXACTLY_ONE_WINNER",
                 "loser fails with distinguishable reason; "
                 "final state always consistent")


def test_concurrent_retire_revoke_reads_never_see_dangling(svc):
    """建边/撤边/退役/查询并发交错：任何快照都不得出现
    "活跃边指向已退役实体"的悬空状态，且影响分析可复现。"""
    n = 12
    for i in range(n):
        _table(svc, f"m{i:02d}")
    lg = svc.lineage
    stop = threading.Event()
    violations: list[str] = []

    def writer(seed: int) -> None:
        i = seed
        while not stop.is_set():
            a, b = f"m{i % n:02d}", f"m{(i + 1) % n:02d}"
            op = i % 4
            try:
                if op == 0:
                    lg.add_lineage(a, b, LineageLevel.TABLE)
                elif op == 1:
                    lg.revoke_lineage(a, b, LineageLevel.TABLE)
                elif op == 2:
                    svc.catalog.retire_entity(b)
                else:
                    lg.impact_analysis(a, "downstream")
            except (ConflictError, RetiredError, NotFoundError):
                pass  # 冲突方显式失败是预期行为
            i += 1

    def reader() -> None:
        while not stop.is_set():
            snap = svc.store.current_snapshot()
            view = svc.store.view_at(snap)
            # 判定一：快照内不得有活跃边引用该快照下已退役的实体
            for edge in view.edges:
                if (edge.source_entity in view.retired_entities
                        or edge.target_entity in view.retired_entities):
                    violations.append(
                        f"snapshot {snap}: active edge {edge.id} "
                        f"references retired entity"
                    )
            # 判定二：快照内每条活跃边的两端实体都必须存在（无半更新）
            for edge in view.edges:
                if (edge.source_entity not in view.entities
                        or edge.target_entity not in view.entities):
                    violations.append(
                        f"snapshot {snap}: edge {edge.id} half-committed"
                    )

    with ThreadPoolExecutor(max_workers=6) as pool:
        wfutures = [pool.submit(writer, s) for s in range(3)]
        rfutures = [pool.submit(reader) for _ in range(3)]
        stop.wait(0.6)
        stop.set()
        for f in wfutures + rfutures:
            f.result()

    log_judgment("concurrent-retire-revoke-read",
                 {"writers": 3, "readers": 3,
                  "violations": len(violations)},
                 svc.store.current_snapshot(),
                 "ATOMIC" if not violations else "CORRUPTED",
                 "no snapshot shows active edge to retired entity "
                 "or half-committed edge")
    assert not violations, violations


def test_old_snapshot_reproducible_across_revoke_and_retire(svc):
    """撤销+退役之后，最早快照上的影响分析结论仍逐位复现。"""
    for tid in ("t1", "t2", "t3"):
        _table(svc, tid)
    lg = svc.lineage
    lg.add_lineage("t1", "t2", LineageLevel.TABLE)
    lg.add_lineage("t2", "t3", LineageLevel.TABLE)
    snap0 = svc.store.current_snapshot()
    admin = User("root", is_admin=True)
    original = lg.scoped_impact_analysis(admin, "t1", "downstream",
                                         snapshot_id=snap0)
    sig_original = [(n.node.entity_id, n.depth, n.via_edge_id)
                    for n in original["all_hits"]]

    lg.revoke_lineage("t2", "t3", LineageLevel.TABLE)
    lg.revoke_lineage("t1", "t2", LineageLevel.TABLE)
    svc.catalog.retire_entity("t3")
    svc.catalog.retire_entity("t2")

    replay = lg.scoped_impact_analysis(admin, "t1", "downstream",
                                       snapshot_id=snap0)
    sig_replay = [(n.node.entity_id, n.depth, n.via_edge_id)
                  for n in replay["all_hits"]]
    assert sig_replay == sig_original, \
        "撤销与退役不得改变历史快照上的既有结论"
    # 老快照上的发现结果也原样保留（t3 当时未退役）
    page = svc.discovery.query(admin, owner="alice", snapshot_id=snap0)
    assert {e.id for e in page["items"]} == {"t1", "t2", "t3"}
    log_judgment("old-snapshot-survives-retire",
                 {"snapshot": snap0, "sig": sig_original},
                 svc.store.current_snapshot(), "REPRODUCIBLE",
                 "revoke+retire leave historical snapshot conclusions intact")
