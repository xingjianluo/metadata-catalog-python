"""分页、标签/归属过滤、越权拒绝与血缘可见性裁剪。"""
from __future__ import annotations

import pytest

from metadata_catalog.errors import (
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from metadata_catalog.models import EntityKind, LineageLevel, User

from .conftest import log_judgment


def _seed(svc, n=6):
    c = svc.catalog
    for i in range(n):
        owner = "alice" if i < 4 else "bob"
        tags = {"odd"} if i % 2 else {"even"}
        c.register_entity(f"e{i}", f"E{i}", EntityKind.TABLE,
                          [], tags, owner)


def test_pagination_is_complete_and_ordered(svc):
    _seed(svc)
    d = svc.discovery
    admin = User("root", is_admin=True)
    seen, token = [], None
    while True:
        page = d.query(admin, limit=2, page_token=token)
        seen.extend(e.id for e in page["items"])
        token = page["next_page_token"]
        if token is None:
            break
    assert seen == sorted(seen), f"page order unstable: {seen}"
    assert len(seen) == len(set(seen)), f"duplicates: {seen}"
    assert set(seen) == {f"e{i}" for i in range(6)}, f"missing: {seen}"
    log_judgment("pagination-complete",
                 {"limit": 2, "seen": seen},
                 svc.store.current_snapshot(), "COMPLETE",
                 "no duplicates and no omissions across pages")


def test_concurrent_writes_do_not_duplicate_or_skip_pages(svc):
    """翻页期间交错写入：固定快照的翻页必须不重不漏。"""
    _seed(svc, n=4)
    d, c = svc.discovery, svc.catalog
    admin = User("root", is_admin=True)
    first = d.query(admin, limit=2)
    assert [e.id for e in first["items"]] == ["e0", "e1"]
    # 交错：在翻到第二页之前写入新实体（含一个 id 排序靠前、一个靠后）
    c.register_entity("e00", "E00", EntityKind.TABLE, [], None, "alice")
    c.register_entity("e9", "E9", EntityKind.TABLE, [], None, "alice")
    second = d.query(admin, limit=2, page_token=first["next_page_token"])
    assert first["snapshot_id"] == second["snapshot_id"]
    assert [e.id for e in second["items"]] == ["e2", "e3"]
    assert second["next_page_token"] is None
    seen = ["e0", "e1"] + [e.id for e in second["items"]]
    assert set(seen) == {"e0", "e1", "e2", "e3"}
    log_judgment(
        "pagination-concurrent",
        {"interleaved_writes": ["e00", "e9"], "seen": seen},
        first["snapshot_id"], "STABLE",
        "token pins snapshot; new writes do not duplicate/skip old pages",
    )


def test_tag_and_owner_filters(svc):
    _seed(svc)
    d = svc.discovery
    admin = User("root", is_admin=True)
    r = d.query(admin, tags={"even"})
    assert [e.id for e in r["items"]] == ["e0", "e2", "e4"]
    r = d.query(admin, owner="bob")
    assert [e.id for e in r["items"]] == ["e4", "e5"]
    r = d.query(admin, tags={"even"}, owner="bob")
    assert [e.id for e in r["items"]] == ["e4"]
    log_judgment("filters", {"tags": {"even"}, "owner": "bob"},
                 r["snapshot_id"], "FILTERED",
                 "tag-any match plus exact owner intersection")


def test_non_owner_sees_only_granted_entities(svc):
    _seed(svc)
    v, d = svc.visibility, svc.discovery
    carol = User("carol")
    assert d.query(carol)["items"] == []
    v.grant("carol", "e1")
    r = d.query(carol)
    assert [e.id for e in r["items"]] == ["e1"]
    log_judgment("grant-visibility", {"user": "carol", "granted": ["e1"]},
                 r["snapshot_id"], "SCOPED",
                 "unauthorized entities excluded from discovery")


def test_unauthorized_entity_access_is_denied_not_empty(svc):
    _seed(svc)
    carol = User("carol")
    with pytest.raises(PermissionDeniedError) as exc_info:
        svc.visibility.require_read(carol, "e0")
    assert exc_info.value.reason == "permission_denied"
    with pytest.raises(NotFoundError) as exc_info2:
        svc.visibility.require_read(carol, "ghost")
    assert exc_info2.value.reason == "not_found"
    log_judgment(
        "deny-vs-notfound",
        {"existing-but-forbidden": "e0", "missing": "ghost"},
        svc.store.current_snapshot(), "REJECTED",
        "permission_denied and not_found must be distinguishable",
    )


def test_owner_access_grant_covers_existing_entities(svc):
    _seed(svc)
    svc.visibility.grant_owner_access("dave")
    dave = User("dave")
    r = svc.discovery.query(dave)
    assert len(r["items"]) == 6


def test_lineage_is_hidden_when_either_end_forbidden(svc):
    _seed(svc)
    c, lg, v = svc.catalog, svc.lineage, svc.visibility
    # e0(alice) -> e5(bob)
    lg.add_lineage("e0", "e5", LineageLevel.TABLE)
    v.grant("carol", "e0")
    carol = User("carol")
    # carol 只见 e0：血缘另一端 e5 不可见，故下游展开为空
    result = lg.scoped_impact_analysis(carol, "e0", "downstream")
    assert result["nodes"] == []
    # 起点越权本身被拒绝
    with pytest.raises(PermissionDeniedError) as exc_info:
        lg.scoped_impact_analysis(carol, "e5", "upstream")
    assert exc_info.value.reason == "permission_denied"
    log_judgment(
        "lineage-endpoint-visibility",
        {"user": "carol", "visible": ["e0"], "edge": "e0->e5"},
        result["snapshot_id"], "HIDDEN",
        "edge with an invisible endpoint is not exposed; "
        "unauthorized start raises permission_denied",
    )


def test_invalid_page_token_rejected(svc):
    _seed(svc)
    with pytest.raises(ValidationError) as exc_info:
        svc.discovery.query(User("root", is_admin=True), page_token="garbage")
    assert exc_info.value.reason == "invalid_page_token"
