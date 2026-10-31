"""Roles, branch access and tenant isolation through the API (plan 3.2, Phase 3 exit)."""

import pytest

from tests.conftest import login

CAMERA = {
    "name": "New camera",
    "role": "counter",
    "rtsp_main": "rtsp://10.0.0.9:554/Streaming/Channels/301",
}


@pytest.fixture
def h(client, org):
    """Auth headers per user, logged in lazily."""
    cache = {}

    def headers(name: str) -> dict[str, str]:
        if name not in cache:
            cache[name] = login(client, org, name)
        return cache[name]

    return headers


def test_branch_scoped_roles_see_only_their_branches(client, org, h):
    owner = {b["id"] for b in client.get("/v1/branches", headers=h("owner_a")).json()}
    manager = {b["id"] for b in client.get("/v1/branches", headers=h("manager_a")).json()}
    assert owner == {str(org.ids["a1"]), str(org.ids["a2"])}
    assert manager == {str(org.ids["a1"])}

    cams = client.get("/v1/cameras", headers=h("manager_a")).json()
    assert [c["id"] for c in cams] == [str(org.ids["cam_a1"])]
    assert client.get(f"/v1/cameras/{org.ids['cam_a2']}", headers=h("manager_a")).status_code == 404
    assert client.get(f"/v1/branches/{org.ids['a2']}", headers=h("reviewer_a")).status_code == 404


@pytest.mark.parametrize(
    ("user", "method", "path", "body", "expected"),
    [
        # Reviewers look at events but change nothing.
        ("reviewer_a", "post", "/v1/cameras", "camera_a1", 403),
        ("reviewer_a", "put", "/v1/feature-settings", "branch_setting", 403),
        ("reviewer_a", "get", "/v1/users", None, 403),
        ("reviewer_a", "get", "/v1/events", None, 200),
        # Installers set up cameras but don't see events or footage.
        ("installer_a", "post", "/v1/cameras", "camera_a1", 201),
        ("installer_a", "get", "/v1/events", None, 403),
        ("installer_a", "get", "/v1/users", None, 403),
        # Branch managers tune their branch but don't manage users or the tenant.
        ("manager_a", "put", "/v1/feature-settings", "branch_setting", 200),
        ("manager_a", "put", "/v1/feature-settings", "tenant_setting", 403),
        ("manager_a", "post", "/v1/users", "new_user", 403),
        ("manager_a", "patch", "/v1/tenant", "tenant_patch", 403),
        ("manager_a", "post", "/v1/edge-devices", "device_a1", 403),
        # Owners do everything in their tenant, nothing on the platform.
        ("owner_a", "post", "/v1/users", "new_user", 201),
        ("owner_a", "put", "/v1/feature-settings", "tenant_setting", 200),
        ("owner_a", "get", "/v1/platform/tenants", None, 403),
    ],
)
def test_role_permissions(client, org, h, user, method, path, body, expected):
    bodies = {
        "camera_a1": {**CAMERA, "branch_id": str(org.ids["a1"])},
        "branch_setting": {
            "scope": "branch",
            "branch_id": str(org.ids["a1"]),
            "feature_key": "security",
            "enabled": True,
        },
        "tenant_setting": {"scope": "tenant", "feature_key": "security", "enabled": True},
        "new_user": {"email": "new@example.com", "full_name": "New", "role": "reviewer"},
        "tenant_patch": {"retention_media_days": 30},
        "device_a1": {"branch_id": str(org.ids["a1"]), "name": "Box", "serial": "SN1"},
    }
    kwargs = {"json": bodies[body]} if body else {}
    response = getattr(client, method)(path, headers=h(user), **kwargs)
    assert response.status_code == expected, response.text


def test_installer_cannot_add_cameras_to_other_branches(client, org, h):
    r = client.post(
        "/v1/cameras", json={**CAMERA, "branch_id": str(org.ids["a2"])}, headers=h("installer_a")
    )
    assert r.status_code == 404


