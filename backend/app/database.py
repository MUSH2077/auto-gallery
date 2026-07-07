from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

# Connection pool sized per Python process, configured via env so the backend
# (concurrent HTTP) can run a larger pool than the workers (one job at a time).
# Defaults are worker-sized; the backend service sets DB_POOL_SIZE/DB_MAX_OVERFLOW
# higher in docker-compose. A too-small backend pool (was 2+2) caused
# QueuePool timeouts under normal dashboard request fan-out.
# Worst-case connections = (pool_size + max_overflow) x processes; kept well
# under postgres max_connections (100).
engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_recycle=3600,       # recycle connections hourly to avoid stale pg connections
    pool_pre_ping=True,      # verify connection is alive before use
    pool_timeout=settings.db_pool_timeout,  # fail fast when pool is exhausted
)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncSession:
    async with async_session() as session:
        yield session
