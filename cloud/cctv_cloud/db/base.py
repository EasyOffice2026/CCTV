import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    MetaData,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Stable constraint names so Alembic can diff and drop them.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {  # noqa: RUF012 - SQLAlchemy reads this class attribute
        datetime: DateTime(timezone=True),
        dict[str, Any]: JSONB,
    }


class UUIDPk:
    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )


class CreatedAt:
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class TenantScoped:
    """Rows owned by one tenant. Every such table gets a row-level-security policy."""

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)


def check_in(column: str, enum: type[Enum]) -> CheckConstraint:
    values = ", ".join(f"'{member.value}'" for member in enum)
    return CheckConstraint(f"{column} IN ({values})", name=f"{column}_valid")


def tenant_fk(column: str, parent_table: str, ondelete: str | None = None) -> ForeignKeyConstraint:
    """FK that also matches tenant_id, so a row can never point at another tenant's row.

    Plain FKs are checked without RLS, so without this a tenant could link to a
    foreign row just by knowing its UUID. The parent needs UNIQUE (id, tenant_id).
    """
    return ForeignKeyConstraint(
        [column, "tenant_id"],
        [f"{parent_table}.id", f"{parent_table}.tenant_id"],
        ondelete=ondelete,
    )
