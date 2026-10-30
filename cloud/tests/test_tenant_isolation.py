"""Row-level security must keep every tenant's data separate (brief: "Data separation")."""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from cctv_cloud.db.models import AuditLog, Branch, Camera, FaceTemplate, Feature, Staff, Tenant
from cctv_cloud.db.session import tenant_session


def test_every_tenant_table_has_row_level_security(owner_engine):
    with owner_engine.connect() as conn:
        tenant_tables = set(
            conn.execute(
                text(
                    "SELECT table_name FROM information_schema.columns"
                    " WHERE table_schema = 'public' AND column_name = 'tenant_id'"
                )
            ).scalars()
        ) | {"tenants"}
        protected = set(
            conn.execute(
                text(
                    "SELECT c.relname FROM pg_class c"
                    " JOIN pg_policies p ON p.tablename = c.relname AND p.schemaname = 'public'"
                    " WHERE c.relrowsecurity"
                )
            ).scalars()
        )
        app_role = conn.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = 'cctv_app'")
        ).one()
    assert tenant_tables - protected == set(), "tables with tenant_id but no RLS policy"
    assert tuple(app_role) == (False, False), "app role must not bypass RLS"


def test_tenant_sees_only_its_own_rows(tenants):
    a = tenants["a"]
    with tenant_session(a["tenant"]) as session:
        assert session.scalars(select(Branch.id)).all() == [a["branch"]]
        assert session.scalars(select(Camera.id)).all() == [a["camera"]]
        assert session.scalars(select(Tenant.id)).all() == [a["tenant"]]


def test_no_tenant_set_sees_nothing(tenants, app_engine):
    with Session(app_engine) as session:
        assert session.scalar(select(func.count()).select_from(Branch)) == 0
        assert session.scalar(select(func.count()).select_from(Tenant)) == 0


def test_cannot_insert_rows_for_another_tenant(tenants):
    with (
        pytest.raises(DBAPIError, match="row-level security"),
        tenant_session(tenants["a"]["tenant"]) as session,
    ):
        session.add(Branch(tenant_id=tenants["b"]["tenant"], name="Sneaky"))
        session.flush()


def test_cannot_link_to_another_tenants_row(tenants):
    """FK checks ignore RLS, so composite (id, tenant_id) FKs must block this."""
    a, b = tenants["a"], tenants["b"]
    with pytest.raises(IntegrityError, match="foreign key"), tenant_session(a["tenant"]) as session:
        session.add(
            Camera(
                tenant_id=a["tenant"],
                branch_id=b["branch"],
                name="Cross-tenant",
                rtsp_main="rtsp://10.0.0.1/Streaming/Channels/101",
            )
        )
        session.flush()


def test_cannot_update_another_tenants_rows(tenants, owner_engine):
    with tenant_session(tenants["a"]["tenant"]) as session:
        result = session.execute(
            update(Branch).where(Branch.id == tenants["b"]["branch"]).values(name="Hacked")
        )
        assert result.rowcount == 0
    with Session(owner_engine) as session:
        assert session.get(Branch, tenants["b"]["branch"]).name == "Main"


def test_audit_log_is_append_only_for_the_app(tenants):
    tenant_id = tenants["a"]["tenant"]
    with tenant_session(tenant_id) as session:
        session.add(AuditLog(tenant_id=tenant_id, action="view_clip", target_type="event"))
    with (
        pytest.raises(DBAPIError, match="permission denied"),
        tenant_session(tenant_id) as session,
    ):
        session.execute(update(AuditLog).values(action="nothing_to_see"))


def test_feature_catalogue_is_seeded_and_read_only(tenants):
    with tenant_session(tenants["a"]["tenant"]) as session:
        keys = set(session.scalars(select(Feature.key)))
    assert {"attendance", "kitchen_compliance", "cashier", "security"} <= keys
    assert len(keys) == 9
    with (
        pytest.raises(DBAPIError, match="permission denied"),
        tenant_session(tenants["a"]["tenant"]) as session,
    ):
        session.add(Feature(key="x", name="x", category="x", availability="pilot"))
        session.flush()


def test_rtsp_credentials_rejected_by_database(tenants):
    a = tenants["a"]
    with (
        pytest.raises(IntegrityError, match="rtsp_no_credentials"),
        tenant_session(a["tenant"]) as s,
    ):
        s.add(
            Camera(
                tenant_id=a["tenant"],
                branch_id=a["branch"],
                name="Leaky",
                rtsp_main="rtsp://admin:pass@10.0.0.1/Streaming/Channels/101",
            )
        )
        s.flush()


def test_face_enrolment_requires_consent_and_is_purged_when_staff_leave(tenants):
    a = tenants["a"]

    def template(staff_id: uuid.UUID) -> FaceTemplate:
        return FaceTemplate(
            tenant_id=a["tenant"],
            staff_id=staff_id,
            embedding_encrypted=b"\x00" * 16,
            key_id="kms/test",
            model_version="test",
        )

    with tenant_session(a["tenant"]) as session:
        staff = Staff(
            tenant_id=a["tenant"], branch_id=a["branch"], employee_code="E1", full_name="A"
        )
        session.add(staff)
    staff_id = staff.id

    with (
        pytest.raises(IntegrityError, match="no recorded consent"),
        tenant_session(a["tenant"]) as s,
    ):
        s.add(template(staff_id))
        s.flush()

    with tenant_session(a["tenant"]) as session:
        session.execute(
            update(Staff)
            .where(Staff.id == staff_id)
            .values(consent_signed_at=datetime(2026, 11, 1, tzinfo=timezone.utc))
        )
        session.add(template(staff_id))

    with tenant_session(a["tenant"]) as session:
        assert session.scalar(select(func.count()).select_from(FaceTemplate)) == 1
        session.execute(update(Staff).where(Staff.id == staff_id).values(status="left"))

    with tenant_session(a["tenant"]) as session:
        assert session.scalar(select(func.count()).select_from(FaceTemplate)) == 0
