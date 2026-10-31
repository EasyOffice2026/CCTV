"""Deployment checks for plan 7.1, 7.2 and 7.4: `python -m cctv_cloud.cli security-check`.

Settings that are plainly unsafe in staging or production already stop the API from
starting (see `Settings`); this lists the rest, so a deployment can be checked before
go-live and after changes.
"""

from dataclasses import dataclass
from typing import Literal
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import func, select

from cctv_cloud.config import DEV_SECRET, Settings
from cctv_cloud.db.models import FaceTemplate
from cctv_cloud.db.session import tenant_session
from cctv_cloud.services.face_keys import current_key_id
from cctv_cloud.services.storage import S3Storage

Level = Literal["error", "warning", "ok"]
TLS_SSLMODES = {"require", "verify-ca", "verify-full"}


@dataclass(frozen=True)
class Finding:
    level: Level
    topic: str
    message: str


def check_settings(s: Settings) -> list[Finding]:
    deployed = s.environment in ("staging", "production")
    bad: Level = "error" if deployed else "warning"
    out: list[Finding] = []

    def add(ok: bool, topic: str, good: str, problem: str, level: Level = bad) -> None:
        out.append(Finding("ok", topic, good) if ok else Finding(level, topic, problem))

    secret = s.secret_key.get_secret_value()
    add(
        secret != DEV_SECRET and len(secret) >= 32,
        "secret key",
        "CCTV_SECRET_KEY is set",
        "CCTV_SECRET_KEY is the dev default or shorter than 32 characters",
    )
    # 7.1: TLS everywhere.
    add(
        urlsplit(s.web_url).scheme == "https",
        "tls",
        "the dashboard is served over https",
        f"CCTV_WEB_URL is not https ({s.web_url})",
    )
    add(
        not s.s3_endpoint_url or urlsplit(s.s3_endpoint_url).scheme == "https",
        "tls",
        "object storage is reached over https",
        f"CCTV_S3_ENDPOINT_URL is not https ({s.s3_endpoint_url})",
    )
    sslmode = parse_qs(urlsplit(s.database_url).query).get("sslmode", [""])[0]
    add(
        sslmode in TLS_SSLMODES,
        "tls",
        f"the database connection requires TLS (sslmode={sslmode})",
        "CCTV_DATABASE_URL has no sslmode=require (or verify-full); fine only when the "
        "database is on the same private host",
        "warning",
    )
    if s.redis_url:
        add(
            s.redis_url.startswith("rediss://"),
            "tls",
            "Redis is reached over TLS",
            "CCTV_REDIS_URL is not rediss://; fine only on a private network (it carries "
            "event notifications, not footage)",
            "warning",
        )
    # 7.2: face templates.
    key = current_key_id()
    add(
        key.startswith(("aws-kms:", "local:")),
        "encryption",
        f"face templates are encrypted with {key}",
        "CCTV_FACE_KEY is not set: face templates use a key derived from CCTV_SECRET_KEY",
    )
    if key.startswith("local:"):
        out.append(
            Finding(
                "warning",
                "encryption",
                "face templates use a local key (CCTV_FACE_KEYS); plan 7.2 asks for KMS",
            )
        )
    # 6.x channels that carry secrets of their own.
    if s.whatsapp_token.get_secret_value():
        add(
            bool(s.whatsapp_app_secret.get_secret_value()),
            "secrets",
            "WhatsApp delivery reports are verified",
            "CCTV_WHATSAPP_APP_SECRET is empty: delivery reports can't be verified",
        )
    return out


def check_database() -> list[Finding]:
    """Face templates still encrypted with an older key (run rotate-face-keys)."""
    from cctv_cloud.worker import tenant_ids  # the worker imports most of the app

    key = current_key_id()
    stale = 0
    for tenant_id in tenant_ids():
        with tenant_session(tenant_id) as db:
            stale += db.scalar(
                select(func.count()).select_from(FaceTemplate).where(FaceTemplate.key_id != key)
            )
    if stale:
        return [
            Finding(
                "warning",
                "encryption",
                f"{stale} face templates use an older key: run rotate-face-keys",
            )
        ]
    return [Finding("ok", "encryption", "every face template uses the current key")]


def check_storage(storage: S3Storage) -> list[Finding]:
    try:
        algorithm = storage.default_encryption()
    except Exception as exc:  # no permission to read the setting, endpoint down, ...
        return [Finding("warning", "encryption", f"bucket encryption unknown: {exc}")]
    if algorithm is None:
        return [
            Finding(
                "error",
                "encryption",
                f"bucket {storage.bucket} has no default encryption (SSE-S3 or SSE-KMS)",
            )
        ]
    return [Finding("ok", "encryption", f"bucket {storage.bucket} encrypts with {algorithm}")]
