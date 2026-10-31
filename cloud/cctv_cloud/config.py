from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_SECRET = "dev-only-insecure-secret-key-change-me"  # noqa: S105 - rejected outside dev/test


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CCTV_", env_file=".env", extra="ignore")

    environment: Literal["dev", "test", "staging", "production"] = "dev"
    # The API connects as the restricted `cctv_app` role, which is subject to row-level security.
    database_url: str = "postgresql+psycopg://cctv_app:cctv_app@localhost:5432/cctv"
    database_connect_timeout_seconds: int = 5
    # Migrations and system jobs connect as the table owner.
    migration_database_url: str = "postgresql+psycopg://cctv_owner:cctv_owner@localhost:5432/cctv"
    # Role that migrations grant table access to (must already exist; see infra/postgres/init).
    app_db_role: str = "cctv_app"

    # Signs access/refresh tokens and encrypts TOTP secrets. 32+ random bytes in production.
    secret_key: SecretStr = SecretStr(DEV_SECRET)
    access_token_minutes: int = 15
    # Sessions end after this long without use, and after session_max_days regardless.
    session_idle_minutes: int = 8 * 60
    session_max_days: int = 7
    login_max_failures: int = 5
    login_lockout_minutes: int = 15
    password_reset_minutes: int = 60
    # Base URL of the web app, for links in emails (password reset, invitations).
    web_url: str = "http://localhost:3000"

    # S3-compatible object storage for clips and snapshots (MinIO locally).
    s3_endpoint_url: str | None = "http://localhost:9000"
    s3_region: str = "us-east-1"
    s3_bucket: str = "cctv-media"
    s3_access_key: SecretStr = SecretStr("cctv")
    s3_secret_key: SecretStr = SecretStr("cctv-dev-secret")
    upload_url_seconds: int = 900
    media_url_seconds: int = 300

    # Redis for realtime notifications (new events, config changes). Empty: disabled.
    redis_url: str | None = "redis://localhost:6379/0"

    # Edge devices: pairing codes expire; a device is offline after missing heartbeats.
    pairing_code_hours: int = 24
    device_offline_seconds: int = 120

    @model_validator(mode="after")
    def _check_secrets(self) -> "Settings":
        if self.environment in ("staging", "production"):
            secret = self.secret_key.get_secret_value()
            if secret == DEV_SECRET or len(secret) < 32:
                raise ValueError("set CCTV_SECRET_KEY to at least 32 random characters")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
