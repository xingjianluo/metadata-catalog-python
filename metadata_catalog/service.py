"""组合门面：把目录、血缘、可见性、发现装配在一起。"""
from __future__ import annotations

from .catalog import CatalogService
from .discovery import DiscoveryService
from .lineage import LineageService
from .store import InMemoryStore
from .visibility import VisibilityService


class MetadataCatalog:
    def __init__(self) -> None:
        self.store = InMemoryStore()
        self.visibility = VisibilityService(self.store)
        self.catalog = CatalogService(self.store)
        self.lineage = LineageService(self.store, self.visibility)
        self.discovery = DiscoveryService(self.store, self.visibility)
