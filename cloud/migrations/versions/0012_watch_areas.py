"""objects left or removed: watch_area zones; the feature becomes available

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-01 21:10:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BEFORE = (
    "kitchen",
    "counter",
    "cash_drawer",
    "queue",
    "dining",
    "staff_area",
    "restricted",
    "ignore",
    "entrance_line",
)
CONSTRAINT = "ck_zones_zone_type_valid"


def _check(types: Sequence[str]) -> str:
    return "zone_type IN ({})".format(", ".join(f"'{t}'" for t in types))


def upgrade() -> None:
    op.drop_constraint(CONSTRAINT, "zones")
    op.create_check_constraint(op.f(CONSTRAINT), "zones", _check((*BEFORE, "watch_area")))
    # Built now: no longer marked "later" in Settings > Features.
    op.execute("UPDATE features SET availability = 'pilot' WHERE key = 'object_left_removed'")


def downgrade() -> None:
    op.execute("UPDATE features SET availability = 'later' WHERE key = 'object_left_removed'")
    op.execute("DELETE FROM zones WHERE zone_type = 'watch_area'")
    op.drop_constraint(CONSTRAINT, "zones")
    op.create_check_constraint(op.f(CONSTRAINT), "zones", _check(BEFORE))
