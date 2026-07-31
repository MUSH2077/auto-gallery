from urllib.parse import urlparse

from pydantic_settings import BaseSettings

DEFAULT_ADMIN_PASSWORD = "change-me-admin"


def _is_placeholder(value: str | None) -> bool:
    if not value:
        return False
    normalized = value.strip().lower()
    return normalized == "changeme" or normalized.startswith("change-me")


def _is_allowed_bootstrap_admin_password(value: str | None) -> bool:
    if not value:
        return False
    normalized = value.strip().lower()
    return normalized == DEFAULT_ADMIN_PASSWORD


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://autogallery:changeme@postgres:5432/autogallery"
    redis_url: str = "redis://:changeme@redis:6379/0"
    meili_url: str = "http://meilisearch:7700"
    meili_master_key: str = ""
    meili_index_prefix: str = ""

    secret_key: str = ""
    admin_password: str = ""
    access_token_expire_minutes: int = 10080  # 7 days (NAS single-user)

    download_root: str = "/downloads"
    library_root: str = "/library"
    gallerydl_config_root: str = "/gallerydl-config"
    app_config_root: str = "/app-config"

    cors_origins: str = "http://localhost:13000"
    log_level: str = "INFO"

    timezone: str = "UTC"
    min_download_free_gb: float = 5.0
    max_pending_artifacts: int = 50000

    # DB connection pool, per Python process. Defaults are worker-sized (a worker
    # handles one job at a time). The backend serves concurrent HTTP and overrides
    # these via env (DB_POOL_SIZE / DB_MAX_OVERFLOW) — the old 2+2 ceiling caused
    # QueuePool timeouts under normal dashboard fan-out. Worst-case connections =
    # (pool + overflow) x processes, kept well under postgres max_connections (100).
    db_pool_size: int = 2
    db_max_overflow: int = 3
    db_pool_timeout: int = 10

    # Memory monitor: log backend RSS every N seconds, WARN past the threshold.
    # Set memory_warn_mb below the container mem_limit so the logs capture the
    # climb (and the last activity) BEFORE the OOM killer fires.
    memory_warn_mb: int = 700
    memory_log_interval_seconds: int = 60

    model_config = {"env_prefix": "", "case_sensitive": False}

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Validate critical settings aren't at insecure defaults
        errors: list[str] = []

        if not self.secret_key or _is_placeholder(self.secret_key):
            errors.append("SECRET_KEY is not set or is still a factory placeholder.")
        if not self.admin_password or self.admin_password.strip().lower() == "changeme":
            errors.append(
                f"ADMIN_PASSWORD is not set. Use {DEFAULT_ADMIN_PASSWORD!r} for first login "
                "or set a custom initial password."
            )
        elif _is_placeholder(self.admin_password) and not _is_allowed_bootstrap_admin_password(self.admin_password):
            errors.append(
                f"ADMIN_PASSWORD may only use the documented bootstrap default "
                f"{DEFAULT_ADMIN_PASSWORD!r}; replace other placeholder values."
            )
        if _is_placeholder(self.meili_master_key):
            errors.append("MEILI_MASTER_KEY is still a factory placeholder.")
        if len(self.meili_index_prefix) > 64 or any(
            not (character.isalnum() or character in {"_", "-"})
            for character in self.meili_index_prefix
        ):
            errors.append(
                "MEILI_INDEX_PREFIX may contain only letters, numbers, '_' and '-' "
                "and must be at most 64 characters."
            )

        # Check DB/Redis passwords — refuse to start with factory defaults.
        # Parse the password component from each URL to avoid false positives
        # when "changeme" appears as part of a longer legitimate password.
        for label, url in [("DATABASE_URL", self.database_url), ("REDIS_URL", self.redis_url)]:
            try:
                parsed = urlparse(url)
                pwd = parsed.password or ""
                if _is_placeholder(pwd):
                    errors.append(
                        f"{label} password is still a factory placeholder. "
                        "Copy .env.example to .env and replace the placeholder."
                    )
            except Exception:
                pass

        if errors:
            msg = (
                "\n============================================================\n"
                "  auto-gallery refused to start — insecure defaults detected\n"
                "============================================================\n\n"
                "  The following settings are still at their factory defaults:\n\n"
            )
            for e in errors:
                msg += f"    • {e}\n"
            msg += (
                "\n  Create a .env file in the project root with real values:\n\n"
                "    cp .env.example .env\n"
                "    # then edit .env and replace service secrets such as SECRET_KEY,\n"
                "    # POSTGRES_PASSWORD, REDIS_PASSWORD, and MEILI_MASTER_KEY.\n"
                f"    # ADMIN_PASSWORD={DEFAULT_ADMIN_PASSWORD} is allowed only for first login.\n\n"
                "  See docs/setup.md for detailed instructions.\n"
                "============================================================\n"
            )
            raise RuntimeError(msg)


settings = Settings()
