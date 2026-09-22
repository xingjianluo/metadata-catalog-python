"""本地启动入口：uv run uvicorn main:app --reload"""

from metadata_catalog.api import create_app
from metadata_catalog.store import CatalogStore

store = CatalogStore()
app = create_app(store)
