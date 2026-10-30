"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-30 23:31:09.912691
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from cctv_cloud.config import get_settings

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Everything below is hand-written. Values are literal on purpose: a migration must
# keep doing the same thing even after the models change.

FEATURES = [
    # key, name, category, availability
    ("attendance", "Staff attendance", "staff", "pilot"),
    ("staff_activity", "Staff activity", "staff", "pilot"),
    ("kitchen_compliance", "Kitchen compliance", "kitchen", "pilot"),
    ("cashier", "Cashier area", "cashier", "pilot"),
    ("customer_count", "Customer count", "customers", "pilot"),
    ("queue", "Queue detection", "customers", "pilot"),
    ("security", "Security", "security", "pilot"),
    ("heatmap", "Heatmaps", "customers", "later"),
    ("object_left_removed", "Object left / removed", "security", "later"),
]

# Tables whose rows belong to exactly one tenant via tenant_id.
TENANT_TABLES = [
    "branches",
    "working_hours",
    "shifts",
    "edge_devices",
    "nvrs",
    "cameras",
    "zones",
    "feature_settings",
    "staff",
    "face_templates",
    "users",
    "user_branch_access",
    "events",
    "attendance_records",
    "count_buckets",
    "alert_subscriptions",
    "alert_deliveries",
    "audit_logs",
]
# The app role may read/write these; tenants (read/update) and audit_logs
# (read/insert: tamper-resistant) are granted separately.
APP_CRUD_TABLES = [t for t in TENANT_TABLES if t != "audit_logs"]

CURRENT_TENANT_FUNCTION_SQL = """
CREATE FUNCTION app_current_tenant() RETURNS uuid
LANGUAGE sql STABLE
AS $$ SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid $$
"""

PRIVACY_TRIGGERS_SQL = [
    # Plan §7.5: no face enrolment without recorded consent.
    """
    CREATE FUNCTION face_template_requires_consent() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM staff
            WHERE id = NEW.staff_id
              AND tenant_id = NEW.tenant_id
              AND status = 'active'
              AND consent_signed_at IS NOT NULL
        ) THEN
            RAISE EXCEPTION 'face enrolment refused: staff % has no recorded consent', NEW.staff_id
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END $$
    """,
    """
    CREATE TRIGGER face_templates_require_consent
    BEFORE INSERT OR UPDATE ON face_templates
    FOR EACH ROW EXECUTE FUNCTION face_template_requires_consent()
    """,
    # Plan §7.5: biometric data is deleted when staff leave or withdraw consent.
    """
    CREATE FUNCTION staff_purge_biometrics() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        IF NEW.status = 'left' OR NEW.consent_signed_at IS NULL THEN
            DELETE FROM face_templates WHERE staff_id = NEW.id AND tenant_id = NEW.tenant_id;
        END IF;
        RETURN NEW;
    END $$
    """,
    """
    CREATE TRIGGER staff_purge_biometrics
    AFTER UPDATE OF status, consent_signed_at ON staff
    FOR EACH ROW EXECUTE FUNCTION staff_purge_biometrics()
    """,
]

DROP_PRIVACY_TRIGGERS_SQL = [
    "DROP TRIGGER staff_purge_biometrics ON staff",
    "DROP FUNCTION staff_purge_biometrics()",
    "DROP TRIGGER face_templates_require_consent ON face_templates",
    "DROP FUNCTION face_template_requires_consent()",
]


def _app_role() -> str:
    role = get_settings().app_db_role
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", role):
        raise ValueError(f"invalid app role name {role!r}")
    return role


def _seed_features() -> None:
    features = sa.table(
        "features",
        sa.column("key", sa.String),
        sa.column("name", sa.String),
        sa.column("category", sa.String),
        sa.column("availability", sa.String),
    )
    # Everything starts switched off; features are enabled per client in Settings.
    op.bulk_insert(
        features,
        [{"key": k, "name": n, "category": c, "availability": a} for k, n, c, a in FEATURES],
    )


