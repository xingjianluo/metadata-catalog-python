"""共享夹具与测试日志工具。"""

import logging
import sys

import pytest

from metadata_catalog.store import CatalogStore

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)


@pytest.fixture
def store() -> CatalogStore:
    return CatalogStore()
