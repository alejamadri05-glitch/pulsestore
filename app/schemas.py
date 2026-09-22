from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

AamiClass = Literal["N", "S", "V", "F", "Q"]

# Same rule as the CHECK constraint on recordings.subject_code (ADR 0002).
SUBJECT_CODE_PATTERN = r"^[A-Z0-9-]{3,32}$"


# --- requests --------------------------------------------------------------------
# Batch sizes are bounded here, so an oversized request is a 422 before it reaches the
# database rather than a timeout inside it.


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


# --- responses -------------------------------------------------------------------
# Declared so that /docs shows what each endpoint returns, and so a column added to a query
# cannot leak into the API without someone naming it here.


class Annotation(BaseModel):
    sample_index: int = Field(description="Offset of the beat from the start of the recording")
    symbol: str = Field(description="Original MIT-BIH annotation symbol")
    aami_class: AamiClass


class HeartRatePoint(BaseModel):
    minute: int = Field(description="Minutes since the start of the recording")
    mean_hr_bpm: float = Field(description="Mean rate over that minute, from RR intervals")
    beats: int = Field(description="Beats that contributed an interval to the mean")


class BeatDistribution(BaseModel):
    recording_id: int
    source_record: str | None
    lead_name: str
    N: int = Field(description="Normal and bundle-branch beats")
    S: int = Field(description="Supraventricular ectopic beats")
    V: int = Field(description="Ventricular ectopic beats")
    F: int = Field(description="Fusion beats")
    Q: int = Field(description="Paced or unclassifiable beats")
    total: int
