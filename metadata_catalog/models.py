"""领域模型：元数据实体、字段、血缘边与使用者。

设计要点：
- 所有模型不可变（frozen dataclass），状态变更只能产生新版本，
  这样快照视图可以直接共享对象引用而无需防御性拷贝。
- 血缘边的唯一标识由其五元组（级别、源实体、源列、目标实体、目标列）
  确定性派生，保证重复登记幂等。
"""
from __future__ import annotations

import enum
from dataclasses import dataclass


class FieldType(str, enum.Enum):
    STRING = "STRING"
    INTEGER = "INTEGER"
    BOOLEAN = "BOOLEAN"
    DOUBLE = "DOUBLE"
    TIMESTAMP = "TIMESTAMP"


class EntityKind(str, enum.Enum):
    TABLE = "TABLE"
    VIEW = "VIEW"
    STREAM = "STREAM"


class LineageLevel(str, enum.Enum):
    TABLE = "TABLE"
    COLUMN = "COLUMN"


@dataclass(frozen=True)
class FieldSpec:
    name: str
    type: FieldType

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("field name must be a non-empty string")
        if not isinstance(self.type, FieldType):
            raise TypeError("field type must be a FieldType")


@dataclass(frozen=True)
class Entity:
    id: str
    name: str
    kind: EntityKind
    fields: tuple[FieldSpec, ...]
    tags: frozenset[str]
    owner: str
    version: int
    created_snapshot: int
    updated_snapshot: int

    def field_map(self) -> dict[str, FieldSpec]:
        return {f.name: f for f in self.fields}


@dataclass(frozen=True)
class LineageEdge:
    id: str
    level: LineageLevel
    source_entity: str
    source_column: str | None
    target_entity: str
    target_column: str | None
    transformation: str
    created_snapshot: int

    @staticmethod
    def make_id(
        level: LineageLevel,
        source_entity: str,
        source_column: str | None,
        target_entity: str,
        target_column: str | None,
    ) -> str:
        if level is LineageLevel.TABLE:
            return f"edge:TABLE:{source_entity}->{target_entity}"
        return (
            f"edge:COLUMN:{source_entity}.{source_column}"
            f"->{target_entity}.{target_column}"
        )


@dataclass(frozen=True)
class User:
    name: str
    is_admin: bool = False
