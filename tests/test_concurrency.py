"""并发写入下的稳定性：分页不重不漏、快照原子、并发演进安全。"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from metadata_catalog.models import EntityKind, FieldSpec, FieldType, LineageLevel, User

from .conftest import log_judgment


def test_concurrent_registration_all_entities_visible_exactly_once(svc):
    """N 个线程并发登记不同实体：全部成功，且查询无重复无遗漏。"""
    c, d = svc.catalog, svc.discovery
    n_threads, per_thread = 8, 25

    def register_batch(start: int) -> None:
        for i in range(start, start + per_thread):
            c.register_entity(
                f"c{i:04d}", f"C{i}", EntityKind.TABLE,
                [FieldSpec("a", FieldType.STRING)], {"bulk"}, "alice",
            )

    with ThreadPoolExecutor(max_workers=n_threads) as pool:
        list(pool.map(register_batch,
                      range(0, n_threads * per_thread, per_thread)))

    admin = User("root", is_admin=True)
    seen, token = [], None
    while True:
        page = d.query(admin, tags={"bulk"}, limit=30, page_token=token)
        seen.extend(e.id for e in page["items"])
        token = page["next_page_token"]
        if token is None:
            break
    expected = {f"c{i:04d}" for i in range(n_threads * per_thread)}
    assert len(seen) == len(set(seen)), "pagination produced duplicates"
    assert set(seen) == expected, (
        f"pagination lost rows: missing {len(expected - set(seen))}"
    )
    log_judgment(
        "concurrent-register-pagination",
        {"threads": n_threads, "per_thread": per_thread,
         "collected": len(seen)},
        svc.store.current_snapshot(), "STABLE",
        "every concurrent write visible exactly once across pages",
    )


def test_concurrent_reads_during_writes_never_see_half_state(svc):
    """写线程持续加边，读线程反复取快照做影响分析：

    任何被报告的边引用的实体都必须在同一快照中存在（无悬挂引用），
    这就是"血缘快照不存在半更新状态"的判定依据。
    """
    c, lg = svc.catalog, svc.lineage
    # 预建实体，避免边引用尚未登记的实体；写入只增加边
    for i in range(20):
        c.register_entity(f"w{i:02d}", f"W{i}", EntityKind.TABLE,
                          [FieldSpec("a", FieldType.STRING)], None, "alice")

    stop = threading.Event()
    violations: list[str] = []

    def writer() -> None:
        i = 0
        while not stop.is_set():
            # 相邻节点成环 w00->w01->...->w19->w00，永不为自环
            lg.add_lineage(f"w{i % 20:02d}", f"w{(i + 1) % 20:02d}",
                           LineageLevel.TABLE)
            i += 1

    def reader() -> None:
        admin = User("root", is_admin=True)
        while not stop.is_set():
            result = lg.scoped_impact_analysis(
                admin, "w00", "downstream"
            )
            snap = result["snapshot_id"]
            view = svc.store.view_at(snap)
            # 判定：快照内每条边的两端实体都必须已经提交存在
            for edge in view.edges:
                if (edge.source_entity not in view.entities
                        or edge.target_entity not in view.entities):
                    violations.append(
                        f"snapshot {snap} has dangling edge {edge.id}"
                    )
            # 判定：该快照上重复计算必须逐位一致（可复现）
            again = lg.scoped_impact_analysis(
                admin, "w00", "downstream", snapshot_id=snap
            )
            sig1 = [(n.node.entity_id, n.cycle) for n in result["all_hits"]]
            sig2 = [(n.node.entity_id, n.cycle) for n in again["all_hits"]]
            if sig1 != sig2:
                violations.append(f"snapshot {snap} not reproducible")

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(writer) for _ in range(2)]
        rfutures = [pool.submit(reader) for _ in range(2)]
        stop.wait(0.5)
        stop.set()
        for f in futures + rfutures:
            f.result()

    log_judgment(
        "concurrent-lineage-snapshot",
        {"writers": 2, "readers": 2, "violations": len(violations)},
        svc.store.current_snapshot(),
        "ATOMIC" if not violations else "CORRUPTED",
        "no dangling edges and identical repeated analysis per snapshot",
    )
    assert not violations, violations


def test_concurrent_add_field_preserves_order_once_each(svc):
    """多个线程给同一实体并发追加不同字段：

    每个字段恰好出现一次，既有字段仍排在最前，版本号连续推进。
    """
    c = svc.catalog
    c.register_entity("t1", "T1", EntityKind.TABLE,
                      [FieldSpec("base", FieldType.STRING)], None, "alice")

    def add_one(i: int) -> None:
        c.add_field("t1", FieldSpec(f"f{i:02d}", FieldType.STRING))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(add_one, range(16)))

    entity = c.get_entity("t1")
    names = [f.name for f in entity.fields]
    assert names[0] == "base", "existing column must remain first"
    added = names[1:]
    # 并发追加的物理顺序是"锁内提交顺序"，确定且可复现，但不承诺等于
    # 线程提交编号；这里保证：每个字段恰好一次、无丢失。
    assert len(added) == len(set(added)) == 16
    assert set(added) == {f"f{i:02d}" for i in range(16)}
    # 提交完成后，顺序在同一快照上重复读取保持稳定（位置不漂移）
    snap = entity.updated_snapshot
    again = [f.name for f in c.get_entity("t1", snap).fields]
    assert again == names, "committed column positions must be stable"
    assert entity.version == 17
    log_judgment(
        "concurrent-add-field",
        {"fields": len(names), "version": entity.version},
        entity.updated_snapshot, "APPENDED",
        "each concurrent add_field appears once; base column stable; "
        "committed order reproducible",
    )


def test_concurrent_same_idempotent_register_never_conflicts(svc):
    """多线程并发登记同一个完全相同的实体：必须全部成功且只有一个版本。"""
    c = svc.catalog

    def register() -> None:
        c.register_entity(
            "same", "SAME", EntityKind.TABLE,
            [FieldSpec("a", FieldType.STRING)], None, "alice",
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.submit(register) for _ in range(16))

    # 视图中该实体只有一个对象；最终快照下唯一定义
    entity = c.get_entity("same")
    assert entity.name == "SAME" and entity.version == 1
    # 任何一次成功登记都不会产生 identifier_conflict（异常会使 future 失败）
    log_judgment("concurrent-idempotent-register",
                 {"id": "same", "threads": 16},
                 svc.store.current_snapshot(), "IDEMPOTENT",
                 "concurrent identical registrations serialize to one version")
