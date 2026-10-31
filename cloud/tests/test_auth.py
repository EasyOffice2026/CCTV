"""Login, 2FA, sessions and password reset (plan 3.1)."""

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session

from cctv_cloud.db.models import User
from tests.conftest import PASSWORD, login, totp_code


def test_login_without_2fa_and_me(client, org):
    headers = login(client, org, "reviewer_a")
    me = client.get("/v1/auth/me", headers=headers).json()
    assert me["email"] == "reviewer_a@example.com"
    assert me["role"] == "reviewer"
    assert me["tenant_id"] == str(org.ids["a"])
    assert me["branch_ids"] == [str(org.ids["a1"])]
    assert "events_review" in me["permissions"] and "tenant_admin" not in me["permissions"]


def test_wrong_password_and_unknown_email_look_the_same(client, org):
    wrong = client.post(
        "/v1/auth/login", json={"email": "reviewer_a@example.com", "password": "nope"}
    )
    unknown = client.post("/v1/auth/login", json={"email": "who@example.com", "password": "nope"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


def test_lockout_after_repeated_failures(client, org):
    for _ in range(5):
        r = client.post(
            "/v1/auth/login", json={"email": "reviewer_a@example.com", "password": "wrong"}
        )
        assert r.status_code == 401
    r = client.post(
        "/v1/auth/login", json={"email": "reviewer_a@example.com", "password": PASSWORD}
    )
    assert r.status_code == 429


def test_owner_must_use_2fa(client, org):
    r = client.post("/v1/auth/login", json={"email": "owner_a@example.com", "password": PASSWORD})
    body = r.json()
    assert body["mfa_required"] == "verify" and "access_token" not in body

    secret = org.totp["owner_a@example.com"]
    wrong = client.post("/v1/auth/mfa/verify", json={"token": body["mfa_token"], "code": "000000"})
    assert wrong.status_code == 401
    ok = client.post(
        "/v1/auth/mfa/verify", json={"token": body["mfa_token"], "code": totp_code(secret)}
    )
    assert ok.status_code == 200

    # The same code can't be used twice.
    again = client.post(
        "/v1/auth/login", json={"email": "owner_a@example.com", "password": PASSWORD}
    )
    replay = client.post(
        "/v1/auth/mfa/verify",
        json={"token": again.json()["mfa_token"], "code": totp_code(secret)},
    )
    assert replay.status_code == 401


def test_owner_without_2fa_must_enrol(client, org, owner_engine):
    with Session(owner_engine) as s, s.begin():
        s.execute(
            update(User).where(User.id == org.ids["owner_b"]).values(totp_secret_encrypted=None)
        )
    r = client.post("/v1/auth/login", json={"email": "owner_b@example.com", "password": PASSWORD})
    assert r.json()["mfa_required"] == "setup"
    mfa_token = r.json()["mfa_token"]
    # The setup-stage token can't be used to skip straight to tokens.
    assert (
        client.post("/v1/auth/mfa/verify", json={"token": mfa_token, "code": "123456"}).status_code
        == 401
    )

    setup = client.post("/v1/auth/mfa/setup", json={"token": mfa_token}).json()
    assert setup["otpauth_uri"].startswith("otpauth://totp/")
    enabled = client.post(
        "/v1/auth/mfa/enable",
        json={"token": setup["setup_token"], "code": totp_code(setup["secret"])},
    )
    assert enabled.status_code == 200
    headers = {"Authorization": f"Bearer {enabled.json()['access_token']}"}
    assert client.get("/v1/auth/me", headers=headers).json()["mfa_enabled"] is True


def test_refresh_rotates_and_detects_reuse(client, org):
    r = client.post(
        "/v1/auth/login", json={"email": "reviewer_a@example.com", "password": PASSWORD}
    )
    first = r.json()
    second = client.post("/v1/auth/refresh", json={"token": first["refresh_token"]})
    assert second.status_code == 200
    # Replaying the old refresh token ends the whole session...
    assert (
        client.post("/v1/auth/refresh", json={"token": first["refresh_token"]}).status_code == 401
    )
    # ...so the new tokens stop working too.
    new = second.json()
    assert client.post("/v1/auth/refresh", json={"token": new["refresh_token"]}).status_code == 401
    me = client.get("/v1/auth/me", headers={"Authorization": f"Bearer {new['access_token']}"})
    assert me.status_code == 401


def test_logout_ends_the_session(client, org):
    headers = login(client, org, "reviewer_a")
    assert client.post("/v1/auth/logout", headers=headers).status_code == 204
    assert client.get("/v1/auth/me", headers=headers).status_code == 401


def test_deactivated_user_is_locked_out_immediately(client, org):
    reviewer = login(client, org, "reviewer_a")
    owner = login(client, org, "owner_a")
    r = client.patch(f"/v1/users/{org.ids['reviewer_a']}", json={"is_active": False}, headers=owner)
    assert r.status_code == 200
    assert client.get("/v1/auth/me", headers=reviewer).status_code == 401


def test_password_reset(client, org, fakes):
    _, _, mailer = fakes
    old = login(client, org, "reviewer_a")
    assert (
        client.post(
            "/v1/auth/password/forgot", json={"email": "reviewer_a@example.com"}
        ).status_code
        == 202
    )
    assert (
        client.post("/v1/auth/password/forgot", json={"email": "x@example.com"}).status_code == 202
    )
    token = mailer.last_token("reviewer_a@example.com")
    new_password = "a brand new password"
    r = client.post("/v1/auth/password/reset", json={"token": token, "new_password": new_password})
    assert r.status_code == 204
    # The link works once, and existing sessions are signed out.
    again = client.post("/v1/auth/password/reset", json={"token": token, "new_password": "x" * 12})
    assert again.status_code == 401
    assert client.get("/v1/auth/me", headers=old).status_code == 401
    r = client.post(
        "/v1/auth/login", json={"email": "reviewer_a@example.com", "password": new_password}
    )
    assert r.status_code == 200


@pytest.mark.parametrize(
    "header",
    [
        "Bearer not-a-token",
        "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl",
    ],
)
def test_bad_tokens_are_rejected(client, org, header):
    assert client.get("/v1/branches", headers={"Authorization": header}).status_code == 401


def test_refresh_token_is_not_an_access_token(client, org):
    r = client.post(
        "/v1/auth/login", json={"email": "reviewer_a@example.com", "password": PASSWORD}
    )
    refresh = r.json()["refresh_token"]
    assert (
        client.get("/v1/branches", headers={"Authorization": f"Bearer {refresh}"}).status_code
        == 401
    )
