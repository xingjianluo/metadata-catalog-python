"""血缘登记、影响分析与环路标记的判定。

判定依据：
- 相同边重复登记幂等；表级/列级粒度冲突 -> LINEAGE_DUPLICATE_CONFLICT；
- 列级血缘引用不存在的列 -> COLUMN_NOT_FOUND；
- 影响分析在同一快照上可复现、节点与边顺序稳定；
- 环路（含自环与多跳环）必须出现在 cycles 中而不是被截断；
- 新增列后既有表级血缘边与检索结论不发生错位。
"""

from __future__ import annotations

import threading

import pytest

from metadata_catalog.errors import RejectReason
from metadata_catalog.models import ColumnDef, ColumnType
from tests.helpers import assert_reason, log_case


def _entity(store, eid: str, owner: str = "alice", cols: tuple[str, ...] = ("id",)) -> None:
    store.register_entity(
        eid,
        eid,
        owner,
        [ColumnDef(c, ColumnType.STRING) for c in cols],
    )


def test_lineage_idempotent(store) -> None:
    _entity(store, "a")
    _entity(store, "b")
    v1 = store.add_lineage("a", "b")
    v2 = store.add_lineage("a", "b")
    assert v1 == v2
    assert len(store.current_snapshot().edges) == 1
    log_case("lineage_idempotent", store.current_snapshot(), "PASS", versions=(v1, v2), edge_count=1)


def test_granularity_conflict(store) -> None:
    _entity(store, "a", cols=("id",))
    _entity(store, "b", cols=("id",))
    store.add_lineage("a", "b", "id", "id")
    with pytest.raises(Exception) as exc:
        store.add_lineage("a", "b")
    assert_reason(exc, RejectReason.LINEAGE_DUPLICATE_CONFLICT, "granularity_conflict", store.current_snapshot())


def test_column_lineage_unknown_column(store) -> None:
    _entity(store, "a", cols=("id",))
    _entity(store, "b", cols=("id",))
    with pytest.raises(Exception) as exc:
        store.add_lineage("a", "b", "id", "missing")
    assert_reason(exc, RejectReason.COLUMN_NOT_FOUND, "unknown_target_col", store.current_snapshot())


def test_downstream_impact_stable_and_reproducible(store) -> None:
    # a -> b -> c, a -> c（菱形），全部 owner=alice 可见
    for eid in ("a", "b", "c"):
        _entity(store, eid)
    store.add_lineage("a", "b")
    store.add_lineage("b", "c")
    store.add_lineage("a", "c")

    head = store.current_snapshot().version
    r1 = store.impact("a", "downstream", "alice")
    barrier = threading.Barrier(2)

    def _other_reader() -> None:
        barrier.wait()
        r = store.impact("a", "downstream", "alice")
        assert r == r1  # 同一快照并发读，结论逐字节一致
    t = threading.Thread(target=_other_reader)
    t.start()
    barrier.wait()
    r2 = store.impact("a", "downstream", "alice")
    t.join()

    assert r1.nodes == ("a", "b", "c")
    assert r1.snapshot_version == r2.snapshot_version == head == 6
    assert [e.table_key for e in r1.edges] == [("a", "b"), ("a", "c"), ("b", "c")]
    assert r1.cycles == ()
    log_case(
        "downstream_reproducible",
        store.current_snapshot(),
        "PASS",
        nodes=r1.nodes,
        edges=[e.table_key for e in r1.edges],
    )


def test_self_loop_is_marked_not_truncated(store) -> None:
    _entity(store, "a")
    store.add_lineage("a", "a")
    r = store.impact("a", "downstream", "alice")
    assert r.nodes == ("a",)
    assert r.cycles == (("a",),)
    assert len(r.edges) == 1
    log_case("self_loop_marked", store.current_snapshot(), "PASS", cycles=r.cycles)


def test_multi_hop_cycle_is_marked(store) -> None:
    # a -> b -> c -> a，且 a 还有下游 d（环外节点仍可到达）
    for eid in ("a", "b", "c", "d"):
        _entity(store, eid)
    store.add_lineage("a", "b")
    store.add_lineage("b", "c")
    store.add_lineage("c", "a")
    store.add_lineage("a", "d")
    r = store.impact("a", "downstream", "alice")
    assert r.nodes == ("a", "b", "c", "d")
    # 环被规范化为字典序最小起点表示
    assert r.cycles == (("a", "b", "c"),)
    log_case("multi_hop_cycle_marked", store.current_snapshot(), "PASS", cycles=r.cycles, nodes=r.nodes)


def test_upstream_direction(store) -> None:
    for eid in ("a", "b", "c"):
        _entity(store, eid)
    store.add_lineage("a", "b")
    store.add_lineage("b", "c")
    r = store.impact("c", "upstream", "alice")
    assert r.nodes == ("a", "b", "c")
    log_case("upstream_expansion", store.current_snapshot(), "PASS", nodes=r.nodes)


def test_evolution_keeps_existing_lineage_intact(store) -> None:
    _entity(store, "a", cols=("id",))
    _entity(store, "b", cols=("id",))
    store.add_lineage("a", "b", "id", "id")
    # b 追加新可空列，旧列位置不变；既有列级血缘仍然解析得到。
    store.register_entity(
        "b",
        "b",
        "alice",
        [ColumnDef("id", ColumnType.STRING), ColumnDef("note", ColumnType.STRING, nullable=True)],
    )
    edges = store.lineage_between("a", "b", "alice")
    assert [e.key for e in edges] == [("a", "b", "id", "id")]
    r = store.impact("a", "downstream", "alice")
    assert ("a", "b", "id", "id") in [e.key for e in r.edges]
    log_case("lineage_survives_evolution", store.current_snapshot(), "PASS", edges=[e.key for e in edges])
