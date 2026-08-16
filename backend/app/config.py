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
    meili_search_timeout_seconds: float = 3.0

    secret_key: str = ""
    admin_password: str = ""
    access_token_expire_minutes: int = 10080  # 7 days (NAS single-user)
    media_playback_ttl_seconds: int = 7200

    download_root: str = "/downloads"
    library_root: str = "/library"
    gallerydl_config_root: str = "/gallerydl-config"
    app_config_root: str = "/app-config"

    cors_origins: str = "http://localhost:13000"
    log_level: str = "INFO"

    timezone: str = "UTC"
    min_download_free_gb: float = 5.0
    max_pending_artifacts: int = 50000

    # Project-local download ceiling. The database value remains the user's
    # desired concurrency; this cap prevents a stale/high UI setting from
    # recreating a download storm on any device class.
    download_concurrency_cap: int = 1
    download_queue_max_pending: int = 100
    download_dispatch_recovery_interval_seconds: float = 60.0
    download_dispatch_recovery_grace_seconds: float = 180.0
    download_dispatch_recovery_batch_size: int = 10

    # Host resource-pressure guard.  /proc/meminfo and /proc/pressure expose
    # host-wide values inside the Linux containers used by this deployment, so
    # no Docker socket or privileged API is required.
    resource_pressure_sample_interval_seconds: float = 5.0
    # Auto mode scales the host reserve to the device instead of assuming an
    # 8 GiB NAS. Fixed mode retains an explicit operator override.
    resource_memory_reserve_mode: str = "auto"  # auto | fixed
    resource_memory_reserve_ratio: float = 0.15
    resource_memory_reserve_min_mb: int = 384
    resource_memory_reserve_max_mb: int = 1280
    resource_pressure_warning_available_mb: int = 1536
    resource_pressure_pause_available_mb: int = 1280
    resource_pressure_resume_available_mb: int = 1792
    resource_pressure_warning_swap_free_ratio: float = 0.30
    resource_pressure_pause_swap_free_ratio: float = 0.25
    resource_pressure_resume_swap_free_ratio: float = 0.30
    # Swap occupancy is sticky and, by itself, is not proof of current memory
    # pressure.  A very low free ratio is still an emergency; above that floor
    # the controller requires active swap traffic before stopping heavy work.
    resource_pressure_critical_swap_free_ratio: float = 0.15
    resource_pressure_swap_activity_mb_per_minute: float = 64.0
    resource_pressure_pause_memory_psi_full_avg10: float = 2.0
    resource_pressure_pause_io_psi_full_avg10: float = 15.0
    resource_pressure_resume_memory_psi_full_avg10: float = 1.0
    resource_pressure_resume_io_psi_full_avg10: float = 5.0
    resource_pressure_pause_samples: int = 3
    resource_pressure_failure_samples: int = 3
    resource_pressure_resume_seconds: float = 60.0
    resource_pressure_snapshot_ttl_seconds: int = 40
    # Soft PSI pressure controls throughput using additive-increase,
    # multiplicative-decrease.  It never overrides the absolute memory reserve.
    resource_budget_min_scale: float = 0.10
    resource_budget_decrease_factor: float = 0.50
    resource_budget_increase_step: float = 0.10
    resource_budget_increase_stable_seconds: float = 60.0
    resource_governance_max_scale: float = 1.0
    resource_budget_base_read_mb_per_second: float = 20.0
    resource_budget_base_write_mb_per_second: float = 10.0
    resource_governance_mode: str = "shadow"  # shadow | enforce
    resource_device_profile: str = "auto"  # auto | compact | standard | performance
    # Optional rollout allowlist for soft AIMD enforcement. An empty value in
    # enforce mode preserves the historical all-profiles behavior; deploy
    # tooling always sets an explicit list during staged rollout.
    resource_governance_enforced_profiles: str = ""
    resource_foreground_p95_limit_ms: float = 500.0
    resource_foreground_slow_samples: int = 3
    resource_baseline_memory_psi_margin: float = 0.5
    resource_baseline_io_psi_margin: float = 3.0
    resource_baseline_memory_psi_cap: float = 5.0
    resource_baseline_io_psi_cap: float = 30.0
    scheduler_scan_batch_size: int = 100
    task_reconciliation_interval_seconds: float = 60.0
    task_compaction_interval_seconds: float = 300.0
    task_compaction_batch_size: int = 200
    task_compaction_enabled: bool = True
    # Gitllery remains product v1. This selects the side-by-side segment
    # rollout, not a product version: shadow writes .gitllery.build-<generation>
    # while active writes the atomically promoted .gitllery directory.
    gitllery_projection_mode: str = "shadow"  # shadow | active
    gitllery_build_generation: str = "segment-r1"

    # DB connection pool, per Python process. Defaults are worker-sized (a worker
    # handles one job at a time). The backend serves concurrent HTTP and overrides
    # these via env (DB_POOL_SIZE / DB_MAX_OVERFLOW). Worst-case connections =
    # (pool + overflow) x processes, kept under postgres max_connections (40).
    db_pool_size: int = 1
    db_max_overflow: int = 1
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
        if self.gitllery_projection_mode.strip().lower() not in {
            "shadow",
            "active",
        }:
            errors.append(
                "GITLLERY_PROJECTION_MODE must be shadow or active."
            )
        if self.resource_governance_mode.strip().lower() not in {
            "shadow",
            "enforce",
        }:
            errors.append(
                "RESOURCE_GOVERNANCE_MODE must be shadow or enforce."
            )
        if self.resource_device_profile.strip().lower() not in {
            "auto",
            "compact",
            "standard",
            "performance",
        }:
            errors.append(
                "RESOURCE_DEVICE_PROFILE must be auto, compact, standard, or performance."
            )
        if self.resource_memory_reserve_mode.strip().lower() not in {
            "auto",
            "fixed",
        }:
            errors.append(
                "RESOURCE_MEMORY_RESERVE_MODE must be auto or fixed."
            )
        if not 0.01 <= self.resource_memory_reserve_ratio <= 0.90:
            errors.append(
                "RESOURCE_MEMORY_RESERVE_RATIO must be between 0.01 and 0.90."
            )
        if not (
            128 <= self.resource_memory_reserve_min_mb
            <= self.resource_memory_reserve_max_mb
        ):
            errors.append(
                "RESOURCE_MEMORY_RESERVE_MIN_MB must be at least 128 and no "
                "larger than RESOURCE_MEMORY_RESERVE_MAX_MB."
            )
        if not 0.01 <= self.resource_governance_max_scale <= 1.0:
            errors.append(
                "RESOURCE_GOVERNANCE_MAX_SCALE must be between 0.01 and 1.0."
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
