"""The till (POS) sends its sales (later phase: drawer vs sales); see services/pos.py and
docs/integration/pos-and-payroll.md."""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import AwareDatetime, BaseModel, Field

from cctv_cloud.api.deps import Context, context, not_found
from cctv_cloud.db.enums import PosPayment
from cctv_cloud.db.models import Branch
from cctv_cloud.rbac import Permission
from cctv_cloud.services.pos import store_sales

router = APIRouter(prefix="/v1/pos", tags=["integrations"])

Till = Annotated[Context, Depends(context(Permission.POS_WRITE), scope="function")]

MAX_SALES = 1000
# A till clock a little ahead is tolerated; further ahead would stop the checks.
MAX_AHEAD = timedelta(minutes=5)


class SaleIn(BaseModel):
    receipt_id: str = Field(min_length=1, max_length=100, description="Unique in the branch")
    sold_at: AwareDatetime
    total: Decimal = Field(ge=-1_000_000, le=1_000_000, decimal_places=3, examples=["3.250"])
    payment: PosPayment
    till: str | None = Field(default=None, max_length=50, description="Which till, if several")


class SalesIn(BaseModel):
    branch_id: uuid.UUID
    sales: list[SaleIn] = Field(default_factory=list, max_length=MAX_SALES)
    synced_until: AwareDatetime | None = Field(
        default=None,
        description="Every sale up to this time has been sent (in this batch or before). "
        "Drawer openings are checked against sales only up to here.",
    )


class SalesResult(BaseModel):
    accepted: int
    synced_until: datetime | None = Field(description="What the branch's till is complete up to")


@router.post("/sales", response_model=SalesResult)
def receive_sales(body: SalesIn, ctx: Till) -> SalesResult:
    """Sales from a branch's till; a receipt sent again replaces the earlier one. Send
    every few minutes with `synced_until`, also when there were no sales."""
    branch = ctx.db.get(Branch, body.branch_id)
    if branch is None or not ctx.principal.can_access_branch(branch.id):
        raise not_found("branch not found")
    now = datetime.now(timezone.utc)
    if body.synced_until is not None and body.synced_until > now + MAX_AHEAD:
        raise HTTPException(422, "synced_until is in the future; check the till's clock")
    accepted = store_sales(ctx.db, branch.tenant_id, branch.id, body.sales)
    if body.synced_until is not None:
        synced = min(body.synced_until, now)
        if branch.pos_synced_until is None or synced > branch.pos_synced_until:
            branch.pos_synced_until = synced
    return SalesResult(accepted=accepted, synced_until=branch.pos_synced_until)
