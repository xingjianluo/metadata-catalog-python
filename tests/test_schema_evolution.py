"""实体登记幂等性与结构演进拒绝原因的判定。

判定依据：
- 完全相同的重复登记必须幂等，不产生新版本；
- 身份属性（name/owner）变更 -> SCHEMA_IDENTITY_CONFLICT；
- 遗漏既有列 -> SCHEMA_MISSING_FIELD；列类型/可空性变化 -> SCHEMA_TYPE_MISMATCH；
- 新 NOT NULL 列无显式默认值 -> NEW_COLUMN_NO_DEFAULT；
- 新增可空列允许，演进后旧列位置与类型不变（血缘/检索不错位）。
"""

from __future__ import annotations

import pytest

from metadata_catalog.errors import RejectReason
from metadata_catalog.models import ColumnDef, ColumnType
from tests.helpers import assert_reason, log_case


def _cols(*spec: tuple[str, ColumnType, bool, object | None]) -> list[ColumnDef]:
    return [ColumnDef(n, t, nullable=nu, default=d) for n, t, nu, d in spec]


BASE_COLS = [
    ColumnDef("id", ColumnType.INTEGER, nullable=False),
    ColumnDef("amount", ColumnType.FLOAT, nullable=True),
]


def test_idempotent_registration(store) -> None:
    first = store.register_entity("e1", "orders", "alice", list(BASE_COLS), tags={"pii"})
    second = store.register_entity("e1", "orders", "alice", list(BASE_COLS), tags={"pii"})
    assert first.snapshot_version == 1
    assert second.snapshot_version == first.snapshot_version
    assert second.evolved is False
    log_case(
        "idempotent_registration",
        store.current_snapshot(),
        "PASS",
        input={"id": "e1", "columns": [c.name for c in BASE_COLS]},
        versions=(first.snapshot_version, second.snapshot_version),
    )


def test_identity_conflict_name(store) -> None:
    store.register_entity("e1", "orders", "alice", list(BASE_COLS))
    with pytest.raises(Exception) as exc:
        store.register_entity("e1", "orders_v2", "alice", list(BASE_COLS))
    assert_reason(exc, RejectReason.SCHEMA_IDENTITY_CONFLICT, "identity_conflict_name", store.current_snapshot())


def test_identity_conflict_owner(store) -> None:
    store.register_entity("e1", "orders", "alice", list(BASE_COLS))
    with pytest.raises(Exception) as exc:
        store.register_entity("e1", "orders", "bob", list(BASE_COLS))
    assert_reason(exc, RejectReason.SCHEMA_IDENTITY_CONFLICT, "identity_conflict_owner", store.current_snapshot())


def test_missing_field_on_evolve(store) -> None:
    store.register_entity("e1", "orders", "alice", list(BASE_COLS))
    with pytest.raises(Exception) as exc:
        store.register_entity(
            "e1", "orders", "alice", [ColumnDef("id", ColumnType.INTEGER, nullable=False)]
        )
    assert_reason(exc, RejectReason.SCHEMA_MISSING_FIELD, "missing_field", store.current_snapshot())


def test_type_mismatch_on_evolve(store) -> None:
    store.register_entity("e1", "orders", "alice", list(BASE_COLS))
    with pytest.raises(Exception) as exc:
        store.register_entity(
            "e1",
            "orders",
            "alice",
            [ColumnDef("id", ColumnType.STRING, nullable=False), ColumnDef("amount", ColumnType.FLOAT)],
        )
    assert_reason(exc, RejectReason.SCHEMA_TYPE_MISMATCH, "type_mismatch", store.current_snapshot())


def test_new_not_null_column_requires_default(store) -> None:
    store.register_entity("e1", "orders", "alice", list(BASE_COLS))
    with pytest.raises(Exception) as exc:
        store.register_entity(
            "e1",
            "orders",
            "alice",
            [*BASE_COLS, ColumnDef("region", ColumnType.STRING, nullable=False)],
        )
    assert_reason(exc, RejectReason.NEW_COLUMN_NO_DEFAULT, "new_column_no_default", store.current_snapshot())


def test_add_nullable_column_is_stable_append(store) -> None:
    store.register_entity("e1", "orders", "alice", list(BASE_COLS))
    result = store.register_entity(
        "e1",
        "orders",
        "alice",
        [*BASE_COLS, ColumnDef("region", ColumnType.STRING, nullable=True)],
        tags={"pii"},
    )
    entity = store.current_snapshot().entities["e1"]
    assert result.evolved is True
    assert entity.column_names() == ("id", "amount", "region")
    assert entity.column("amount").type is ColumnType.FLOAT
    # 新增可空列缺省语义确定：STRING -> None
    assert entity.column("region").effective_default() is None
    # INTEGER/BOOLEAN 历史回填缺省稳定
    assert ColumnType.INTEGER.default_default == 0
    assert ColumnType.BOOLEAN.default_default is False
    log_case(
        "stable_append_column",
        store.current_snapshot(),
        "PASS",
        columns=entity.column_names(),
        effective_default=entity.column("region").effective_default(),
    )


def test_duplicate_column_in_same_request(store) -> None:
    with pytest.raises(Exception) as exc:
        store.register_entity(
            "e1",
            "orders",
            "alice",
            _cols(
                ("id", ColumnType.INTEGER, False, None),
                ("id", ColumnType.STRING, True, None),
            ),
        )
    assert_reason(exc, RejectReason.SCHEMA_DUPLICATE_FIELD, "duplicate_field", store.current_snapshot())
