"""测试公共夹具与判定日志辅助。

每个用例通过 ``judge`` 记录：输入摘要、快照标识、判定结论与依据，
失败时断言信息本身也携带原因码，保证"失败原因可区分、断言有依据"。
"""
from __future__ import annotations

import logging

import pytest

from metadata_catalog.service import MetadataCatalog

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
case_logger = logging.getLogger("catalog-test")


def log_judgment(case: str, inp: object, snapshot_id: object,
                 verdict: str, basis: str) -> None:
    case_logger.info(
        "CASE=%s | INPUT=%r | SNAPSHOT=%s | VERDICT=%s | BASIS=%s",
        case, inp, snapshot_id, verdict, basis,
    )


@pytest.fixture
def svc() -> MetadataCatalog:
    return MetadataCatalog()
