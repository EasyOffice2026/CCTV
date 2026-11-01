"""Changing CCTV_SECRET_KEY: secrets encrypted with it are moved to the new key."""

from datetime import datetime, timezone

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from cctv_cloud.config import get_settings
from cctv_cloud.db.models import FaceTemplate, SsoIntegration, Staff, User, Webhook
from cctv_cloud.security import decrypt_totp_secret
from cctv_cloud.services import sso, webhooks
from cctv_cloud.services.face_keys import DERIVED_KEY_ID, open_embedding, seal_embedding
from cctv_cloud.services.secret_rotation import rotate_secret_key

NEW_KEY = "a-new-secret-key-for-the-rotation-test-0123456789"


@pytest.fixture
def secrets(org, owner_engine):
    """A sign-in integration, a webhook and a face template under the current (old) key."""
    t = org.ids["a"]
    with Session(owner_engine) as s, s.begin():
        staff = Staff(
            tenant_id=t,
            branch_id=org.ids["a1"],
            employee_code="E9",
            full_name="Face",
            consent_signed_at=datetime.now(timezone.utc),
        )
        s.add(staff)
        s.flush()
        staff_id = staff.id
        sealed, key_id = seal_embedding([0.5] * 8, t, staff.id)
        assert key_id == DERIVED_KEY_ID
        s.add(
            FaceTemplate(
                tenant_id=t,
                staff_id=staff.id,
                embedding_encrypted=sealed,
                key_id=key_id,
                model_version="test",
            )
        )
        s.add(
            SsoIntegration(
                tenant_id=t,
                name="Ops",
                issuer="https://ops.test",
                algorithm="HS256",
                secret_encrypted=sso.encrypt_secret("sso-shh"),
            )
        )
        s.add(
            Webhook(
                tenant_id=t,
                name="Hook",
                url="https://hooks.test/x",
                secret_encrypted=webhooks.encrypt_secret("whsec_shh"),
                topics=["event.new"],
            )
        )
    return staff_id


@pytest.fixture
def new_key():
    settings = get_settings()
    old = settings.secret_key
    settings.secret_key = SecretStr(NEW_KEY)
    yield old.get_secret_value()
    settings.secret_key = old


def test_rotation_moves_every_secret_to_the_new_key(org, owner_engine, secrets, new_key):
    with Session(owner_engine) as s:
        totp_before = {
            u.email: u.totp_secret_encrypted
            for u in s.scalars(select(User).where(User.totp_secret_encrypted.is_not(None)))
        }
    result = rotate_secret_key(new_key, owner_engine)
    assert result.unreadable == []
    assert (result.totp, result.sso, result.webhooks, result.face_templates) == (
        len(totp_before),
        1,
        1,
        1,
    )

    # Readable with the new key (the one now in the settings), with the same values.
    with Session(owner_engine) as s:
        for user in s.scalars(select(User).where(User.totp_secret_encrypted.is_not(None))):
            assert user.totp_secret_encrypted != totp_before[user.email]
            assert decrypt_totp_secret(user.totp_secret_encrypted) == org.totp[user.email]
        assert sso.decrypt_secret(s.scalar(select(SsoIntegration)).secret_encrypted) == "sso-shh"
        assert webhooks.decrypt_secret(s.scalar(select(Webhook)).secret_encrypted) == "whsec_shh"
        face = s.scalar(select(FaceTemplate))
        assert (
            open_embedding(face.embedding_encrypted, face.key_id, face.tenant_id, face.staff_id)
            == [0.5] * 8
        )

    # Running it again changes nothing.
    again = rotate_secret_key(new_key, owner_engine)
    assert (again.totp, again.sso, again.webhooks, again.face_templates) == (0, 0, 0, 0)
    assert again.already_new == len(totp_before) + 3 and again.unreadable == []


def test_a_wrong_old_key_changes_nothing(org, owner_engine, secrets, new_key):
    with Session(owner_engine) as s:
        before = s.scalar(select(Webhook)).secret_encrypted
    result = rotate_secret_key("not-the-old-key-at-all-0123456789", owner_engine)
    assert result.unreadable  # nothing could be read with it: all rolled back
    with Session(owner_engine) as s:
        assert s.scalar(select(Webhook)).secret_encrypted == before
    with pytest.raises(ValueError):
        rotate_secret_key(NEW_KEY, owner_engine)  # the "old" key is the current one