def test_tenants_cannot_see_each_other(client, org, h):
    owner_a = h("owner_a")
    for path in (
        f"/v1/branches/{org.ids['b1']}",
        f"/v1/cameras/{org.ids['cam_b1']}",
        f"/v1/cameras/{org.ids['cam_b1']}/zones",
        f"/v1/cameras/{org.ids['cam_b1']}/features",
    ):
        assert client.get(path, headers=owner_a).status_code == 404, path
    # Writing into another tenant's branch fails the same way.
    r = client.post(
        "/v1/cameras", json={**CAMERA, "branch_id": str(org.ids["b1"])}, headers=owner_a
    )
    assert r.status_code == 404
    r = client.patch(f"/v1/users/{org.ids['owner_b']}", json={"full_name": "x"}, headers=owner_a)
    assert r.status_code == 404
    # Asking for the other tenant explicitly is refused.
    other = {**owner_a, "X-Tenant-Id": str(org.ids["b"])}
    assert client.get("/v1/branches", headers=other).status_code == 403
    names = {b["name"] for b in client.get("/v1/branches", headers=h("owner_b")).json()}
    assert names == {"Branch b1"}


def test_super_admin_picks_a_tenant(client, org, h, fakes):
    admin = h("admin")
    assert client.get("/v1/branches", headers=admin).status_code == 400
    r = client.get("/v1/branches", headers={**admin, "X-Tenant-Id": str(org.ids["b"])})
    assert {b["name"] for b in r.json()} == {"Branch b1"}

    tenants = client.get("/v1/platform/tenants", headers=admin).json()
    assert {t["slug"] for t in tenants} == {"tenant-a", "tenant-b"}
    r = client.post(
        "/v1/platform/tenants",
        json={
            "name": "Cafe C",
            "slug": "cafe-c",
            "owner_email": "owner_c@example.com",
            "owner_name": "Owner C",
        },
        headers=admin,
    )
    assert r.status_code == 201, r.text
    _, _, mailer = fakes
    assert mailer.last_token("owner_c@example.com")  # invitation sent
    duplicate = client.post(
        "/v1/platform/tenants",
        json={"name": "x", "slug": "cafe-c", "owner_email": "z@example.com", "owner_name": "z"},
        headers=admin,
    )
    assert duplicate.status_code == 409


def test_owner_cannot_demote_themselves(client, org, h):
    r = client.patch(
        f"/v1/users/{org.ids['owner_a']}", json={"role": "reviewer"}, headers=h("owner_a")
    )
    assert r.status_code == 403


def test_super_admin_role_cannot_be_granted(client, org, h):
    r = client.post(
        "/v1/users",
        json={"email": "evil@example.com", "full_name": "x", "role": "super_admin"},
        headers=h("owner_a"),
    )
    assert r.status_code == 422


def test_api_keys(client, org, h):
    owner = h("owner_a")
    created = client.post(
        "/v1/api-keys", json={"name": "POS", "scopes": ["events:read"]}, headers=owner
    )
    assert created.status_code == 201
    key = {"X-API-Key": created.json()["key"]}
    assert client.get("/v1/events", headers=key).status_code == 200
    assert (
        client.post(
            "/v1/cameras", json={**CAMERA, "branch_id": str(org.ids["a1"])}, headers=key
        ).status_code
        == 403
    )
    assert client.get("/v1/users", headers=key).status_code == 403

    bad = client.post("/v1/api-keys", json={"name": "x", "scopes": ["admin"]}, headers=owner)
    assert bad.status_code == 422

    key_id = created.json()["api_key"]["id"]
    assert client.delete(f"/v1/api-keys/{key_id}", headers=owner).status_code == 204
    assert client.get("/v1/events", headers=key).status_code == 401
    assert client.get("/v1/events", headers={"X-API-Key": "cctv_nope_nope"}).status_code == 401


def test_audit_log_records_admin_changes(client, org, h):
    owner = h("owner_a")
    client.post(
        "/v1/users",
        json={"email": "audited@example.com", "full_name": "A", "role": "reviewer"},
        headers=owner,
    )
    actions = [e["action"] for e in client.get("/v1/audit-logs", headers=owner).json()]
    assert "user_change" in actions and "login" in actions
    # Tenant b's log doesn't contain tenant a's entries.
    b_log = client.get("/v1/audit-logs", headers=h("owner_b")).json()
    assert all(e["user_id"] != str(org.ids["owner_a"]) for e in b_log)


def test_api_key_secret_may_contain_underscores(client, org, h, monkeypatch):
    from cctv_cloud.api import integrations

    monkeypatch.setattr(integrations, "new_secret", lambda n=32: "abc_def-" + "x" * n)
    created = client.post(
        "/v1/api-keys", json={"name": "HRM", "scopes": ["events:read"]}, headers=h("owner_a")
    ).json()
    assert created["key"].count("_") >= 3
    assert client.get("/v1/events", headers={"X-API-Key": created["key"]}).status_code == 200
