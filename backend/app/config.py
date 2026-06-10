import os
import secrets

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://autogallery:changeme@postgres:5432/autogallery"
    redis_url: str = "redis://:changeme@redis:6379/0"
    meili_url: str = "http://meilisearch:7700"
    meili_master_key: str = ""

    secret_key: str = ""
    admin_password: str = ""
    access_token_expire_minutes: int = 30

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
        if not self.secret_key or self.secret_key == "changeme":
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
        # Validate critical settings
        if not self.secret_key or self.secret_key in ("", "changeme"):
            raise RuntimeError("SECRET_KEY is not set. Define it in .env or ensure /app-config is writable.")
        if not self.admin_password or self.admin_password in ("", "changeme"):
            raise RuntimeError("ADMIN_PASSWORD is not set. Define it in .env or /app-config.")


settings = Settings()
