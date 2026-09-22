"""实体登记幂等、拒绝原因可区分、结构演进兼容性。"""
from __future__ import annotations

import pytest

from metadata_catalog.errors import (
    NotFoundError,
    SchemaEvolutionError,
    ValidationError,
)
from metadata_catalog.models import EntityKind, FieldSpec, FieldType

from .conftest import log_judgment


def _fields(*pairs):
    return [FieldSpec(n, t) for n, t in pairs]


def test_register_is_idempotent_for_identical_definition(svc):
    c = svc.catalog
    e1, created1 = c.register_entity(
        "t1", "T1", EntityKind.TABLE,
        _fields(("a", FieldType.STRING)), {"pii"}, "alice",
    )
    e2, created2 = c.register_entity(
        "t1", "T1", EntityKind.TABLE,
        _fields(("a", FieldType.STRING)), {"pii"}, "alice",
    )
    log_judgment(
        "register-idempotent",
        {"id": "t1", "twice": True}, e2.updated_snapshot,
        "ACCEPTED_IDEMPOTENT",
        "created flag False on second identical call and entity unchanged",
    )
    assert created1 is True, "first registration must create"
    assert created2 is False, "identical re-registration must be idempotent"
    assert e1 == e2, "idempotent call must return the same entity version"


@pytest.mark.parametrize(
    "case, kwargs, expected_reason",
    [
        ("empty-id",
         dict(entity_id=" ", name="N", kind=EntityKind.TABLE, fields=[],
              tags=None, owner="a"),
         "invalid_identifier"),
        ("empty-owner",
         dict(entity_id="t", name="N", kind=EntityKind.TABLE, fields=[],
              tags=None, owner=""),
         "invalid_identifier"),
        ("bad-kind",
         dict(entity_id="t", name="N", kind="TABLE", fields=[],
              tags=None, owner="a"),
         "invalid_kind"),
        ("duplicate-field",
         dict(entity_id="t", name="N", kind=EntityKind.TABLE,
              fields=_fields(("a", FieldType.STRING), ("a", FieldType.INTEGER)),
              tags=None, owner="a"),
         "invalid_field"),
        ("field-not-fieldspec",
         dict(entity_id="t", name="N", kind=EntityKind.TABLE,
              fields=[("a", "STRING")], tags=None, owner="a"),
         "invalid_field"),
    ],
)
def test_register_rejections_are_distinguishable(svc, case, kwargs, expected_reason):
    with pytest.raises(ValidationError) as exc_info:
        svc.catalog.register_entity(**kwargs)
    log_judgment(
        f"register-reject/{case}", kwargs, svc.store.current_snapshot(),
        f"REJECTED:{exc_info.value.reason}",
        f"expect reason == {expected_reason}",
    )
    assert exc_info.value.reason == expected_reason, (
        f"{case}: expected {expected_reason}, got {exc_info.value.reason}"
    )


def test_re_registration_with_different_definition_conflicts(svc):
    c = svc.catalog
    c.register_entity("t1", "T1", EntityKind.TABLE, [], None, "alice")
    with pytest.raises(SchemaEvolutionError) as exc_info:
        c.register_entity("t1", "T1-CHANGED", EntityKind.TABLE, [], None, "alice")
    log_judgment(
        "register-identifier-conflict",
        {"id": "t1", "name": "T1-CHANGED"}, svc.store.current_snapshot(),
        f"REJECTED:{exc_info.value.reason}",
        "same id with different name must not silently update",
    )
    assert exc_info.value.reason == "identifier_conflict"
    # 冲突不改变既有状态
    assert c.get_entity("t1").name == "T1"


