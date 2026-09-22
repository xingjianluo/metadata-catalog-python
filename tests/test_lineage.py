"""血缘建立、双向展开、环路显式标记、快照可复现。"""
from __future__ import annotations

import pytest

from metadata_catalog.errors import LineageError, NotFoundError
from metadata_catalog.models import EntityKind, FieldSpec, FieldType, LineageLevel

from .conftest import log_judgment


def _three_tables(svc):
    c = svc.catalog
    for tid in ["t1", "t2", "t3"]:
        c.register_entity(
            tid, tid.upper(), EntityKind.TABLE,
            [FieldSpec("a", FieldType.STRING),
             FieldSpec("b", FieldType.INTEGER)], None, "alice",
        )


def test_add_lineage_is_idempotent(svc):
    _three_tables(svc)
    e1, created1 = svc.lineage.add_lineage("t1", "t2", LineageLevel.TABLE)
    e2, created2 = svc.lineage.add_lineage("t1", "t2", LineageLevel.TABLE)
    assert created1 is True and created2 is False and e1 == e2
    log_judgment("lineage-idempotent", {"edge": e1.id},
                 e1.created_snapshot, "ACCEPTED_IDEMPOTENT",
                 "identical five-tuple returns same edge, no new snapshot")


def test_self_lineage_rejected(svc):
    _three_tables(svc)
    with pytest.raises(LineageError) as exc_info:
        svc.lineage.add_lineage("t1", "t1", LineageLevel.TABLE)
    assert exc_info.value.reason == "self_lineage"
    log_judgment("self-lineage", {"src": "t1", "dst": "t1"},
                 svc.store.current_snapshot(),
                 f"REJECTED:{exc_info.value.reason}",
                 "self lineage disallowed")


def test_level_column_mismatch_rejected(svc):
    _three_tables(svc)
    with pytest.raises(LineageError) as exc_info:
        svc.lineage.add_lineage("t1", "t2", LineageLevel.COLUMN, "a", None)
    assert exc_info.value.reason == "level_column_mismatch"
    with pytest.raises(LineageError) as exc_info2:
        svc.lineage.add_lineage("t1", "t2", LineageLevel.TABLE, "a", None)
    assert exc_info2.value.reason == "level_column_mismatch"


def test_lineage_rejects_unknown_entity_and_column(svc):
    _three_tables(svc)
    with pytest.raises(NotFoundError):
        svc.lineage.add_lineage("ghost", "t2", LineageLevel.TABLE)
    with pytest.raises(LineageError) as exc_info:
        svc.lineage.add_lineage("t1", "t2", LineageLevel.COLUMN, "a", "zzz")
    assert exc_info.value.reason == "column_not_found"
    log_judgment("lineage-bad-column",
                 {"src": "t1.a", "dst": "t2.zzz"},
                 svc.store.current_snapshot(),
                 f"REJECTED:{exc_info.value.reason}",
                 "target column does not exist")


def test_upstream_downstream_direct_expansion(svc):
    _three_tables(svc)
    lg = svc.lineage
    lg.add_lineage("t1", "t2", LineageLevel.TABLE)
    lg.add_lineage("t3", "t1", LineageLevel.TABLE)
    up = [n.entity_id for n in lg.upstream("t1")]
    down = [n.entity_id for n in lg.downstream("t1")]
    assert up == ["t3"], up
    assert down == ["t2"], down
    log_judgment("direct-expansion", {"node": "t1", "up": up, "down": down},
                 svc.store.current_snapshot(), "OK",
                 "upstream/downstream neighbor sets")


def test_cycle_is_marked_not_truncated(svc):
    _three_tables(svc)
    lg = svc.lineage
    lg.add_lineage("t1", "t2", LineageLevel.TABLE)
    lg.add_lineage("t2", "t3", LineageLevel.TABLE)
    lg.add_lineage("t3", "t1", LineageLevel.TABLE)
    result = lg.impact_analysis("t1", "downstream")
    ids = [n.node.entity_id for n in result["nodes"]]
    assert ids == ["t2", "t3"], ids
    cycles = result["cycles_detected"]
    assert len(cycles) == 1, "exactly one back-edge to ancestor expected"
    cyc = cycles[0]
    path_ids = [n.entity_id for n in cyc.cycle_path]
    assert cyc.cycle is True
    assert path_ids == ["t1", "t2", "t3", "t1"], path_ids
    log_judgment("cycle-marked",
                 {"start": "t1", "path": path_ids},
                 result["snapshot_id"], "CYCLE_DETECTED",
                 "back-edge to ancestor reported with full cycle path; "
                 "no silent truncation",
                 )


def test_impact_result_reproducible_on_same_snapshot(svc):
    _three_tables(svc)
    lg = svc.lineage
    lg.add_lineage("t1", "t2", LineageLevel.TABLE)
    lg.add_lineage("t2", "t3", LineageLevel.TABLE)
    snap = svc.store.current_snapshot()
    r1 = lg.impact_analysis("t1", "downstream", snapshot_id=snap)
    # 并发交错进来的新实体/新边不影响旧快照结论
    svc.catalog.register_entity("t4", "T4", EntityKind.TABLE, [], None, "alice")
    lg.add_lineage("t3", "t4", LineageLevel.TABLE)
    r2 = lg.impact_analysis("t1", "downstream", snapshot_id=snap)
    sig1 = [(n.node.entity_id, n.depth, n.cycle) for n in r1["all_hits"]]
    sig2 = [(n.node.entity_id, n.depth, n.cycle) for n in r2["all_hits"]]
    assert sig1 == sig2
    assert r1["snapshot_id"] == snap == r2["snapshot_id"]
    log_judgment("snapshot-reproducible",
                 {"snapshot": snap, "sig": sig1}, snap, "REPRODUCIBLE",
                 "later commits do not alter an older snapshot result")


def test_order_is_stable_alphabetic(svc):
    # t1 同时流向 t3 与 t2，输出仍须按 id 稳定排序
    _three_tables(svc)
    lg = svc.lineage
    lg.add_lineage("t1", "t3", LineageLevel.TABLE)
    lg.add_lineage("t1", "t2", LineageLevel.TABLE)
    result = lg.impact_analysis("t1", "downstream")
    assert [n.node.entity_id for n in result["nodes"]] == ["t2", "t3"]


def test_column_level_lineage(svc):
    _three_tables(svc)
    svc.lineage.add_lineage(
        "t1", "t2", LineageLevel.COLUMN, "a", "b", "UPPER",
    )
    down = [(n.entity_id, n.column)
            for n in svc.lineage.downstream("t1", "a")]
    assert down == [("t2", "b")]
    up = [(n.entity_id, n.column)
          for n in svc.lineage.upstream("t2", "b")]
    assert up == [("t1", "a")]
    log_judgment("column-lineage", {"down": down, "up": up},
                 svc.store.current_snapshot(), "OK",
                 "column-level anchors resolve both directions")
