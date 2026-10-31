"""Staff attendance from face recognition on the edge box.

The box matches faces against the enrolled templates of its branch (`GET /face-templates`)
and sends only matches, as sightings: who, which camera, when. Faces that match nobody
are thrown away on the box and never leave it. The cloud turns sightings into one
attendance record per staff member and work day (first sighting = check-in, last =
check-out), so resending a sighting is harmless.
"""

from uuid import UUID

from pydantic import Field

from cctv_contracts.v1.common import ContractModel, UtcDatetime

MAX_SIGHTINGS_PER_BATCH = 500
MAX_TEMPLATES = 2000

Embedding = list[float]


class StaffSighting(ContractModel):
    staff_id: UUID
    camera_id: UUID
    seen_at: UtcDatetime
    similarity: float = Field(ge=0, le=1, description="Match score against the template")


class SightingBatch(ContractModel):
    sightings: list[StaffSighting] = Field(min_length=1, max_length=MAX_SIGHTINGS_PER_BATCH)


class RejectedItem(ContractModel):
    index: int = Field(ge=0, description="Position in the batch")
    error: str = Field(max_length=200)


class BatchResult(ContractModel):
    """Items are accepted or rejected one by one. A rejected item (unknown staff member or
    camera) will be rejected again, so the box must not resend it."""

    accepted: int = Field(ge=0)
    rejected: list[RejectedItem] = Field(default_factory=list)


class FaceTemplate(ContractModel):
    staff_id: UUID
    model_version: str = Field(max_length=50)
    embedding: Embedding = Field(min_length=64, max_length=4096)


class FaceTemplateList(ContractModel):
    """Templates of the branch's active staff with recorded consent. Sent decrypted over
    TLS; the box keeps them in memory only, never on disk, and only uses templates made
    with its own face model version."""

    templates: list[FaceTemplate] = Field(default_factory=list, max_length=MAX_TEMPLATES)
