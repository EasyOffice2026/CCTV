"""edge software updates: releases, target version and update reports

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-01 18:43:21.092579
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from cctv_cloud.config import get_settings

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEVICE_COLUMNS = [
    sa.Column("target_version", sa.String(length=50), nullable=True),
    sa.Column("target_set_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("update_version", sa.String(length=50), nullable=True),
    sa.Column("update_state", sa.String(length=20), nullable=True),
    sa.Column("update_detail", sa.String(length=500), nullable=True),
    sa.Column("update_requested_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("update_reported_at", sa.DateTime(timezone=True), nullable=True),
]


def _app_role() -> str:
    role = get_settings().app_db_role
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", role):
        raise ValueError(f"invalid app role name {role!r}")
    return role


def upgrade() -> None:
    op.create_table(
        "edge_releases",
        sa.Column("version", sa.String(length=50), nullable=False),
        sa.Column("image", sa.String(length=330), nullable=False),
        sa.Column("notes", sa.String(length=2000), server_default="", nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_edge_releases_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_edge_releases")),
        sa.UniqueConstraint("version", name=op.f("uq_edge_releases_version")),
    )
    for column in DEVICE_COLUMNS:
        op.add_column("edge_devices", column)
    op.create_check_constraint(
        op.f("ck_edge_devices_update_state_valid"),
        "edge_devices",
        "update_state IN ('updating', 'updated', 'failed', 'rolled_back')",
    )
    # A catalogue, not tenant data (like features): no row-level security. Boxes accept
    # only images signed by the release workflow.
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON edge_releases TO {_app_role()}")


def downgrade() -> None:
    op.drop_constraint(op.f("ck_edge_devices_update_state_valid"), "edge_devices")
    for column in reversed(DEVICE_COLUMNS):
        op.drop_column("edge_devices", column.name)
    op.drop_table("edge_releases")
