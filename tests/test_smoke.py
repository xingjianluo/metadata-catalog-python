"""骨架冒烟测试：仅验证包可导入、门面可实例化。"""
from metadata_catalog.service import MetadataCatalog


def test_skeleton_boots() -> None:
    svc = MetadataCatalog()
    assert svc is not None
