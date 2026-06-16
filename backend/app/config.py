import os
import secrets
from urllib.parse import urlparse

from pydantic_settings import BaseSettings


def _is_placeholder(value: str | None) -> bool:
    if not value:
        return False
    normalized = value.strip().lower()
    return normalized == "changeme" or normalized.startswith("change-me")


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://autogallery:changeme@postgres:5432/autogallery"
    redis_url: str = "redis://:changeme@redis:6379/0"
    meili_url: str = "http://meilisearch:7700"
    meili_master_key: str = ""

    secret_key: str = ""
    admin_password: str = ""
    access_token_expire_minutes: int = 480  # 8 hours (NAS LAN environment)

    download_root: str = "/downloads"
    library_root: str = "/library"
    gallerydl_config_root: str = "/gallerydl-config"
    app_config_root: str = "/app-config"

    cors_origins: str = "http://localhost:13000"
    log_level: str = "INFO"

    timezone: str = "UTC"

    model_config = {"env_prefix": "", "case_sensitive": False}

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Generate a persistent SECRET_KEY on first run so JWT signing is
        # secure from the very first request, before the lifespan runs.
        if not self.secret_key or self.secret_key.strip().lower() == "changeme":
            key_dir = self.app_config_root
            key_file = os.path.join(key_dir, "secret_key")
            try:
                if os.path.exists(key_file):
                    with open(key_file) as f:
                        self.secret_key = f.read().strip()
                else:
                    os.makedirs(key_dir, exist_ok=True)
                    self.secret_key = secrets.token_hex(32)
                    with open(key_file, "w") as f:
                        f.write(self.secret_key)
            except OSError:
                if not self.secret_key:
                    self.secret_key = secrets.token_hex(32)
        # Validate critical settings aren't at insecure defaults
        errors: list[str] = []

        if not self.secret_key or _is_placeholder(self.secret_key):
            errors.append("SECRET_KEY is not set or is still a factory placeholder.")
        if not self.admin_password or _is_placeholder(self.admin_password):
            errors.append("ADMIN_PASSWORD is not set or is still a factory placeholder.")
        if _is_placeholder(self.meili_master_key):
            errors.append("MEILI_MASTER_KEY is still a factory placeholder.")

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
                "    # then edit .env and replace all 'change-me-*' placeholders\n\n"
                "  See docs/setup.md for detailed instructions.\n"
                "============================================================\n"
            )
            raise RuntimeError(msg)


settings = Settings()
