"""元数据模型：实体、字段、血缘边、快照与查询结果的数据结构。

所有模型均为不可变值对象；目录状态的每次提交都会生成一个新版本的
``Snapshot``，读写双方通过快照版本号引用同一份结论，保证可复现。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class ColumnType(str, Enum):
    STRING = "STRING"
    INTEGER = "INTEGER"
    BOOLEAN = "BOOLEAN"
    FLOAT = "FLOAT"
    TIMESTAMP = "TIMESTAMP"

    @property
    def default_default(self) -> Any:
        """新增字段在历史数据上的稳定缺省语义。"""
        if self is ColumnType.BOOLEAN:
            return False
        if self is ColumnType.INTEGER:
            return 0
        if self is ColumnType.FLOAT:
            return 0.0
        return None


@dataclass(frozen=True)
class ColumnDef:
    """列定义。结构演进中列名即稳定标识，类型与可空性一经登记不可改变。"""

    name: str
    type: ColumnType
    nullable: bool = True
    default: Any | None = None

    def effective_default(self) -> Any | None:
        """该列在历史行上回填时使用的确定缺省值。"""
        return self.default if self.default is not None else self.type.default_default


@dataclass(frozen=True)
class Entity:
    """元数据实体。

    ``name`` 与 ``owner`` 为身份属性，重复登记时不允许变化；
    ``columns`` 只允许尾部追加新列；``tags`` 允许在重复登记时合并演进。
    """

    id: str
    name: str
    owner: str
    columns: tuple[ColumnDef, ...]
    tags: frozenset[str] = frozenset()
    created_snapshot: int = 0
    updated_snapshot: int = 0

    def column_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)

    def column(self, name: str) -> ColumnDef | None:
        for c in self.columns:
            if c.name == name:
                return c
        return None


@dataclass(frozen=True)
class LineageEdge:
    """血缘边。

    ``source_col``/``target_col`` 均为 ``None`` 时为表级血缘，
    否则为列级血缘。同一 key 的重复登记幂等；同表级/列级同端点但
    另一粒度不同的边视为标识冲突。
    """

    source_entity: str
    target_entity: str
    source_col: str | None = None
    target_col: str | None = None
    created_snapshot: int = 0

    @property
    def is_column_level(self) -> bool:
        return self.source_col is not None or self.target_col is not None

    @property
    def key(self) -> tuple[str, str, str | None, str | None]:
        return (self.source_entity, self.target_entity, self.source_col, self.target_col)

    @property
    def table_key(self) -> tuple[str, str]:
        return (self.source_entity, self.target_entity)


@dataclass(frozen=True)
class Snapshot:
    """某个提交时刻的不可变目录快照。

    ``entities`` / ``edges`` / ``acl`` 均为冻结映射/序列，提交后不允许变更。
    """

    version: int
    entities: dict[str, Entity]
    edges: tuple[LineageEdge, ...]
    acl: dict[str, frozenset[str]]


@dataclass(frozen=True)
class Page:
    """分页结果。``next_cursor`` 内嵌快照版本，翻页永远锁定同一快照。"""

    items: tuple[Entity, ...]
    next_cursor: str | None
    snapshot_version: int


@dataclass(frozen=True)
class ImpactResult:
    """影响分析结论：可达节点、参与边与明确标记出的环路。"""

    root: str
    direction: str
    nodes: tuple[str, ...]
    edges: tuple[LineageEdge, ...]
    cycles: tuple[tuple[str, ...], ...]
    snapshot_version: int


@dataclass(frozen=True)
class RegisterResult:
    entity_id: str
    snapshot_version: int
    evolved: bool
