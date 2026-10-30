from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


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


@lru_cache
def get_settings() -> Settings:
    return Settings()
