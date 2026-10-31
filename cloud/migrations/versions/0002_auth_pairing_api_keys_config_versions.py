"""auth, pairing, api keys, config versions

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-31 03:21:23.297063
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from cctv_cloud.config import get_settings

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Hand-written SQL below. Values are literal on purpose (see 0001).

CURRENT_USER_FUNCTION_SQL = """
CREATE FUNCTION app_current_user() RETURNS uuid
LANGUAGE sql STABLE
AS $$ SELECT NULLIF(current_setting('app.user_id', true), '')::uuid $$
"""

# Lookups that must work before the tenant is known (login, device and API-key auth,
# pairing). They run as the table owner, bypass row-level security, and return only ids,
# never hashes or other columns; the caller then opens a tenant session.
LOOKUP_FUNCTIONS = {
    "auth_find_user(text)": """
        CREATE FUNCTION auth_find_user(p_email text)
        RETURNS TABLE (user_id uuid, tenant_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
        AS $$ SELECT id, tenant_id FROM users WHERE lower(email) = lower(p_email) $$
    """,
    "auth_find_device(text)": """
        CREATE FUNCTION auth_find_device(p_token_hash text)
        RETURNS TABLE (device_id uuid, tenant_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
        AS $$
            SELECT id, tenant_id FROM edge_devices
            WHERE token_hash = p_token_hash AND status <> 'retired'
        $$
    """,
    "auth_find_pairing(text)": """
        CREATE FUNCTION auth_find_pairing(p_code_hash text)
        RETURNS TABLE (device_id uuid, tenant_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
        AS $$
            SELECT id, tenant_id FROM edge_devices
            WHERE pairing_code_hash = p_code_hash AND pairing_expires_at > now()
              AND status <> 'retired'
        $$
    """,
    "auth_find_api_key(text)": """
        CREATE FUNCTION auth_find_api_key(p_prefix text)
        RETURNS TABLE (api_key_id uuid, tenant_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
        AS $$ SELECT id, tenant_id FROM api_keys WHERE prefix = p_prefix AND revoked_at IS NULL $$
    """,
    # Super-admin tenant list and the retention worker; the API checks the caller's role.
    "platform_list_tenants()": """
        CREATE FUNCTION platform_list_tenants()
        RETURNS TABLE (tenant_id uuid, name text, slug text, status text)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
        AS $$ SELECT id, name, slug, status FROM tenants ORDER BY name $$
    """,
}


def _app_role() -> str:
    role = get_settings().app_db_role
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", role):
        raise ValueError(f"invalid app role name {role!r}")
    return role


def _upgrade_tenants() -> None:
    op.drop_constraint(op.f("ck_tenants_retention_footage_range"), "tenants", type_="check")
    op.alter_column("tenants", "retention_footage_days", new_column_name="retention_media_days")
    op.alter_column("tenants", "retention_media_days", server_default="90")
    op.alter_column("tenants", "retention_event_days", server_default="365")
    op.execute(
        "UPDATE tenants SET retention_event_days = retention_media_days"
        " WHERE retention_event_days < retention_media_days"
    )
    op.create_check_constraint(
        "retention_media_range", "tenants", "retention_media_days BETWEEN 1 AND 3650"
    )
    op.create_check_constraint(
        "media_within_events", "tenants", "retention_media_days <= retention_event_days"
    )


def _downgrade_tenants() -> None:
    op.drop_constraint(op.f("ck_tenants_media_within_events"), "tenants", type_="check")
    op.drop_constraint(op.f("ck_tenants_retention_media_range"), "tenants", type_="check")
    op.alter_column("tenants", "retention_event_days", server_default="90")
    op.alter_column("tenants", "retention_media_days", server_default="30")
    op.alter_column("tenants", "retention_media_days", new_column_name="retention_footage_days")
    op.create_check_constraint(
        "retention_footage_range", "tenants", "retention_footage_days BETWEEN 1 AND 3650"
    )


def _upgrade_security(role: str) -> None:
    op.execute(CURRENT_USER_FUNCTION_SQL)
    # Super-admins have no tenant; they can still read and update their own user row.
    op.execute("DROP POLICY tenant_isolation ON users")
    op.execute(
        "CREATE POLICY tenant_isolation ON users"
        " USING (tenant_id = app_current_tenant() OR id = app_current_user())"
        " WITH CHECK (tenant_id = app_current_tenant() OR id = app_current_user())"
    )
    op.execute("ALTER TABLE user_sessions ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY own_sessions ON user_sessions"
        " USING (user_id = app_current_user()) WITH CHECK (user_id = app_current_user())"
    )
    op.execute("ALTER TABLE api_keys ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON api_keys"
        " USING (tenant_id = app_current_tenant()) WITH CHECK (tenant_id = app_current_tenant())"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON user_sessions, api_keys TO {role}")
    # Super-admins create tenants by setting app.tenant_id to the new id first (RLS check).
    op.execute(f"GRANT INSERT ON tenants TO {role}")
    for signature, sql in LOOKUP_FUNCTIONS.items():
        op.execute(sql)
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {role}")


def _downgrade_security(role: str) -> None:
    for signature in LOOKUP_FUNCTIONS:
        op.execute(f"DROP FUNCTION {signature}")
    op.execute(f"REVOKE INSERT ON tenants FROM {role}")
    op.execute(f"REVOKE ALL ON user_sessions, api_keys FROM {role}")
    op.execute("DROP POLICY tenant_isolation ON users")
    op.execute(
        "CREATE POLICY tenant_isolation ON users"
        " USING (tenant_id = app_current_tenant()) WITH CHECK (tenant_id = app_current_tenant())"
    )


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table(
        "api_keys",
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("prefix", sa.String(length=16), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("scopes", postgresql.ARRAY(sa.String(length=40)), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_api_keys_created_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_api_keys_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_keys")),
        sa.UniqueConstraint("id", "tenant_id", name=op.f("uq_api_keys_id_tenant_id")),
        sa.UniqueConstraint("prefix", name=op.f("uq_api_keys_prefix")),
    )
    op.create_index(op.f("ix_api_keys_tenant_id"), "api_keys", ["tenant_id"], unique=False)
    op.create_table(
        "user_sessions",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("refresh_jti", sa.String(length=64), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.String(length=500), nullable=True),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_sessions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_sessions")),
    )
    op.create_index("ix_user_sessions_user", "user_sessions", ["user_id"], unique=False)
    op.add_column(
        "branches", sa.Column("config_version", sa.BigInteger(), server_default="1", nullable=False)
    )
    op.add_column(
        "edge_devices", sa.Column("pairing_code_hash", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "edge_devices", sa.Column("pairing_expires_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.alter_column(
        "edge_devices",
        "token_hash",
        existing_type=sa.VARCHAR(length=128),
        type_=sa.String(length=64),
        existing_nullable=True,
    )
    op.create_unique_constraint(
        op.f("uq_edge_devices_pairing_code_hash"), "edge_devices", ["pairing_code_hash"]
    )
    op.create_unique_constraint(op.f("uq_edge_devices_token_hash"), "edge_devices", ["token_hash"])
    op.create_index(
        "ix_events_tenant_branch_started",
        "events",
        ["tenant_id", "branch_id", sa.literal_column("started_at DESC")],
        unique=False,
    )
    op.add_column("users", sa.Column("totp_last_step", sa.BigInteger(), nullable=True))
    op.add_column(
        "users", sa.Column("failed_logins", sa.SmallInteger(), server_default="0", nullable=False)
    )
    op.add_column("users", sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "users", sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=True)
    )
    # ### end Alembic commands ###
    _upgrade_tenants()
    _upgrade_security(_app_role())


def downgrade() -> None:
    _downgrade_security(_app_role())
    _downgrade_tenants()
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_column("users", "password_changed_at")
    op.drop_column("users", "locked_until")
    op.drop_column("users", "failed_logins")
    op.drop_column("users", "totp_last_step")
    op.drop_index("ix_events_tenant_branch_started", table_name="events")
    op.drop_constraint(op.f("uq_edge_devices_token_hash"), "edge_devices", type_="unique")
    op.drop_constraint(op.f("uq_edge_devices_pairing_code_hash"), "edge_devices", type_="unique")
    op.alter_column(
        "edge_devices",
        "token_hash",
        existing_type=sa.String(length=64),
        type_=sa.VARCHAR(length=128),
        existing_nullable=True,
    )
    op.drop_column("edge_devices", "pairing_expires_at")
    op.drop_column("edge_devices", "pairing_code_hash")
    op.drop_column("branches", "config_version")
    op.drop_index("ix_user_sessions_user", table_name="user_sessions")
    op.drop_table("user_sessions")
    op.drop_index(op.f("ix_api_keys_tenant_id"), table_name="api_keys")
    op.drop_table("api_keys")
    # ### end Alembic commands ###
    op.execute("DROP FUNCTION app_current_user()")  # after the policies that use it
