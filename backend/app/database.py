from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

# Memory-optimized connection pool for NAS deployment (7.5 GB RAM).
# With 7 concurrent Python processes (backend, 3 download workers, 2 import workers,
# 1 scheduler), a small pool per process prevents exhausting PostgreSQL connections
# and resident memory.  asyncpg defaults (pool_size=10, max_overflow=10) would
# create up to 140 connections (~700 MB–1.4 GB just for connection buffers).
engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_size=3,            # steady-state connections per process  (default 10)
    max_overflow=3,          # burst connections per process         (default 10)
    pool_recycle=3600,       # recycle connections hourly to avoid stale pg connections
    pool_pre_ping=True,      # verify connection is alive before use
    pool_timeout=10,         # fail fast (don't queue) when pool is exhausted
)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncSession:
    async with async_session() as session:
        yield session
