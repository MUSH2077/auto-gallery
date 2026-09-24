import pytest

from app.config import Settings


def _settings_kwargs(**overrides):
    values = {
        "database_url": "postgresql+asyncpg://autogallery:strong-db-password@postgres:5432/autogallery",
        "redis_url": "redis://:strong-redis-password@redis:6379/0",
        "meili_master_key": "strong-meili-master-key",
        "secret_key": "strong-secret-key-for-tests",
        "admin_password": "strong-admin-password",
    }
    values.update(overrides)
    return values


def test_default_bootstrap_admin_password_is_allowed() -> None:
    settings = Settings(**_settings_kwargs(admin_password="change-me-admin"))

    assert settings.admin_password == "change-me-admin"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("secret_key", "change-me-secret-key-at-least-32-chars"),
        ("database_url", "postgresql+asyncpg://autogallery:change-me-postgres@postgres:5432/autogallery"),
        ("redis_url", "redis://:change-me-redis@redis:6379/0"),
        ("meili_master_key", "change-me-meilisearch"),
    ],
)
def test_service_placeholders_are_rejected(field: str, value: str) -> None:
    with pytest.raises(RuntimeError, match="insecure defaults detected"):
        Settings(**_settings_kwargs(**{field: value}))


@pytest.mark.parametrize("admin_password", ["", "changeme", "change-me-anything-else"])
def test_undocumented_admin_placeholder_passwords_are_rejected(admin_password: str) -> None:
    with pytest.raises(RuntimeError, match="ADMIN_PASSWORD"):
        Settings(**_settings_kwargs(admin_password=admin_password))


def test_remote_discovery_settings_defaults_are_really_fail_closed(monkeypatch) -> None:
    """The real Settings defaults must stay closed independent of feature fixtures."""

    fields = (
        "remote_discovery_private_members_enabled",
        "remote_discovery_pixiv_preview_enabled",
        "remote_discovery_pixiv_auto_import_enabled",
        "remote_discovery_x_enabled",
        "remote_discovery_x_auto_import_enabled",
        "remote_discovery_bilibili_enabled",
        "remote_discovery_bilibili_auto_import_enabled",
    )
    for field in fields:
        monkeypatch.delenv(field.upper(), raising=False)

    isolated = Settings(_env_file=None, **_settings_kwargs())

    assert {field: getattr(isolated, field) for field in fields} == {
        field: False for field in fields
    }


def test_default_automatic_memory_reserve_keeps_the_2560_mib_ceiling(monkeypatch) -> None:
    monkeypatch.delenv("RESOURCE_MEMORY_RESERVE_MAX_MB", raising=False)

    isolated = Settings(_env_file=None, **_settings_kwargs())

    assert isolated.resource_memory_reserve_mode == "auto"
    assert isolated.resource_memory_reserve_ratio == 0.15
    assert isolated.resource_memory_reserve_min_mb == 384
    assert isolated.resource_memory_reserve_max_mb == 2560


def test_gitllery_active_mode_requires_matching_verified_generation() -> None:
    with pytest.raises(RuntimeError, match="GITLLERY_ACTIVE_VERIFIED_GENERATION"):
        Settings(
            **_settings_kwargs(
                gitllery_projection_mode="active",
                gitllery_build_generation="next-r1",
            )
        )

    isolated = Settings(
        **_settings_kwargs(
            gitllery_projection_mode="active",
            gitllery_build_generation="next-r1",
            gitllery_active_verified_generation="next-r1",
        )
    )
    assert isolated.gitllery_projection_mode == "active"
