"""并发写入与查询交错场景下的一致性判定。

判定依据：
1. 分页稳定性：首页固定快照版本后，用返回的 next_cursor 逐页拉取，
   即使另一线程持续写入，整份结果无重复、无遗漏，且每页快照版本一致；
2. 快照原子性：血缘与实体写入交错时，任何已提交快照中每条边的两端点
   都必须存在（不存在半更新状态），快照内边数只增不减；
3. 可复现：同一快照版本上重复分页得到完全相同的结论。
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from metadata_catalog.models import ColumnDef, ColumnType
from tests.helpers import log_case


def _seed(store, n: int, owner: str = "alice") -> None:
    for i in range(n):
        store.register_entity(
            f"e{i:04d}",
            f"entity-{i}",
            owner,
            [ColumnDef("id", ColumnType.INTEGER, nullable=False)],
            tags={"even"} if i % 2 == 0 else {"odd"},
        )


def test_pagination_stable_under_concurrent_writes(store) -> None:
    _seed(store, 100)
    stop = threading.Event()

    def _writer() -> None:
        i = 100
        while not stop.is_set():
            store.register_entity(
                f"w{i:04d}",
                f"writer-{i}",
                "alice",
                [ColumnDef("id", ColumnType.INTEGER, nullable=False)],
            )
            i += 1

    writer = threading.Thread(target=_writer)
    writer.start()
    try:
        first = store.list_entities("alice", limit=7)
        pinned_version = first.snapshot_version
        # 期望值直接取自被固定快照本身（与分页遍历无关的另一条读取路径）
        pinned_snapshot = store.snapshot_at(pinned_version)
        expected = {
            eid
            for eid, e in pinned_snapshot.entities.items()
            if e.owner == "alice" or eid in pinned_snapshot.acl.get("alice", frozenset())
        }
        seen: list[str] = [e.id for e in first.items]
        pages = 1
        cursor = first.next_cursor
        while cursor is not None:
            page = store.list_entities("alice", limit=7, cursor=cursor)
            assert page.snapshot_version == pinned_version, (
                f"page {pages} drifted to snapshot {page.snapshot_version}, "
                f"expected {pinned_version}"
            )
            seen.extend(e.id for e in page.items)
            cursor = page.next_cursor
            pages += 1
    finally:
        stop.set()
        writer.join()

    assert len(seen) == len(set(seen)), "pagination produced duplicate items"
    assert set(seen) == expected, (
        "pagination missed or admitted items relative to pinned snapshot"
    )
    log_case(
        "pagination_stable_with_writer",
        pinned_version,
        "PASS",
        pages=pages,
        items=len(seen),
    )


def test_filtered_pagination_pins_filters(store) -> None:
    _seed(store, 50)
    first = store.list_entities("alice", tag="even", limit=4)
    pinned = first.snapshot_version
    ids: list[str] = [e.id for e in first.items]
    cursor = first.next_cursor
    while cursor:
        page = store.list_entities("alice", tag="even", limit=4, cursor=cursor)
        assert page.snapshot_version == pinned
        ids.extend(e.id for e in page.items)
        cursor = page.next_cursor
    expected = {f"e{i:04d}" for i in range(50) if i % 2 == 0}
    assert set(ids) == expected and len(ids) == len(set(ids))
    log_case("filtered_pagination", pinned, "PASS", items=len(ids))


def test_replay_same_snapshot_is_identical(store) -> None:
    _seed(store, 30)
    page_a = store.list_entities("alice", limit=5)
    version = page_a.snapshot_version
    # 后续写入
    _seed_tail = store.register_entity(
        "later", "later", "alice", [ColumnDef("id", ColumnType.INTEGER, nullable=False)]
    )
    assert _seed_tail.snapshot_version > version
    page_b = store.list_entities("alice", limit=5, snapshot_version=version)
    assert [e.id for e in page_a.items] == [e.id for e in page_b.items]
    assert page_b.snapshot_version == version
    log_case("snapshot_replay", version, "PASS", ids=[e.id for e in page_b.items])


def test_interleaved_writes_never_half_updated(store) -> None:
    stop = threading.Event()
    observed: list[tuple[int, int, bool]] = []

    def _writer() -> None:
        for i in range(200):
            eid = f"t{i:04d}"
            store.register_entity(
                eid, eid, "alice", [ColumnDef("id", ColumnType.STRING)]
            )
            if i > 0:
                store.add_lineage(f"t{i-1:04d}", eid)
            if i % 10 == 0:
                time.sleep(0.0005)  # 强制与读线程交错
        stop.set()

    def _reader() -> None:
        while not stop.is_set():
            snap = store.current_snapshot()
            ids = set(snap.entities)
            complete = all(
                e.source_entity in ids and e.target_entity in ids for e in snap.edges
            )
            observed.append((snap.version, len(snap.edges), complete))
            assert complete, f"snapshot {snap.version} has dangling lineage edge"
            time.sleep(0.0002)

    tw = threading.Thread(target=_writer)
    tr = threading.Thread(target=_reader)
    tw.start()
    tr.start()
    tw.join()
    tr.join()

    # 快照旧版本保留，最终快照边数 = 实体链数；观察到的边数随版本单调不减
    final = store.current_snapshot()
    assert len(observed) > 1, "reader did not interleave with writer"
    edge_counts = [count for _, count, _ in observed]
    assert edge_counts == sorted(edge_counts), "observed edge count went backwards"
    assert len(final.edges) == 199
    log_case(
        "no_half_updated_snapshot",
        final.version,
        "PASS",
        observations=len(observed),
        final_edges=len(final.edges),
    )


def test_concurrent_registrations_each_gets_commit(store) -> None:
    def _register(i: int) -> int:
        result = store.register_entity(
            f"p{i:03d}", f"p{i}", "alice", [ColumnDef("id", ColumnType.INTEGER, nullable=False)]
        )
        return result.snapshot_version

    with ThreadPoolExecutor(max_workers=8) as pool:
        versions = list(pool.map(_register, range(50)))
    # 50 个不同实体各占一个提交版本，无丢失
    assert len(set(versions)) == 50
    page = store.list_entities("alice", limit=100)
    assert {e.id for e in page.items} == {f"p{i:03d}" for i in range(50)}
    log_case("concurrent_registration", store.current_snapshot(), "PASS", versions=len(set(versions)))
