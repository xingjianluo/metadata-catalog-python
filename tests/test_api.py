"""HTTP 端到端：状态码、可区分错误码、越权 403 与分页/影响分析响应结构。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from metadata_catalog.api import create_app
from metadata_catalog.store import CatalogStore
from tests.helpers import log_case


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(CatalogStore()))


def _mk(client: TestClient, eid: str, owner: str, cols: list[dict] | None = None, tags=None) -> None:
    client.post(
        "/entities",
        json={
            "id": eid,
            "name": eid,
            "owner": owner,
            "columns": cols if cols is not None else [{"name": "id", "type": "INTEGER", "nullable": False}],
            "tags": tags or [],
        },
    )


def test_register_and_idempotent_http(client: TestClient) -> None:
    payload = {
        "id": "e1", "name": "orders", "owner": "alice",
        "columns": [{"name": "id", "type": "INTEGER", "nullable": False}],
        "tags": ["pii"],
    }
    r1 = client.post("/entities", json=payload)
    r2 = client.post("/entities", json=payload)
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["snapshot_version"] == r2.json()["snapshot_version"]
    log_case("http_idempotent", r1.json()["snapshot_version"], "PASS")


def test_rejections_have_distinct_reasons_http(client: TestClient) -> None:
    _mk(client, "e1", "alice")
    # 1) 标识冲突
    conflict = client.post(
        "/entities",
        json={"id": "e1", "name": "other", "owner": "alice", "columns": [
            {"name": "id", "type": "INTEGER", "nullable": False}]},
    )
    assert conflict.status_code == 422
    assert conflict.json()["error"]["reason"] == "SCHEMA_IDENTITY_CONFLICT"

    # 2) 新 NOT NULL 列无默认
    evolve = client.post(
        "/entities",
        json={"id": "e1", "name": "e1", "owner": "alice", "columns": [
            {"name": "id", "type": "INTEGER", "nullable": False},
            {"name": "x", "type": "STRING", "nullable": False}]},
    )
    assert evolve.json()["error"]["reason"] == "NEW_COLUMN_NO_DEFAULT"
    log_case("http_distinct_reasons", client.get("/health").json()["snapshot"], "PASS",
             reasons=["SCHEMA_IDENTITY_CONFLICT", "NEW_COLUMN_NO_DEFAULT"])


def test_unauthorized_is_403_not_empty(client: TestClient) -> None:
    _mk(client, "secret", "alice")
    r_list = client.get("/entities", headers={"X-User": "bob"})
    assert r_list.json()["items"] == []  # 目录列举按可见性裁剪为空，合法
    r_impact = client.get(
        "/entities/secret/impact", params={"direction": "downstream"},
        headers={"X-User": "bob"},
    )
    assert r_impact.status_code == 403
    assert r_impact.json()["error"]["reason"] == "UNAUTHORIZED_ENTITY"
    log_case("http_403_for_unauthorized", client.get("/health").json()["snapshot"], "PASS")


def test_lineage_impact_and_cycle_http(client: TestClient) -> None:
    _mk(client, "a", "alice")
    _mk(client, "b", "alice")
    _mk(client, "c", "alice")
    client.post("/lineage", json={"source_entity": "a", "target_entity": "b"})
    client.post("/lineage", json={"source_entity": "b", "target_entity": "c"})
    client.post("/lineage", json={"source_entity": "c", "target_entity": "a"})

    r = client.get("/entities/a/impact", params={"direction": "downstream"},
                   headers={"X-User": "alice"})
    body = r.json()
    assert body["nodes"] == ["a", "b", "c"]
    assert body["cycles"] == [["a", "b", "c"]]
    log_case("http_cycle_marked", body["snapshot_version"], "PASS", cycles=body["cycles"])


def test_pagination_http(client: TestClient) -> None:
    for i in range(15):
        _mk(client, f"e{i:02d}", "alice", tags=["x"] if i % 2 == 0 else [])
    collected: list[str] = []
    cursor = None
    version = None
    for _ in range(10):
        params = {"limit": 4, "tag": "x"}
        if cursor:
            params["cursor"] = cursor
        page = client.get("/entities", params=params, headers={"X-User": "alice"}).json()
        if version is None:
            version = page["snapshot_version"]
        assert page["snapshot_version"] == version
        collected.extend(i["id"] for i in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert collected == sorted(set(collected))
    assert len(collected) == 8  # 0,2,...,14
    log_case("http_pagination", version, "PASS", items=collected)
