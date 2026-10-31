from functools import lru_cache
from typing import Any, Literal

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

    # Live view, playback, frames and face enrolment go through the branch's edge box;
    # the browser's request waits this long for the box to answer.
    remote_timeout_seconds: float = 15
    face_embedding_timeout_seconds: float = 30
    # ICE servers given to browsers for WebRTC (live view, playback). Add a TURN server for
    # viewers whose network blocks direct connections to the box.
    webrtc_ice_servers: list[dict[str, Any]] = [{"urls": ["stun:stun.l.google.com:19302"]}]

    event_export_max_rows: int = 10_000

    # Alerts (plan 6.1-6.4). A user gets one alert per event and channel, and none for the
    # same camera, zone and event type within the cooldown. Events older than
    # alert_max_age_minutes when they arrive (e.g. synced after an outage) don't alert;
    # they are in the feed and the reports.
    alert_cooldown_seconds: int = 300
    alert_max_age_minutes: int = 30
    alert_max_attempts: int = 5
    # WhatsApp Business Platform (Cloud API). Without a token, messages go to the log.
    whatsapp_api_url: str = "https://graph.facebook.com/v21.0"
    whatsapp_phone_number_id: str = ""
    whatsapp_token: SecretStr = SecretStr("")
    # Verify delivery-report webhooks (app secret) and the webhook subscription.
    whatsapp_app_secret: SecretStr = SecretStr("")
    whatsapp_verify_token: SecretStr = SecretStr("")
    # Message templates approved by Meta (see cloud/README.md for their text).
    whatsapp_templates: dict[str, str] = {
        "alert": "cctv_alert",
        "resolved": "cctv_alert_resolved",
        "report": "cctv_report",
    }
    whatsapp_max_per_hour: int = 20  # per user; WhatsApp messages cost money
    # Web Push (VAPID): `python -m cctv_cloud.cli vapid-keys`. Empty: browser
    # notifications only while the dashboard is open.
    vapid_private_key: SecretStr = SecretStr("")
    vapid_subject: str = "mailto:alerts@example.com"

    # Reports (plan 6.6): yesterday's report is made after this hour (branch time), so
    # events the edge buffered overnight are in it. Weeks start on Sunday (6; Monday = 0).
    report_hour: int = 2
    report_week_start: int = 6
    report_retention_days: int = 730
    # A TrueType font with Arabic for PDF reports; found automatically when empty.
    report_font: str = ""

    @property
    def secure_cookies(self) -> bool:
        return self.environment in ("staging", "production")

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
