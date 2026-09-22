"""测试公共工具：统一打印输入、快照标识与判定结果。"""

from __future__ import annotations

import logging
from typing import Any

from metadata_catalog.errors import CatalogError, RejectReason

log = logging.getLogger("catalog-test")


def log_case(case: str, snapshot: Any, verdict: str, **detail: Any) -> None:
    snap_id = getattr(snapshot, "version", snapshot)
    log.info(
        "case=%s snapshot=%s verdict=%s detail=%s",
        case,
        snap_id,
        verdict,
        detail,
    )


def assert_reason(exc_info, reason: RejectReason, case: str, snapshot: Any) -> None:
    err: CatalogError = exc_info.value
    assert err.reason == reason, (
        f"{case}: expected reject reason {reason}, got {err.reason}; "
        f"context={err.context}"
    )
    log_case(
        case,
        snapshot,
        f"REJECTED:{reason.value}",
        message=err.message,
        context=err.context,
    )