def _enable_row_level_security() -> None:
    op.execute(CURRENT_TENANT_FUNCTION_SQL)
    op.execute("ALTER TABLE tenants ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON tenants"
        " USING (id = app_current_tenant()) WITH CHECK (id = app_current_tenant())"
    )
    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table}"
            " USING (tenant_id = app_current_tenant())"
            " WITH CHECK (tenant_id = app_current_tenant())"
        )


def _grant_app_role(role: str) -> None:
    op.execute(
        f"""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                RAISE EXCEPTION 'database role {role} is missing: create it first (infra/postgres/init)';
            END IF;
        END $$
        """
    )
    op.execute(f"GRANT USAGE ON SCHEMA public TO {role}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {', '.join(APP_CRUD_TABLES)} TO {role}")
    op.execute(f"GRANT SELECT, UPDATE ON tenants TO {role}")
    op.execute(f"GRANT SELECT, INSERT ON audit_logs TO {role}")
    op.execute(f"GRANT SELECT ON features TO {role}")
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}")


def _revoke_app_role(role: str) -> None:
    op.execute(
        f"""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {role};
                REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {role};
            END IF;
        END $$
        """
    )


def _create_privacy_triggers() -> None:
    for statement in PRIVACY_TRIGGERS_SQL:
        op.execute(statement)


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table(
        "features",
        sa.Column("key", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("category", sa.String(length=40), nullable=False),
        sa.Column("availability", sa.String(length=20), nullable=False),
        sa.Column("default_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.CheckConstraint(
            "availability IN ('pilot', 'later')", name=op.f("ck_features_availability_valid")
        ),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_features")),
    )
    op.create_table(
        "tenants",
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("slug", sa.String(length=63), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="active", nullable=False),
        sa.Column("retention_footage_days", sa.Integer(), server_default="30", nullable=False),
        sa.Column("retention_event_days", sa.Integer(), server_default="90", nullable=False),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('active', 'suspended')", name=op.f("ck_tenants_status_valid")
        ),
        sa.CheckConstraint(
            "retention_event_days BETWEEN 1 AND 3650", name=op.f("ck_tenants_retention_event_range")
        ),
        sa.CheckConstraint(
            "retention_footage_days BETWEEN 1 AND 3650",
            name=op.f("ck_tenants_retention_footage_range"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenants")),
        sa.UniqueConstraint("slug", name=op.f("uq_tenants_slug")),
    )
    op.create_table(
        "branches",
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("timezone", sa.String(length=64), server_default="Asia/Kuwait", nullable=False),
        sa.Column("address", sa.String(length=500), nullable=True),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_branches_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_branches")),
        sa.UniqueConstraint("id", "tenant_id", name=op.f("uq_branches_id_tenant_id")),
        sa.UniqueConstraint("tenant_id", "name", name=op.f("uq_branches_tenant_id_name")),
    )
    op.create_index(op.f("ix_branches_tenant_id"), "branches", ["tenant_id"], unique=False)
    op.create_table(
        "users",
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("full_name", sa.String(length=200), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=True),
        sa.Column("totp_secret_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(role = 'super_admin') = (tenant_id IS NULL)", name=op.f("ck_users_role_tenant")
        ),
        sa.CheckConstraint(
            "role IN ('super_admin', 'owner', 'branch_manager', 'reviewer', 'installer')",
            name=op.f("ck_users_role_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_users_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("id", "tenant_id", name=op.f("uq_users_id_tenant_id")),
    )
    op.create_index(op.f("ix_users_tenant_id"), "users", ["tenant_id"], unique=False)
    op.create_index(
        "uq_users_email_lower", "users", [sa.literal_column("lower(email)")], unique=True
    )
    op.create_table(
        "alert_subscriptions",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("feature_key", sa.String(length=40), nullable=True),
        sa.Column("branch_id", sa.Uuid(), nullable=True),
        sa.Column("min_severity", sa.String(length=10), server_default="warning", nullable=False),
        sa.Column(
            "channels",
            postgresql.ARRAY(sa.String(length=20)),
            server_default=sa.text("ARRAY['web']::varchar(20)[]"),
            nullable=False,
        ),
        sa.Column("quiet_from", sa.Time(), nullable=True),
        sa.Column("quiet_until", sa.Time(), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "channels <@ ARRAY['web', 'whatsapp', 'email']::varchar(20)[]",
            name=op.f("ck_alert_subscriptions_channels_valid"),
        ),
        sa.CheckConstraint(
            "min_severity IN ('info', 'warning', 'critical')",
            name=op.f("ck_alert_subscriptions_min_severity_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["branch_id", "tenant_id"],
            ["branches.id", "branches.tenant_id"],
            name=op.f("fk_alert_subscriptions_branch_id_tenant_id_branches"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["feature_key"],
            ["features.key"],
            name=op.f("fk_alert_subscriptions_feature_key_features"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_alert_subscriptions_tenant_id_tenants")
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name=op.f("fk_alert_subscriptions_user_id_tenant_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alert_subscriptions")),
    )
    op.create_index(
        op.f("ix_alert_subscriptions_tenant_id"), "alert_subscriptions", ["tenant_id"], unique=False
    )
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=50), nullable=False),
        sa.Column("target_type", sa.String(length=50), nullable=True),
        sa.Column("target_id", sa.String(length=100), nullable=True),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.String(length=500), nullable=True),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_audit_logs_tenant_id_tenants")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_audit_logs_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_logs")),
    )
    op.create_index(
        "ix_audit_logs_tenant_at",
        "audit_logs",
        ["tenant_id", sa.literal_column("at DESC")],
        unique=False,
    )
    op.create_table(
        "edge_devices",
        sa.Column("branch_id", sa.Uuid(), nullable=False),
        sa.Column("serial", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("software_version", sa.String(length=50), nullable=True),
        sa.Column("config_version_applied", sa.Integer(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("token_hash", sa.String(length=128), nullable=True),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'online', 'offline', 'retired')",
            name=op.f("ck_edge_devices_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["branch_id", "tenant_id"],
            ["branches.id", "branches.tenant_id"],
            name=op.f("fk_edge_devices_branch_id_tenant_id_branches"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_edge_devices_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_edge_devices")),
        sa.UniqueConstraint("id", "tenant_id", name=op.f("uq_edge_devices_id_tenant_id")),
        sa.UniqueConstraint("serial", name=op.f("uq_edge_devices_serial")),
    )
    op.create_index(op.f("ix_edge_devices_tenant_id"), "edge_devices", ["tenant_id"], unique=False)
    op.create_table(
        "nvrs",
        sa.Column("branch_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("vendor", sa.String(length=50), server_default="hikvision", nullable=False),
        sa.Column("model", sa.String(length=100), nullable=True),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("http_port", sa.Integer(), server_default="80", nullable=False),
        sa.Column("rtsp_port", sa.Integer(), server_default="554", nullable=False),
        sa.Column("credentials_ref", sa.String(length=100), nullable=True),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["branch_id", "tenant_id"],
            ["branches.id", "branches.tenant_id"],
            name=op.f("fk_nvrs_branch_id_tenant_id_branches"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_nvrs_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_nvrs")),
        sa.UniqueConstraint("branch_id", "name", name=op.f("uq_nvrs_branch_id_name")),
        sa.UniqueConstraint("id", "tenant_id", name=op.f("uq_nvrs_id_tenant_id")),
    )
    op.create_index(op.f("ix_nvrs_tenant_id"), "nvrs", ["tenant_id"], unique=False)
    op.create_table(
        "shifts",
        sa.Column("branch_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("starts_at", sa.Time(), nullable=False),
        sa.Column("ends_at", sa.Time(), nullable=False),
        sa.Column("grace_minutes", sa.SmallInteger(), server_default="10", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "grace_minutes BETWEEN 0 AND 240", name=op.f("ck_shifts_grace_minutes_range")
        ),
        sa.ForeignKeyConstraint(
            ["branch_id", "tenant_id"],
            ["branches.id", "branches.tenant_id"],
            name=op.f("fk_shifts_branch_id_tenant_id_branches"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_shifts_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shifts")),
        sa.UniqueConstraint("branch_id", "name", name=op.f("uq_shifts_branch_id_name")),
        sa.UniqueConstraint("id", "tenant_id", name=op.f("uq_shifts_id_tenant_id")),
    )
    op.create_index(op.f("ix_shifts_tenant_id"), "shifts", ["tenant_id"], unique=False)
    op.create_table(
        "user_branch_access",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("branch_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["branch_id", "tenant_id"],
            ["branches.id", "branches.tenant_id"],
            name=op.f("fk_user_branch_access_branch_id_tenant_id_branches"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_user_branch_access_tenant_id_tenants")
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name=op.f("fk_user_branch_access_user_id_tenant_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "branch_id", name=op.f("pk_user_branch_access")),
    )
    op.create_index(
        op.f("ix_user_branch_access_tenant_id"), "user_branch_access", ["tenant_id"], unique=False
    )
    op.create_table(
        "working_hours",
        sa.Column("branch_id", sa.Uuid(), nullable=False),
        sa.Column("weekday", sa.SmallInteger(), nullable=False),
        sa.Column("opens_at", sa.Time(), nullable=False),
        sa.Column("closes_at", sa.Time(), nullable=False),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("weekday BETWEEN 0 AND 6", name=op.f("ck_working_hours_weekday_range")),
        sa.ForeignKeyConstraint(
            ["branch_id", "tenant_id"],
            ["branches.id", "branches.tenant_id"],
            name=op.f("fk_working_hours_branch_id_tenant_id_branches"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_working_hours_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_working_hours")),
    )
    op.create_index(
        op.f("ix_working_hours_tenant_id"), "working_hours", ["tenant_id"], unique=False
    )
    op.create_table(
        "cameras",
        sa.Column("branch_id", sa.Uuid(), nullable=False),
        sa.Column("nvr_id", sa.Uuid(), nullable=True),
        sa.Column("channel", sa.SmallInteger(), nullable=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("role", sa.String(length=30), server_default="other", nullable=False),
        sa.Column("rtsp_main", sa.String(length=500), nullable=False),
        sa.Column("rtsp_sub", sa.String(length=500), nullable=True),
        sa.Column("credentials_ref", sa.String(length=100), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("health", sa.String(length=20), server_default="offline", nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "health IN ('online', 'degraded', 'offline')", name=op.f("ck_cameras_health_valid")
        ),
        sa.CheckConstraint(
            "role IN ('entrance', 'staff_entrance', 'kitchen', 'counter', 'dining', 'storage', 'outdoor', 'other')",
            name=op.f("ck_cameras_role_valid"),
        ),
        sa.CheckConstraint(
            "rtsp_main NOT LIKE '%%@%%' AND (rtsp_sub IS NULL OR rtsp_sub NOT LIKE '%%@%%')",
            name=op.f("ck_cameras_rtsp_no_credentials"),
        ),
        sa.ForeignKeyConstraint(
            ["branch_id", "tenant_id"],
            ["branches.id", "branches.tenant_id"],
            name=op.f("fk_cameras_branch_id_tenant_id_branches"),
        ),
        sa.ForeignKeyConstraint(
            ["nvr_id", "tenant_id"],
            ["nvrs.id", "nvrs.tenant_id"],
            name=op.f("fk_cameras_nvr_id_tenant_id_nvrs"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_cameras_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cameras")),
        sa.UniqueConstraint("branch_id", "name", name=op.f("uq_cameras_branch_id_name")),
        sa.UniqueConstraint("id", "tenant_id", name=op.f("uq_cameras_id_tenant_id")),
        sa.UniqueConstraint("nvr_id", "channel", name=op.f("uq_cameras_nvr_id_channel")),
    )
    op.create_index(op.f("ix_cameras_tenant_id"), "cameras", ["tenant_id"], unique=False)
    op.create_table(
        "staff",
        sa.Column("branch_id", sa.Uuid(), nullable=False),
        sa.Column("shift_id", sa.Uuid(), nullable=True),
        sa.Column("employee_code", sa.String(length=50), nullable=False),
        sa.Column("full_name", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=10), server_default="active", nullable=False),
        sa.Column("consent_signed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("status IN ('active', 'left')", name=op.f("ck_staff_status_valid")),
        sa.ForeignKeyConstraint(
            ["branch_id", "tenant_id"],
            ["branches.id", "branches.tenant_id"],
            name=op.f("fk_staff_branch_id_tenant_id_branches"),
        ),
        sa.ForeignKeyConstraint(
            ["shift_id", "tenant_id"],
            ["shifts.id", "shifts.tenant_id"],
            name=op.f("fk_staff_shift_id_tenant_id_shifts"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_staff_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_staff")),
        sa.UniqueConstraint("id", "tenant_id", name=op.f("uq_staff_id_tenant_id")),
        sa.UniqueConstraint(
            "tenant_id", "employee_code", name=op.f("uq_staff_tenant_id_employee_code")
        ),
    )
    op.create_index(op.f("ix_staff_tenant_id"), "staff", ["tenant_id"], unique=False)
    op.create_table(
        "attendance_records",
        sa.Column("branch_id", sa.Uuid(), nullable=False),
        sa.Column("staff_id", sa.Uuid(), nullable=False),
        sa.Column("work_date", sa.Date(), nullable=False),
        sa.Column("check_in_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("check_out_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("source", sa.String(length=10), server_default="face", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "source IN ('face', 'manual')", name=op.f("ck_attendance_records_source_valid")
        ),
        sa.CheckConstraint(
            "status IN ('on_time', 'late', 'absent', 'day_off')",
            name=op.f("ck_attendance_records_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["branch_id", "tenant_id"],
            ["branches.id", "branches.tenant_id"],
            name=op.f("fk_attendance_records_branch_id_tenant_id_branches"),
        ),
        sa.ForeignKeyConstraint(
            ["staff_id", "tenant_id"],
            ["staff.id", "staff.tenant_id"],
            name=op.f("fk_attendance_records_staff_id_tenant_id_staff"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_attendance_records_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_attendance_records")),
        sa.UniqueConstraint(
            "staff_id", "work_date", name=op.f("uq_attendance_records_staff_id_work_date")
        ),
    )
    op.create_index(
        op.f("ix_attendance_records_tenant_id"), "attendance_records", ["tenant_id"], unique=False
    )
    op.create_index(
        "ix_attendance_tenant_date", "attendance_records", ["tenant_id", "work_date"], unique=False
    )
    op.create_table(
        "face_templates",
        sa.Column("staff_id", sa.Uuid(), nullable=False),
        sa.Column("embedding_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column("key_id", sa.String(length=200), nullable=False),
        sa.Column("model_version", sa.String(length=50), nullable=False),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["staff_id", "tenant_id"],
            ["staff.id", "staff.tenant_id"],
            name=op.f("fk_face_templates_staff_id_tenant_id_staff"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_face_templates_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_face_templates")),
    )
    op.create_index(
        op.f("ix_face_templates_tenant_id"), "face_templates", ["tenant_id"], unique=False
    )
    op.create_table(
        "feature_settings",
        sa.Column("scope", sa.String(length=10), nullable=False),
        sa.Column("branch_id", sa.Uuid(), nullable=True),
        sa.Column("camera_id", sa.Uuid(), nullable=True),
        sa.Column("feature_key", sa.String(length=40), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "params",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "(scope = 'tenant' AND branch_id IS NULL AND camera_id IS NULL) OR (scope = 'branch' AND branch_id IS NOT NULL AND camera_id IS NULL) OR (scope = 'camera' AND camera_id IS NOT NULL AND branch_id IS NULL)",
            name=op.f("ck_feature_settings_scope_target"),
        ),
        sa.CheckConstraint(
            "scope IN ('tenant', 'branch', 'camera')", name=op.f("ck_feature_settings_scope_valid")
        ),
        sa.ForeignKeyConstraint(
            ["branch_id", "tenant_id"],
            ["branches.id", "branches.tenant_id"],
            name=op.f("fk_feature_settings_branch_id_tenant_id_branches"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["camera_id", "tenant_id"],
            ["cameras.id", "cameras.tenant_id"],
            name=op.f("fk_feature_settings_camera_id_tenant_id_cameras"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["feature_key"], ["features.key"], name=op.f("fk_feature_settings_feature_key_features")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_feature_settings_tenant_id_tenants")
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"], ["users.id"], name=op.f("fk_feature_settings_updated_by_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_feature_settings")),
        sa.UniqueConstraint(
            "tenant_id",
            "feature_key",
            "branch_id",
            "camera_id",
            name=op.f("uq_feature_settings_tenant_id_feature_key_branch_id_camera_id"),
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index(
        op.f("ix_feature_settings_tenant_id"), "feature_settings", ["tenant_id"], unique=False
    )
    op.create_table(
        "zones",
        sa.Column("camera_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("zone_type", sa.String(length=30), nullable=False),
        sa.Column("points", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "zone_type IN ('kitchen', 'counter', 'cash_drawer', 'queue', 'dining', 'staff_area', 'restricted', 'ignore', 'entrance_line')",
            name=op.f("ck_zones_zone_type_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["camera_id", "tenant_id"],
            ["cameras.id", "cameras.tenant_id"],
            name=op.f("fk_zones_camera_id_tenant_id_cameras"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_zones_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_zones")),
        sa.UniqueConstraint("camera_id", "name", name=op.f("uq_zones_camera_id_name")),
        sa.UniqueConstraint("id", "tenant_id", name=op.f("uq_zones_id_tenant_id")),
    )
    op.create_index(op.f("ix_zones_tenant_id"), "zones", ["tenant_id"], unique=False)
    op.create_table(
        "count_buckets",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("camera_id", sa.Uuid(), nullable=False),
        sa.Column("zone_id", sa.Uuid(), nullable=True),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("bucket_seconds", sa.SmallInteger(), server_default="300", nullable=False),
        sa.Column("in_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("out_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_occupancy", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "in_count >= 0 AND out_count >= 0", name=op.f("ck_count_buckets_counts_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["camera_id", "tenant_id"],
            ["cameras.id", "cameras.tenant_id"],
            name=op.f("fk_count_buckets_camera_id_tenant_id_cameras"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_count_buckets_tenant_id_tenants")
        ),
        sa.ForeignKeyConstraint(
            ["zone_id", "tenant_id"],
            ["zones.id", "zones.tenant_id"],
            name=op.f("fk_count_buckets_zone_id_tenant_id_zones"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_count_buckets")),
        sa.UniqueConstraint(
            "camera_id",
            "zone_id",
            "bucket_start",
            name=op.f("uq_count_buckets_camera_id_zone_id_bucket_start"),
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index(
        "ix_count_buckets_tenant_start",
        "count_buckets",
        ["tenant_id", "bucket_start"],
        unique=False,
    )
    op.create_table(
        "events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("branch_id", sa.Uuid(), nullable=False),
        sa.Column("camera_id", sa.Uuid(), nullable=False),
        sa.Column("zone_id", sa.Uuid(), nullable=True),
        sa.Column("edge_device_id", sa.Uuid(), nullable=True),
        sa.Column("feature_key", sa.String(length=40), nullable=False),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("severity", sa.String(length=10), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confidence", sa.Double(), nullable=False),
        sa.Column("staff_id", sa.Uuid(), nullable=True),
        sa.Column("track_id", sa.String(length=64), nullable=True),
        sa.Column("snapshot_key", sa.String(length=512), nullable=True),
        sa.Column("clip_key", sa.String(length=512), nullable=True),
        sa.Column(
            "attributes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("review_status", sa.String(length=15), server_default="new", nullable=False),
        sa.Column("reviewed_by", sa.Uuid(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "review_status IN ('new', 'valid', 'false_alarm', 'reviewed')",
            name=op.f("ck_events_review_status_valid"),
        ),
        sa.CheckConstraint(
            "severity IN ('info', 'warning', 'critical')", name=op.f("ck_events_severity_valid")
        ),
        sa.CheckConstraint("status IN ('open', 'closed')", name=op.f("ck_events_status_valid")),
        sa.CheckConstraint("confidence BETWEEN 0 AND 1", name=op.f("ck_events_confidence_range")),
        sa.CheckConstraint(
            "ended_at IS NULL OR ended_at >= started_at", name=op.f("ck_events_ended_after_started")
        ),
        sa.ForeignKeyConstraint(
            ["branch_id", "tenant_id"],
            ["branches.id", "branches.tenant_id"],
            name=op.f("fk_events_branch_id_tenant_id_branches"),
        ),
        sa.ForeignKeyConstraint(
            ["camera_id", "tenant_id"],
            ["cameras.id", "cameras.tenant_id"],
            name=op.f("fk_events_camera_id_tenant_id_cameras"),
        ),
        sa.ForeignKeyConstraint(
            ["edge_device_id", "tenant_id"],
            ["edge_devices.id", "edge_devices.tenant_id"],
            name=op.f("fk_events_edge_device_id_tenant_id_edge_devices"),
        ),
        sa.ForeignKeyConstraint(
            ["feature_key"], ["features.key"], name=op.f("fk_events_feature_key_features")
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by"], ["users.id"], name=op.f("fk_events_reviewed_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["staff_id", "tenant_id"],
            ["staff.id", "staff.tenant_id"],
            name=op.f("fk_events_staff_id_tenant_id_staff"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_events_tenant_id_tenants")
        ),
        sa.ForeignKeyConstraint(
            ["zone_id", "tenant_id"],
            ["zones.id", "zones.tenant_id"],
            name=op.f("fk_events_zone_id_tenant_id_zones"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_events")),
        sa.UniqueConstraint("id", "tenant_id", name=op.f("uq_events_id_tenant_id")),
    )
    op.create_index(
        "ix_events_tenant_camera_started",
        "events",
        ["tenant_id", "camera_id", sa.literal_column("started_at DESC")],
        unique=False,
    )
    op.create_index(
        "ix_events_tenant_feature_started",
        "events",
        ["tenant_id", "feature_key", sa.literal_column("started_at DESC")],
        unique=False,
    )
    op.create_index(
        "ix_events_tenant_staff_started",
        "events",
        ["tenant_id", "staff_id", sa.literal_column("started_at DESC")],
        unique=False,
        postgresql_where=sa.text("staff_id IS NOT NULL"),
    )
    op.create_index(
        "ix_events_tenant_started",
        "events",
        ["tenant_id", sa.literal_column("started_at DESC")],
        unique=False,
    )
    op.create_index(
        "ix_events_tenant_unreviewed",
        "events",
        ["tenant_id", sa.literal_column("started_at DESC")],
        unique=False,
        postgresql_where=sa.text("review_status = 'new'"),
    )
    op.create_table(
        "alert_deliveries",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=10), server_default="queued", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "channel IN ('web', 'whatsapp', 'email')",
            name=op.f("ck_alert_deliveries_channel_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'sent', 'delivered', 'failed')",
            name=op.f("ck_alert_deliveries_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["event_id", "tenant_id"],
            ["events.id", "events.tenant_id"],
            name=op.f("fk_alert_deliveries_event_id_tenant_id_events"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_alert_deliveries_tenant_id_tenants")
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name=op.f("fk_alert_deliveries_user_id_tenant_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alert_deliveries")),
    )
    op.create_index("ix_alert_deliveries_event", "alert_deliveries", ["event_id"], unique=False)
    op.create_index(
        op.f("ix_alert_deliveries_tenant_id"), "alert_deliveries", ["tenant_id"], unique=False
    )
    # ### end Alembic commands ###
    _seed_features()
    _enable_row_level_security()
    _grant_app_role(_app_role())
    _create_privacy_triggers()


def downgrade() -> None:
    _revoke_app_role(_app_role())
    for statement in DROP_PRIVACY_TRIGGERS_SQL:
        op.execute(statement)
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index(op.f("ix_alert_deliveries_tenant_id"), table_name="alert_deliveries")
    op.drop_index("ix_alert_deliveries_event", table_name="alert_deliveries")
    op.drop_table("alert_deliveries")
    op.drop_index(
        "ix_events_tenant_unreviewed",
        table_name="events",
        postgresql_where=sa.text("review_status = 'new'"),
    )
    op.drop_index("ix_events_tenant_started", table_name="events")
    op.drop_index(
        "ix_events_tenant_staff_started",
        table_name="events",
        postgresql_where=sa.text("staff_id IS NOT NULL"),
    )
    op.drop_index("ix_events_tenant_feature_started", table_name="events")
    op.drop_index("ix_events_tenant_camera_started", table_name="events")
    op.drop_table("events")
    op.drop_index("ix_count_buckets_tenant_start", table_name="count_buckets")
    op.drop_table("count_buckets")
    op.drop_index(op.f("ix_zones_tenant_id"), table_name="zones")
    op.drop_table("zones")
    op.drop_index(op.f("ix_feature_settings_tenant_id"), table_name="feature_settings")
    op.drop_table("feature_settings")
    op.drop_index(op.f("ix_face_templates_tenant_id"), table_name="face_templates")
    op.drop_table("face_templates")
    op.drop_index("ix_attendance_tenant_date", table_name="attendance_records")
    op.drop_index(op.f("ix_attendance_records_tenant_id"), table_name="attendance_records")
    op.drop_table("attendance_records")
    op.drop_index(op.f("ix_staff_tenant_id"), table_name="staff")
    op.drop_table("staff")
    op.drop_index(op.f("ix_cameras_tenant_id"), table_name="cameras")
    op.drop_table("cameras")
    op.drop_index(op.f("ix_working_hours_tenant_id"), table_name="working_hours")
    op.drop_table("working_hours")
    op.drop_index(op.f("ix_user_branch_access_tenant_id"), table_name="user_branch_access")
    op.drop_table("user_branch_access")
    op.drop_index(op.f("ix_shifts_tenant_id"), table_name="shifts")
    op.drop_table("shifts")
    op.drop_index(op.f("ix_nvrs_tenant_id"), table_name="nvrs")
    op.drop_table("nvrs")
    op.drop_index(op.f("ix_edge_devices_tenant_id"), table_name="edge_devices")
    op.drop_table("edge_devices")
    op.drop_index("ix_audit_logs_tenant_at", table_name="audit_logs")
    op.drop_table("audit_logs")
    op.drop_index(op.f("ix_alert_subscriptions_tenant_id"), table_name="alert_subscriptions")
    op.drop_table("alert_subscriptions")
    op.drop_index("uq_users_email_lower", table_name="users")
    op.drop_index(op.f("ix_users_tenant_id"), table_name="users")
    op.drop_table("users")
    op.drop_index(op.f("ix_branches_tenant_id"), table_name="branches")
    op.drop_table("branches")
    op.drop_table("tenants")
    op.drop_table("features")
    # ### end Alembic commands ###
    op.execute("DROP FUNCTION app_current_tenant()")
