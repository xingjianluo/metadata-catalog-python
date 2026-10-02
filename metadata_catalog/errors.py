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


class RetiredError(CatalogError):
    """目标实体已退役：与"从未登记"(not_found)、"无权限"(permission_denied)
    明确区分的第三类拒绝理由。"""

    reason = "entity_retired"


class ConflictError(CatalogError):
    """状态冲突：当前操作与已提交状态矛盾（如退役仍挂活跃血缘的实体），
    冲突方必须失败且原因码可区分。"""

    reason = "state_conflict"
