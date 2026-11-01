"""Changing CCTV_SECRET_KEY (docs/security.md §7).

The secret key signs sign-in tokens and encrypts the 2FA secrets of users, the shared
secrets of sign-in integrations (HS256) and webhook secrets, and, when CCTV_FACE_KEY isn't
set, face templates. After CCTV_SECRET_KEY has been changed to the new key, this decrypts
all of them with the old key and encrypts them again with the new one, in one database
transaction: if anything can't be decrypted, nothing is changed. Values already under the
new key are left as they are, so running it twice is harmless.

It runs as the owner role (CCTV_MIGRATION_DATABASE_URL), because it goes over every tenant
and the super-admins. Everyone is signed out (their tokens were signed with the old key);
nobody has to set up 2FA again.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session

from cctv_cloud.config import get_settings
from cctv_cloud.db.models import FaceTemplate, SsoIntegration, User, Webhook
from cctv_cloud.security import decrypt_totp_secret, encrypt_totp_secret
from cctv_cloud.services import sso, webhooks
from cctv_cloud.services.face_keys import (
    DERIVED_KEY_ID,
    LEGACY_KEY_ID,
    open_embedding,
    seal_embedding,
)


@dataclass
class Rotated:
    totp: int = 0
    sso: int = 0
    webhooks: int = 0
    face_templates: int = 0
    already_new: int = 0  # under the new key already (an earlier run)
    unreadable: list[str] = field(default_factory=list)


class _UnreadableError(Exception):
    """Something can be decrypted with neither key: change nothing."""


@contextmanager
def _secret_key(key: str) -> Iterator[None]:
    settings = get_settings()
    saved = settings.secret_key
    settings.secret_key = SecretStr(key)
    try:
        yield
    finally:
        settings.secret_key = saved


def _readable(decrypt: Callable[[], object]) -> bool:
    try:
        decrypt()
    except Exception:
        return False
    return True


def rotate_secret_key(old_key: str, engine: Engine | None = None) -> Rotated:
    new_key = get_settings().secret_key.get_secret_value()
    if not old_key or old_key == new_key:
        raise ValueError("give the previous key; CCTV_SECRET_KEY must already be the new one")
    engine = engine or create_engine(get_settings().migration_database_url)
    result = Rotated()
    try:
        _rotate(engine, old_key, result)
    except _UnreadableError:
        return result  # rolled back
    return result


def _rotate(engine: Engine, old_key: str, result: Rotated) -> None:
    with Session(engine) as db, db.begin():
        users = db.scalars(select(User).where(User.totp_secret_encrypted.is_not(None))).all()
        integrations = db.scalars(
            select(SsoIntegration).where(SsoIntegration.secret_encrypted.is_not(None))
        ).all()
        hooks = db.scalars(select(Webhook)).all()
        templates = db.scalars(
            select(FaceTemplate).where(FaceTemplate.key_id.in_([LEGACY_KEY_ID, DERIVED_KEY_ID]))
        ).all()

        # (row, attribute, decrypt, encrypt, counter) for every encrypted value.
        values = [
            (u, "totp_secret_encrypted", decrypt_totp_secret, encrypt_totp_secret, "totp")
            for u in users
        ]
        values += [
            (i, "secret_encrypted", sso.decrypt_secret, sso.encrypt_secret, "sso")
            for i in integrations
        ]
        values += [
            (h, "secret_encrypted", webhooks.decrypt_secret, webhooks.encrypt_secret, "webhooks")
            for h in hooks
        ]
        plain: list[tuple] = []
        with _secret_key(old_key):
            for row, attr, decrypt, encrypt, counter in values:
                blob = getattr(row, attr)
                if _readable(lambda d=decrypt, b=blob: d(b)):
                    plain.append((row, attr, decrypt(blob), encrypt, counter))
                else:
                    plain.append((row, attr, None, encrypt, counter))
            faces = []
            for t in templates:
                try:
                    faces.append(
                        (
                            t,
                            open_embedding(
                                t.embedding_encrypted, t.key_id, t.tenant_id, t.staff_id
                            ),
                        )
                    )
                except Exception:
                    faces.append((t, None))
        for row, attr, secret, encrypt, counter in plain:
            if secret is not None:
                setattr(row, attr, encrypt(secret))
                setattr(result, counter, getattr(result, counter) + 1)
            elif _readable(lambda r=row, a=attr, c=counter: _decrypt_new(c, getattr(r, a))):
                result.already_new += 1
            else:
                result.unreadable.append(f"{type(row).__name__} {row.id}")
        for template, embedding in faces:
            if embedding is not None:
                template.embedding_encrypted, template.key_id = seal_embedding(
                    embedding, template.tenant_id, template.staff_id
                )
                result.face_templates += 1
            elif _readable(
                lambda t=template: open_embedding(
                    t.embedding_encrypted, t.key_id, t.tenant_id, t.staff_id
                )
            ):
                result.already_new += 1
            else:
                result.unreadable.append(f"FaceTemplate {template.id}")
        if result.unreadable:
            raise _UnreadableError


def _decrypt_new(counter: str, blob: bytes) -> object:
    decrypt = {
        "totp": decrypt_totp_secret,
        "sso": sso.decrypt_secret,
        "webhooks": webhooks.decrypt_secret,
    }[counter]
    return decrypt(blob)
