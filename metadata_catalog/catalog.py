"""实体登记与结构演进。

登记规则（失败必须显式拒绝，原因码可区分）：
- ``invalid_identifier``：实体 id/名称为空或非字符串。
- ``invalid_kind``：实体类型不合法。
- ``invalid_field``：字段名重复或为空、字段类型非法。
- ``identifier_conflict``：同一 id 重复登记但 name/kind/owner 不一致。
- 完全一致的重复登记幂等返回既有实体（created=False）。

演进规则（缺省语义确定稳定，且不静默错位既有血缘）：
- 仅允许"新增字段"：新字段排在字段表末尾，不移动既有列序号；
  列级血缘以 (实体 id, 列名) 为锚点，因此既有血缘不受影响。
- 新增同名字段但类型不同 => ``field_type_conflict`` 拒绝。
- 新增同名字段且类型相同 => 幂等无变化。
- ``drop_field``：若该列仍被列级血缘引用，以
  ``field_in_use_by_lineage`` 拒绝；否则删除并推进版本。
- ``change_field_type``：一律以 ``type_change_not_allowed`` 拒绝，
  调用方应新增字段，避免既有下游按旧类型读取而静默错位。

退役（``retire_entity``）：
- 仍挂活跃血缘的实体拒绝退役（``entity_has_active_lineage``）；
- 退役幂等、终态：重复退役不报错也不改状态；退役后同 id 的
  重新登记与字段演进一律以 ``entity_retired`` 拒绝；
- 已退役实体不再出现在发现结果与血缘展开中；直查返回
  ``entity_retired``，与 ``not_found`` / ``permission_denied`` 区分。
"""
from __future__ import annotations

from dataclasses import replace

from .errors import (
    ConflictError,
    NotFoundError,
    RetiredError,
    SchemaEvolutionError,
    ValidationError,
)
from .models import Entity, EntityKind, FieldSpec
from .store import InMemoryStore


