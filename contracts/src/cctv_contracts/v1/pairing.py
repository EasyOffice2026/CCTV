from uuid import UUID

from pydantic import Field

from cctv_contracts.v1.common import ContractModel


class PairRequest(ContractModel):
    """Sent once by a new box, with the code shown in the admin panel (no token yet)."""

    pairing_code: str = Field(min_length=8, max_length=20)
    serial: str | None = Field(default=None, max_length=100)
    software_version: str | None = Field(default=None, max_length=50)


class PairResponse(ContractModel):
    """`device_token` is returned only here; the cloud keeps just its hash."""

    device_id: UUID
    tenant_id: UUID
    branch_id: UUID
    device_token: str
