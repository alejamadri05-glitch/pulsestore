from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

AamiClass = Literal["N", "S", "V", "F", "Q"]

# Same rule as the CHECK constraint on recordings.subject_code (ADR 0002).
SUBJECT_CODE_PATTERN = r"^[A-Z0-9-]{3,32}$"


class RecordingIn(BaseModel):
    device_serial: str = Field(min_length=1, max_length=64)
    device_model: str = Field(min_length=1, max_length=64)
    subject_code: str = Field(pattern=SUBJECT_CODE_PATTERN)
    source_record: str | None = Field(default=None, max_length=32)
    lead_name: str = Field(min_length=1, max_length=16)
    sampling_rate_hz: int = Field(gt=0, le=10_000)
    started_at: datetime


class SegmentIn(BaseModel):
    segment_index: int = Field(ge=0)
    start_sample: int = Field(ge=0)
    samples: list[float] = Field(min_length=1, max_length=20_000)


class SegmentBatch(BaseModel):
    items: list[SegmentIn] = Field(min_length=1, max_length=100)


class AnnotationIn(BaseModel):
    sample_index: int = Field(ge=0)
    symbol: str = Field(min_length=1, max_length=1)
    aami_class: AamiClass


class AnnotationBatch(BaseModel):
    items: list[AnnotationIn] = Field(min_length=1, max_length=50_000)
