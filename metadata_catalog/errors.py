"""可区分的拒绝原因（错误码）与统一异常类型。"""

from __future__ import annotations

from enum import Enum


class RejectReason(str, Enum):
    """登记 / 血缘 / 可见性操作被拒绝的可区分原因。"""

    SCHEMA_MISSING_FIELD = "SCHEMA_MISSING_FIELD"
    SCHEMA_TYPE_MISMATCH = "SCHEMA_TYPE_MISMATCH"
    SCHEMA_IDENTITY_CONFLICT = "SCHEMA_IDENTITY_CONFLICT"
    SCHEMA_DUPLICATE_FIELD = "SCHEMA_DUPLICATE_FIELD"
    NEW_COLUMN_NO_DEFAULT = "NEW_COLUMN_NO_DEFAULT"
    SCHEMA_DROP_FORBIDDEN = "SCHEMA_DROP_FORBIDDEN"
    ENTITY_NOT_FOUND = "ENTITY_NOT_FOUND"
    COLUMN_NOT_FOUND = "COLUMN_NOT_FOUND"
    LINEAGE_DUPLICATE_CONFLICT = "LINEAGE_DUPLICATE_CONFLICT"
    LINEAGE_CYCLE = "LINEAGE_CYCLE"
    UNAUTHORIZED_ENTITY = "UNAUTHORIZED_ENTITY"
    UNAUTHORIZED_LINEAGE = "UNAUTHORIZED_LINEAGE"
    SNAPSHOT_NOT_FOUND = "SNAPSHOT_NOT_FOUND"
    INVALID_CURSOR = "INVALID_CURSOR"
    UNKNOWN_USER = "UNKNOWN_USER"


class CatalogError(Exception):
    """目录服务统一异常，携带机器可判定的拒绝原因与上下文。"""

    def __init__(self, reason: RejectReason, message: str, **context: object) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.context = context

    def to_dict(self) -> dict[str, object]:
        return {
            "reason": self.reason.value,
            "message": self.message,
            "context": self.context,
        }