class CatalogService:
    def __init__(self, store: InMemoryStore) -> None:
        self.store = store

    # ---------- 登记 ----------
    def register_entity(
        self,
        entity_id: str,
        name: str,
        kind: EntityKind,
        fields: list[FieldSpec],
        tags: set[str] | None,
        owner: str,
    ) -> tuple[Entity, bool]:
        if not isinstance(entity_id, str) or not entity_id.strip():
            raise ValidationError("entity id must be a non-empty string",
                                  reason="invalid_identifier")
        if not isinstance(name, str) or not name.strip():
            raise ValidationError("entity name must be a non-empty string",
                                  reason="invalid_identifier")
        if not isinstance(owner, str) or not owner.strip():
            raise ValidationError("entity owner must be a non-empty string",
                                  reason="invalid_identifier")
        if not isinstance(kind, EntityKind):
            raise ValidationError(f"invalid entity kind: {kind!r}",
                                  reason="invalid_kind")
        normalized_fields = self._validate_fields(fields)
        tag_set = frozenset(tags or set())
        if not all(isinstance(t, str) and t for t in tag_set):
            raise ValidationError("tags must be non-empty strings",
                                  reason="invalid_tags")

        with self.store.transaction():
            snapshot_id = self.store.current_snapshot()
            existing = self.store.get_entity_at(entity_id, snapshot_id)
            if existing is not None:
                if self.store.is_retired_at(entity_id, snapshot_id):
                    # 退役是终态：同 id 不允许通过重新登记复活，
                    # 语义确定稳定（不随登记内容是否一致而摇摆）。
                    raise RetiredError(
                        f"entity {entity_id!r} is retired; re-registration "
                        f"of a retired id is not allowed"
                    )
                # 幂等：定义完全一致才算重复登记
                same = (
                    existing.name == name
                    and existing.kind is kind
                    and existing.owner == owner
                    and existing.fields == tuple(normalized_fields)
                    and existing.tags == tag_set
                )
                if not same:
                    raise SchemaEvolutionError(
                        f"entity {entity_id!r} already exists with a different "
                        f"definition; re-registration is not an update operation",
                        reason="identifier_conflict",
                    )
                return existing, False

            new_snapshot = self.store.begin()
            entity = Entity(
                id=entity_id,
                name=name,
                kind=kind,
                fields=tuple(normalized_fields),
                tags=tag_set,
                owner=owner,
                version=1,
                created_snapshot=new_snapshot,
                updated_snapshot=new_snapshot,
            )
            self.store.put_entity(new_snapshot, entity)
            return entity, True

    @staticmethod
    def _validate_fields(fields: list[FieldSpec]) -> list[FieldSpec]:
        if not isinstance(fields, list):
            raise ValidationError("fields must be a list",
                                  reason="invalid_field")
        seen: set[str] = set()
        normalized: list[FieldSpec] = []
        for f in fields:
            if not isinstance(f, FieldSpec):
                raise ValidationError(
                    f"each field must be FieldSpec, got {type(f).__name__}",
                    reason="invalid_field",
                )
            if f.name in seen:
                raise ValidationError(
                    f"duplicate field name: {f.name!r}",
                    reason="invalid_field",
                )
            seen.add(f.name)
            normalized.append(f)
        return normalized

    # ---------- 演进 ----------
    def add_field(self, entity_id: str, field_spec: FieldSpec) -> tuple[Entity, bool]:
        """新增字段。返回 (实体, 是否发生变化)。"""
        if not isinstance(field_spec, FieldSpec):
            raise ValidationError("field_spec must be a FieldSpec",
                                  reason="invalid_field")
        with self.store.transaction():
            snap = self.store.current_snapshot()
            entity = self.store.get_entity_at(entity_id, snap)
            if entity is None:
                raise NotFoundError(f"entity not found: {entity_id!r}")
            if self.store.is_retired_at(entity_id, snap):
                raise RetiredError(
                    f"entity {entity_id!r} is retired; schema evolution "
                    f"on a retired entity is not allowed"
                )
            current = entity.field_map()
            if field_spec.name in current:
                if current[field_spec.name].type is field_spec.type:
                    return entity, False  # 幂等
                raise SchemaEvolutionError(
                    f"field {field_spec.name!r} exists with type "
                    f"{current[field_spec.name].type.value}; cannot register "
                    f"as {field_spec.type.value}",
                    reason="field_type_conflict",
                )
            new_snap = self.store.begin()
            evolved = replace(
                entity,
                fields=entity.fields + (field_spec,),
                version=entity.version + 1,
                updated_snapshot=new_snap,
            )
            self.store.put_entity(new_snap, evolved)
            return evolved, True

    def drop_field(self, entity_id: str, field_name: str) -> Entity:
        with self.store.transaction():
            snap = self.store.current_snapshot()
            entity = self.store.get_entity_at(entity_id, snap)
            if entity is None:
                raise NotFoundError(f"entity not found: {entity_id!r}")
            if self.store.is_retired_at(entity_id, snap):
                raise RetiredError(
                    f"entity {entity_id!r} is retired; schema evolution "
                    f"on a retired entity is not allowed"
                )
            names = {f.name for f in entity.fields}
            if field_name not in names:
                raise NotFoundError(
                    f"field {field_name!r} not found on {entity_id!r}"
                )
            view = self.store.view_at(snap)
            referenced = any(
                e.level.value == "COLUMN"
                and (
                    (e.source_entity == entity_id and e.source_column == field_name)
                    or (e.target_entity == entity_id and e.target_column == field_name)
                )
                for e in view.edges
            )
            if referenced:
                raise SchemaEvolutionError(
                    f"field {field_name!r} on {entity_id!r} is still referenced "
                    f"by column lineage; remove lineage before dropping",
                    reason="field_in_use_by_lineage",
                )
            new_snap = self.store.begin()
            evolved = replace(
                entity,
                fields=tuple(f for f in entity.fields if f.name != field_name),
                version=entity.version + 1,
                updated_snapshot=new_snap,
            )
            self.store.put_entity(new_snap, evolved)
            return evolved

    def change_field_type(self, entity_id: str, field_spec: FieldSpec) -> Entity:
        raise SchemaEvolutionError(
            f"in-place type change of {field_spec.name!r} on {entity_id!r} "
            f"is not allowed; add a new field instead to keep existing "
            f"lineage and consumers stable",
            reason="type_change_not_allowed",
        )

    # ---------- 退役 ----------
    def retire_entity(self, entity_id: str) -> tuple[Entity, bool, int]:
        """退役实体。返回 (实体, 本次是否新退役, 生效快照号)。

        规则：
        - 实体从未登记：``not_found``；
        - 已退役：幂等返回 (entity, False, 原退役快照号)，不产生新提交；
        - 仍挂有活跃（未撤销）血缘：拒绝 ``entity_has_active_lineage``，
          必须先把血缘全部撤销，绝不留下指向已退役实体的悬空关系；
        - 退役是终态，不提供复活；同 id 的重新登记/演进一律拒绝
          ``entity_retired``。
        """
        with self.store.transaction():
            snap = self.store.current_snapshot()
            entity = self.store.get_entity_at(entity_id, snap)
            if entity is None:
                raise NotFoundError(f"entity not found: {entity_id!r}")
            already = self.store.retirement_snapshot(entity_id)
            if already is not None:
                return entity, False, already
            view = self.store.view_at(snap)
            active = [
                e.id for e in view.edges
                if e.source_entity == entity_id or e.target_entity == entity_id
            ]
            if active:
                raise ConflictError(
                    f"entity {entity_id!r} still has active lineage "
                    f"{sorted(active)}; revoke all lineage before retiring",
                    reason="entity_has_active_lineage",
                )
            new_snap = self.store.begin()
            self.store.retire_entity(new_snap, entity_id)
            return entity, True, new_snap

    def get_entity(self, entity_id: str, snapshot_id: int | None = None) -> Entity:
        snap = snapshot_id if snapshot_id is not None else \
            self.store.current_snapshot()
        entity = self.store.get_entity_at(entity_id, snap)
        if entity is None:
            raise NotFoundError(f"entity not found: {entity_id!r}")
        if self.store.is_retired_at(entity_id, snap):
            raise RetiredError(f"entity retired: {entity_id!r}")
        return entity
