from sqlalchemy.ext.asyncio import AsyncSession


class SearchService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def reindex(self) -> dict:
        return {"status": "not_implemented", "message": "Meilisearch indexing is deferred to Phase 9"}

    async def search(self, query: str, offset: int = 0, limit: int = 20) -> dict:
        return {"results": [], "total": 0, "message": "Search not yet implemented"}
