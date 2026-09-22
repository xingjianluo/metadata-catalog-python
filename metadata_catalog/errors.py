"""可区分失败原因的异常类型（仅骨架定义）。"""
from __future__ import annotations


class CatalogError(Exception):
    """所有目录错误的基类，携带可区分的原因码。"""

    reason = "catalog_error"

    def __init__(self, message: str = "", *, reason: str | None = None) -> None:
        super().__init__(message)
        if reason is not None:
            self.reason = reason


class ValidationError(CatalogError):
    """登记/演进入参校验失败。"""

    reason = "validation_error"


class SchemaEvolutionError(CatalogError):
    """结构演进不兼容。"""

    reason = "schema_evolution_rejected"


class NotFoundError(CatalogError):
    """实体或血缘不存在。"""

    reason = "not_found"


class PermissionDeniedError(CatalogError):
    """使用者对目标无可见性，越权访问必须明确拒绝。"""

    reason = "permission_denied"


class LineageError(CatalogError):
    """血缘建立失败。"""

    reason = "lineage_error"
