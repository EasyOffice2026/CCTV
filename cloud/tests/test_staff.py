"""Staff, shifts, consent and face enrolment (plan 4.8, privacy rules §7.5)."""

import base64
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from cctv_cloud.db.models import FaceTemplate
from cctv_cloud.services.face_keys import open_embedding
from tests.conftest import login
from tests.test_edge_api import pair_device
from tests.test_live import FakeEdge, heartbeat

JPEG = b"\xff\xd8\xff\xe0" + b"photo" * 10
EMBEDDING = [round(i / 512, 4) for i in range(512)]


@pytest.fixture
def owner(client, org):
    return login(client, org, "owner_a")


def add_staff(client, headers, org, branch="a1", code="E1", **extra) -> dict:
    r = client.post(
        "/v1/staff",
        json={
            "branch_id": str(org.ids[branch]),
            "employee_code": code,
            "full_name": "Sara",
            **extra,
        },
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()


def consent(client, headers, staff_id, when=None):
    when = when or datetime.now(timezone.utc) - timedelta(days=1)
    return client.put(
        f"/v1/staff/{staff_id}/consent", json={"signed_at": when.isoformat()}, headers=headers
    )


def photos(n=2):
    return [("photos", (f"p{i}.jpg", JPEG, "image/jpeg")) for i in range(n)]


def embed(request: dict) -> dict:
    if request["request"]["kind"] != "face_embedding":
        return {"ok": False, "error": "unexpected"}
    assert base64.b64decode(request["request"]["images_base64"][0]) == JPEG
    return {"ok": True, "embedding": EMBEDDING, "model_version": "face-test-1"}


def stored_templates(owner_engine) -> list[FaceTemplate]:
    with Session(owner_engine) as s:
        return list(s.scalars(select(FaceTemplate)))


def test_shifts_and_staff(client, org, owner):
    r = client.post(
        "/v1/shifts",
        json={
            "branch_id": str(org.ids["a1"]),
            "name": "Morning",
            "starts_at": "08:00",
            "ends_at": "16:00",
        },
        headers=owner,
    )
    assert r.status_code == 201, r.text
    shift = r.json()
    staff = add_staff(client, owner, org, shift_id=shift["id"])
    assert staff["shift_id"] == shift["id"]
    assert staff["face_enrolled"] is False and staff["consent_signed_at"] is None

    # A shift of another branch can't be assigned.
    other = add_staff(client, owner, org, branch="a2", code="E2")
    r = client.patch(f"/v1/staff/{other['id']}", json={"shift_id": shift["id"]}, headers=owner)
    assert r.status_code == 422
    # Moving to another branch drops the shift.
    r = client.patch(
        f"/v1/staff/{staff['id']}", json={"branch_id": str(org.ids["a2"])}, headers=owner
    )
    assert r.status_code == 200 and r.json()["shift_id"] is None

    # Duplicate employee codes are refused.
    r = client.post(
        "/v1/staff",
        json={"branch_id": str(org.ids["a1"]), "employee_code": "E1", "full_name": "X"},
        headers=owner,
    )
    assert r.status_code == 409
    assert client.delete(f"/v1/shifts/{shift['id']}", headers=owner).status_code == 204


def test_branch_managers_see_only_their_branch(client, org, owner):
    add_staff(client, owner, org, branch="a1", code="E1")
    in_a2 = add_staff(client, owner, org, branch="a2", code="E2")
    manager = login(client, org, "manager_a")
    assert [s["employee_code"] for s in client.get("/v1/staff", headers=manager).json()] == ["E1"]
    assert client.get(f"/v1/staff/{in_a2['id']}", headers=manager).status_code == 404
    r = client.post(
        "/v1/staff",
        json={"branch_id": str(org.ids["a2"]), "employee_code": "E3", "full_name": "X"},
        headers=manager,
    )
    assert r.status_code == 404
    reviewer = login(client, org, "reviewer_a")
    assert client.get("/v1/staff", headers=reviewer).status_code == 403
    owner_b = login(client, org, "owner_b")
    assert client.get("/v1/staff", headers=owner_b).json() == []


def test_face_enrolment_needs_consent_and_stores_only_the_encrypted_embedding(
    client, org, owner, owner_engine
):
    device = pair_device(client, org, owner)
    heartbeat(client, device)
    staff = add_staff(client, owner, org)
    with FakeEdge(client, device, embed) as box:
        r = client.post(f"/v1/staff/{staff['id']}/face", files=photos(), headers=owner)
        assert r.status_code == 409  # no consent yet
        assert box.requests == []

        future = datetime.now(timezone.utc) + timedelta(days=1)
        assert consent(client, owner, staff["id"], future).status_code == 422
        assert consent(client, owner, staff["id"]).status_code == 200

        r = client.post(f"/v1/staff/{staff['id']}/face", files=photos(), headers=owner)
        assert r.status_code == 200, r.text
        assert r.json()["face_enrolled"] is True
        assert r.json()["face_model_version"] == "face-test-1"
        assert len(box.requests[0]["request"]["images_base64"]) == 2

    [template] = stored_templates(owner_engine)
    assert template.key_id == "app-secret:v2"  # no CCTV_FACE_KEY in tests
    assert JPEG not in template.embedding_encrypted
    opened = open_embedding(
        template.embedding_encrypted, template.key_id, template.tenant_id, template.staff_id
    )
    assert opened == pytest.approx(EMBEDDING)
    actions = [a["action"] for a in client.get("/v1/audit-logs", headers=owner).json()]
    assert "face_enrol" in actions and "consent_change" in actions


def test_withdrawing_consent_or_leaving_deletes_face_data(client, org, owner, owner_engine):
    device = pair_device(client, org, owner)
    heartbeat(client, device)
    first = add_staff(client, owner, org, code="E1")
    second = add_staff(client, owner, org, code="E2")
    with FakeEdge(client, device, embed):
        for staff in (first, second):
            consent(client, owner, staff["id"])
            r = client.post(f"/v1/staff/{staff['id']}/face", files=photos(1), headers=owner)
            assert r.status_code == 200, r.text
    assert len(stored_templates(owner_engine)) == 2

    r = client.delete(f"/v1/staff/{first['id']}/consent", headers=owner)
    assert r.status_code == 200 and r.json()["face_enrolled"] is False
    r = client.patch(f"/v1/staff/{second['id']}", json={"status": "left"}, headers=owner)
    assert r.status_code == 200 and r.json()["face_enrolled"] is False
    assert stored_templates(owner_engine) == []


def test_bad_photos_and_unsupported_boxes(client, org, owner):
    device = pair_device(client, org, owner)
    heartbeat(client, device)
    staff = add_staff(client, owner, org)
    consent(client, owner, staff["id"])
    not_an_image = [("photos", ("x.jpg", b"GIF89a....", "image/jpeg"))]
    r = client.post(f"/v1/staff/{staff['id']}/face", files=not_an_image, headers=owner)
    assert r.status_code == 422
    r = client.post(f"/v1/staff/{staff['id']}/face", files=photos(6), headers=owner)
    assert r.status_code == 422

    refuse = lambda request: {  # noqa: E731 - the box has no face model yet
        "ok": False,
        "error": "face model not installed on this box yet (Phase 5)",
    }
    with FakeEdge(client, device, refuse):
        r = client.post(f"/v1/staff/{staff['id']}/face", files=photos(), headers=owner)
    assert r.status_code == 502
    assert "face model" in r.json()["detail"]


def test_face_data_trigger_still_guards_the_database(owner_engine, org, client, owner):
    """Even bypassing the API, the database refuses a template without consent."""
    staff = add_staff(client, owner, org)
    with owner_engine.begin() as conn, pytest.raises(Exception, match="consent"):
        conn.execute(
            text(
                "INSERT INTO face_templates (id, tenant_id, staff_id, embedding_encrypted,"
                " key_id, model_version) VALUES (gen_random_uuid(), :t, :s, '\\x00', 'k', 'm')"
            ),
            {"t": org.ids["a"], "s": staff["id"]},
        )
