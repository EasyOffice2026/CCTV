"""attendance corrections, zone occupancy

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-31 17:08:51.570877
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Detected times from face sightings, kept apart from the (possibly corrected) times.
    op.add_column(
        "attendance_records", sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "attendance_records", sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.execute(
        "UPDATE attendance_records SET first_seen_at = check_in_at, last_seen_at = check_out_at"
        " WHERE source = 'face'"
    )
    op.add_column("attendance_records", sa.Column("note", sa.Text(), nullable=True))
    op.add_column("attendance_records", sa.Column("corrected_by", sa.Uuid(), nullable=True))
    op.add_column(
        "attendance_records", sa.Column("corrected_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_foreign_key(
        op.f("fk_attendance_records_corrected_by_users"),
        "attendance_records",
        "users",
        ["corrected_by"],
        ["id"],
    )
    op.create_check_constraint(
        op.f("ck_attendance_records_check_out_after_check_in"),
        "attendance_records",
        "check_out_at IS NULL OR check_in_at IS NULL OR check_out_at >= check_in_at",
    )
    op.add_column("count_buckets", sa.Column("occupied_seconds", sa.Integer(), nullable=True))
    op.create_check_constraint(
        op.f("ck_count_buckets_occupied_within_bucket"),
        "count_buckets",
        "occupied_seconds IS NULL OR occupied_seconds BETWEEN 0 AND bucket_seconds",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_count_buckets_occupied_within_bucket"), "count_buckets", type_="check"
    )
    op.drop_column("count_buckets", "occupied_seconds")
    op.drop_constraint(
        op.f("ck_attendance_records_check_out_after_check_in"), "attendance_records", type_="check"
    )
    op.drop_constraint(
        op.f("fk_attendance_records_corrected_by_users"), "attendance_records", type_="foreignkey"
    )
    op.drop_column("attendance_records", "corrected_at")
    op.drop_column("attendance_records", "corrected_by")
    op.drop_column("attendance_records", "note")
    op.drop_column("attendance_records", "last_seen_at")
    op.drop_column("attendance_records", "first_seen_at")
