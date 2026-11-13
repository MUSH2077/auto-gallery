"""Backend selection and durable-command ownership at search cutover."""
from sqlalchemy import exists, or_, select
from app.config import settings


class SearchBackendUnavailable(RuntimeError):
    """A required search index cannot serve a request."""


class SearchBackendMismatch(Exception):
    """An unfinished command still belongs to the previous backend."""


def require_backend(command):
    backend = getattr(command, "backend", None) or "meilisearch"
    if backend != settings.search_backend:
        raise SearchBackendMismatch(
            f"Unfinished search command belongs to {backend}; drain it using that backend before selecting {settings.search_backend}"
        )


async def compatible_commands(db) -> bool:
    from app.models.search_delivery_receipt import SearchDeliveryReceipt as Receipt
    from app.models.search_rebuild import SearchRebuild as Build
    receipt = exists().where(Receipt.state.not_in(("complete", "failed")), Receipt.backend != settings.search_backend)
    rebuild = exists().where(Build.state.not_in(("complete", "failed")), Build.backend != settings.search_backend)
    return not bool(await db.scalar(select(or_(receipt, rebuild))))


async def postgres_search_ready(db) -> bool:
    from sqlalchemy import text
    row = (await db.execute(text("SELECT to_regclass('search_pg_indexes'), to_regclass('search_pg_documents'), to_regclass('search_pg_tasks'), to_regprocedure('levenshtein_less_equal(text,text,integer)')"))).one()
    return all(row) and await compatible_commands(db)