def test_add_field_appends_and_keeps_existing_column_positions(svc):
    c = svc.catalog
    c.register_entity(
        "t1", "T1", EntityKind.TABLE,
        _fields(("a", FieldType.STRING)), None, "alice",
    )
    evolved, changed = c.add_field("t1", FieldSpec("b", FieldType.INTEGER))
    log_judgment(
        "add-field", {"entity": "t1", "field": "b:INTEGER"},
        evolved.updated_snapshot, "ACCEPTED",
        "new field appended at tail; existing order preserved; version +1",
    )
    assert changed is True
    assert [f.name for f in evolved.fields] == ["a", "b"]
    assert [f.type for f in evolved.fields] == [FieldType.STRING, FieldType.INTEGER]
    assert evolved.version == 2


def test_add_same_field_is_idempotent(svc):
    c = svc.catalog
    c.register_entity("t1", "T1", EntityKind.TABLE,
                      _fields(("a", FieldType.STRING)), None, "alice")
    _, changed = c.add_field("t1", FieldSpec("a", FieldType.STRING))
    assert changed is False


def test_add_field_with_conflicting_type_rejected(svc):
    c = svc.catalog
    c.register_entity("t1", "T1", EntityKind.TABLE,
                      _fields(("a", FieldType.STRING)), None, "alice")
    with pytest.raises(SchemaEvolutionError) as exc_info:
        c.add_field("t1", FieldSpec("a", FieldType.INTEGER))
    log_judgment(
        "add-field-type-conflict", {"entity": "t1", "field": "a:INTEGER"},
        svc.store.current_snapshot(), f"REJECTED:{exc_info.value.reason}",
        "existing column typed STRING cannot be re-added as INTEGER",
    )
    assert exc_info.value.reason == "field_type_conflict"
    assert c.get_entity("t1").fields[0].type is FieldType.STRING


def test_change_type_always_rejected(svc):
    c = svc.catalog
    c.register_entity("t1", "T1", EntityKind.TABLE,
                      _fields(("a", FieldType.STRING)), None, "alice")
    with pytest.raises(SchemaEvolutionError) as exc_info:
        c.change_field_type("t1", FieldSpec("a", FieldType.INTEGER))
    assert exc_info.value.reason == "type_change_not_allowed"
    log_judgment(
        "change-type", {"entity": "t1", "field": "a"},
        svc.store.current_snapshot(), f"REJECTED:{exc_info.value.reason}",
        "in-place type change forbidden to protect downstream consumers",
    )


def test_drop_field_blocked_when_lineage_references_it(svc):
    c, lg = svc.catalog, svc.lineage
    c.register_entity("t1", "T1", EntityKind.TABLE,
                      _fields(("a", FieldType.STRING)), None, "alice")
    c.register_entity("t2", "T2", EntityKind.TABLE,
                      _fields(("b", FieldType.STRING)), None, "alice")
    from metadata_catalog.models import LineageLevel
    lg.add_lineage("t1", "t2", LineageLevel.COLUMN, "a", "b")
    with pytest.raises(SchemaEvolutionError) as exc_info:
        c.drop_field("t1", "a")
    assert exc_info.value.reason == "field_in_use_by_lineage"
    log_judgment(
        "drop-field-in-use", {"entity": "t1", "field": "a"},
        svc.store.current_snapshot(), f"REJECTED:{exc_info.value.reason}",
        "column lineage still references the column",
    )


def test_drop_field_allowed_after_lineage_gone(svc):
    # 删除引用该列的边在本系统中不提供（血缘只增不改），
    # 因此用一个从未被血缘引用的字段验证正常删除路径。
    c = svc.catalog
    c.register_entity("t1", "T1", EntityKind.TABLE,
                      _fields(("a", FieldType.STRING),
                              ("tmp", FieldType.STRING)), None, "alice")
    evolved = c.drop_field("t1", "tmp")
    assert [f.name for f in evolved.fields] == ["a"]
    assert evolved.version == 2
    log_judgment(
        "drop-field-ok", {"entity": "t1", "field": "tmp"},
        evolved.updated_snapshot, "ACCEPTED",
        "field without lineage references dropped; version +1",
    )


def test_get_unknown_entity_is_not_found(svc):
    with pytest.raises(NotFoundError) as exc_info:
        svc.catalog.get_entity("ghost")
    assert exc_info.value.reason == "not_found"
