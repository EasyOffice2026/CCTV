import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import CheckConstraint, Index, String, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from cctv_cloud.db.base import Base, TenantScoped, UUIDPk, check_in, tenant_fk
from cctv_cloud.db.enums import ReportPeriod


class Report(UUIDPk, TenantScoped, Base):
    """A branch summary for one day, week or month (plan 6.6). `data` holds the figures
    (services/reports.py); PDF and Excel files are rendered from it on download."""

    __tablename__ = "reports"

    branch_id: Mapped[uuid.UUID]
    period: Mapped[str] = mapped_column(String(10))
    period_start: Mapped[date]
    period_end: Mapped[date]  # last day included
    generated_at: Mapped[datetime] = mapped_column(server_default=func.now())
    data: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    # When subscribers were sent a link (scheduled reports only).
    notified_at: Mapped[datetime | None]

    __table_args__ = (
        tenant_fk("branch_id", "branches", ondelete="CASCADE"),
        UniqueConstraint("branch_id", "period", "period_start"),
        check_in("period", ReportPeriod),
        CheckConstraint("period_end >= period_start", name="period_order"),
        Index(
            "ix_reports_tenant_branch_start", "tenant_id", "branch_id", text("period_start DESC")
        ),
    )
