from ._routers import router, curation_ops_router, tasks_ops_router
from . import settings, data, scheduler, gallerydl, dedup, backup, auth_health  # noqa: F401
from .gallerydl import (  # Backward-compatible public imports for callers/tests.
    GalleryDLMultiConfig,
    PixivSourceConfig,
    _rebuild_managed_postprocessors,
    get_effective_gallerydl_config,
    update_gallerydl_config,
)
from app.services.admin_data import ENTITIES, clear_entity_data

# Compatibility surface retained after the former monolithic admin module was
# split into focused modules. Runtime routes use the submodule functions
# directly; these wrappers keep maintenance scripts and older integrations
# working without reintroducing duplicate routes.
_clear_files = data._clear_files
BACKUP_DIR = backup.BACKUP_DIR
_pg_env_with_passfile = backup._pg_env_with_passfile
_safe_extract_tar = backup._safe_extract_tar


async def clear_entity(entity: str, db):
    from app.services import admin_data

    admin_data._clear_files = _clear_files
    return await clear_entity_data(entity, db)


def _validate_backup_filename(filename: str):
    backup.BACKUP_DIR = BACKUP_DIR
    return backup._validate_backup_filename(filename)
