from datetime import datetime, timezone
from typing import Annotated

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field


def _to_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


# Naive datetimes are rejected: NVR clocks are often wrong (see plan §0), so every
# timestamp must carry its offset and is normalised to UTC.
UtcDatetime = Annotated[AwareDatetime, AfterValidator(_to_utc)]


def _no_parent_segments(value: str) -> str:
    if ".." in value:
        raise ValueError("must not contain '..'")
    return value


# Object-storage key issued by the cloud (see media.UploadUrlResponse); the edge only echoes it.
MediaKey = Annotated[
    str,
    Field(min_length=1, max_length=512, pattern=r"^[A-Za-z0-9/_.\-]+$"),
    AfterValidator(_no_parent_segments),
]


class ContractModel(BaseModel):
    # Unknown fields are ignored so that additive changes inside v1 don't break
    # older edge devices or an older cloud.
    model_config = ConfigDict(frozen=True, extra="ignore", use_enum_values=False)
