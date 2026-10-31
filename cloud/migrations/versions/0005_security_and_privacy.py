"""security and privacy: device token rotation, platform audit entries, audit retention

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-31 23:40:00.000000
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from cctv_cloud.config import get_settings

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Device lookup now also accepts the token being rotated out, until it expires.
FIND_DEVICE_SQL = """
    CREATE OR REPLACE FUNCTION auth_find_device(p_token_hash text)
    RETURNS TABLE (device_id uuid, tenant_id uuid)
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
    AS $$
        SELECT id, tenant_id FROM edge_devices
        WHERE status <> 'retired'
          AND (token_hash = p_token_hash
               OR (previous_token_hash = p_token_hash AND previous_token_expires_at > now()))
    $$
"""
OLD_FIND_DEVICE_SQL = """
    CREATE OR REPLACE FUNCTION auth_find_device(p_token_hash text)
    RETURNS TABLE (device_id uuid, tenant_id uuid)
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
    AS $$
        SELECT id, tenant_id FROM edge_devices
        WHERE token_hash = p_token_hash AND status <> 'retired'
    $$
"""

# The app role can only insert and read audit entries. Old ones are removed through this
# function, which never deletes anything younger than a year, whatever it is asked.
AUDIT_PURGE_SQL = """
    CREATE FUNCTION audit_purge(p_days integer)
    RETURNS bigint
    LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp
    AS $$
        WITH gone AS (
            DELETE FROM audit_logs
            WHERE at < now() - make_interval(days => GREATEST(p_days, 365))
            RETURNING 1
        )
        SELECT count(*) FROM gone
    $$
"""


def _app_role() -> str:
    role = get_settings().app_db_role
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", role):
        raise ValueError(f"invalid app role name {role!r}")
    return role


def upgrade() -> None:
    op.add_column("edge_devices", sa.Column("token_issued_at", sa.DateTime(timezone=True)))
    op.add_column("edge_devices", sa.Column("previous_token_hash", sa.String(length=64)))
    op.add_column(
        "edge_devices", sa.Column("previous_token_expires_at", sa.DateTime(timezone=True))
    )
    op.create_unique_constraint(
        op.f("uq_edge_devices_previous_token_hash"), "edge_devices", ["previous_token_hash"]
    )
    # Paired devices count as issued now; they rotate after CCTV_DEVICE_TOKEN_DAYS.
    op.execute("UPDATE edge_devices SET token_issued_at = now() WHERE token_hash IS NOT NULL")
    op.execute(FIND_DEVICE_SQL)

    op.create_index("ix_audit_logs_target", "audit_logs", ["tenant_id", "target_type", "target_id"])
    # Platform-level entries (no tenant: super-admin sign-ins) are written and read only by
    # sessions without a tenant; tenants still see only their own entries.
    op.execute("DROP POLICY tenant_isolation ON audit_logs")
    op.execute(
        "CREATE POLICY tenant_isolation ON audit_logs"
        " USING (tenant_id = app_current_tenant()"
        " OR (tenant_id IS NULL AND app_current_tenant() IS NULL))"
        " WITH CHECK (tenant_id = app_current_tenant()"
        " OR (tenant_id IS NULL AND app_current_tenant() IS NULL))"
    )
    role = _app_role()
    op.execute(AUDIT_PURGE_SQL)
    op.execute("REVOKE ALL ON FUNCTION audit_purge(integer) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION audit_purge(integer) TO {role}")


def downgrade() -> None:
    op.execute("DROP FUNCTION audit_purge(integer)")
    op.execute("DELETE FROM audit_logs WHERE tenant_id IS NULL")
    op.execute("DROP POLICY tenant_isolation ON audit_logs")
    op.execute(
        "CREATE POLICY tenant_isolation ON audit_logs"
        " USING (tenant_id = app_current_tenant()) WITH CHECK (tenant_id = app_current_tenant())"
    )
    op.drop_index("ix_audit_logs_target", table_name="audit_logs")
    op.execute(OLD_FIND_DEVICE_SQL)
    op.drop_constraint(op.f("uq_edge_devices_previous_token_hash"), "edge_devices", type_="unique")
    op.drop_column("edge_devices", "previous_token_expires_at")
    op.drop_column("edge_devices", "previous_token_hash")
    op.drop_column("edge_devices", "token_issued_at")
