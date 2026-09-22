"""基于标签与归属的发现、分页可见性与越权拒绝判定。

判定依据：
- owner 隐式可见；被 grant 的用户可见；未授权用户不可见；
- 越权访问具体实体/血缘抛 UNAUTHORIZED_*（403），而不是返回空；
- 标签/归属过滤只在可见集合内进行；
- 分页 cursor 内嵌快照版本，并发写入下翻页无重复、无遗漏。
"""

from __future__ import annotations

import pytest

from metadata_catalog.errors import RejectReason
from metadata_catalog.models import ColumnDef, ColumnType
from tests.helpers import assert_reason, log_case


def _entity(store, eid: str, owner: str, tags: set[str] | None = None) -> None:
    store.register_entity(
        eid, eid, owner, [ColumnDef("id", ColumnType.INTEGER, nullable=False)], tags=tags or set()
    )


def test_owner_and_grant_visibility(store) -> None:
    _entity(store, "e1", "alice", tags={"pii"})
    _entity(store, "e2", "carol", tags={"pii"})
    store.grant("e2", "alice")

    alice_page = store.list_entities("alice")
    assert {e.id for e in alice_page.items} == {"e1", "e2"}
    bob_page = store.list_entities("bob")
    assert bob_page.items == ()
    log_case(
        "owner_and_grant_visibility",
        store.current_snapshot(),
        "PASS",
        alice=sorted(e.id for e in alice_page.items),
        bob=sorted(e.id for e in bob_page.items),
    )


def test_tag_filter_within_visible_set(store) -> None:
    _entity(store, "e1", "alice", tags={"pii"})
    _entity(store, "e2", "alice", tags={"finance"})
    _entity(store, "secret", "carol", tags={"finance"})  # alice 不可见
    page = store.list_entities("alice", tag="finance")
    assert {e.id for e in page.items} == {"e2"}
    log_case("tag_filter", store.current_snapshot(), "PASS", ids=[e.id for e in page.items])


def test_owner_filter(store) -> None:
    _entity(store, "e1", "alice")
    _entity(store, "e2", "carol")
    page = store.list_entities("alice", owner="alice")
    assert {e.id for e in page.items} == {"e1"}
    log_case("owner_filter", store.current_snapshot(), "PASS", ids=[e.id for e in page.items])


def test_unauthorized_entity_access_rejected(store) -> None:
    _entity(store, "e1", "alice")
    with pytest.raises(Exception) as exc:
        store.impact("e1", "downstream", "bob")
    assert_reason(exc, RejectReason.UNAUTHORIZED_ENTITY, "unauthorized_entity", store.current_snapshot())


def test_unauthorized_lineage_query_rejected_not_empty(store) -> None:
    _entity(store, "e1", "alice")
    _entity(store, "e2", "carol")
    store.add_lineage("e1", "e2")
    # 未授权用户查询真实存在的血缘 -> 403 而不是空列表
    with pytest.raises(Exception) as exc:
        store.lineage_between("e1", "e2", "bob")
    assert_reason(exc, RejectReason.UNAUTHORIZED_LINEAGE, "unauthorized_lineage", store.current_snapshot())


def test_impact_hides_invisible_edges(store) -> None:
    # alice 的 a 流向 bob 的 secret 再流向 bob 的 out；
    # alice 只能看到 a，展开后不应泄露 secret/out 的存在。
    _entity(store, "a", "alice")
    _entity(store, "secret", "bob")
    _entity(store, "out", "bob")
    store.add_lineage("a", "secret")
    store.add_lineage("secret", "out")
    r = store.impact("a", "downstream", "alice")
    assert r.nodes == ("a",)
    assert r.edges == ()
    assert r.cycles == ()
    log_case("invisible_edges_hidden", store.current_snapshot(), "PASS", nodes=r.nodes)


def test_grant_then_lineage_visible(store) -> None:
    _entity(store, "a", "alice")
    _entity(store, "b", "bob")
    store.add_lineage("a", "b")
    # 授权 alice 看 b 之后，血缘边立即可见
    store.grant("b", "alice")
    edges = store.lineage_between("a", "b", "alice")
    assert len(edges) == 1
    r = store.impact("a", "downstream", "alice")
    assert r.nodes == ("a", "b")
    log_case("grant_reveals_lineage", store.current_snapshot(), "PASS", nodes=r.nodes)
