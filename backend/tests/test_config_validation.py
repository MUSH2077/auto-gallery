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
