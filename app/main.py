import os
import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from psycopg import errors
from psycopg.rows import dict_row

from app.db import pool
from app.queries import HEART_RATE_SQL, LIST_ANNOTATIONS_SQL
from app.schemas import AamiClass, AnnotationBatch, RecordingIn, SegmentBatch


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool.open(wait=True)
    yield
    pool.close()


app = FastAPI(
    title="PulseStore",
    description=(
        "ECG recordings and beat annotations on PostgreSQL. Public, de-identified data only."
    ),
    lifespan=lifespan,
)


def require_key(x_api_key: str | None = Header(None)) -> None:
    # Optional at the framework level so a missing key is a 401 (missing credentials),
    # not a 422 (malformed request), which is what a required Header would produce.
    # compare_digest takes the same time whether the first or the last character is wrong,
    # so response timing does not leak how much of a guessed key was correct.
    if x_api_key is None or not secrets.compare_digest(
        x_api_key.encode(), os.environ["API_KEY"].encode()
    ):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def ensure_recording(conn, rid: int) -> None:
    """404 for an unknown recording, instead of an empty result that looks like no data."""
    if conn.execute("SELECT 1 FROM recordings WHERE id = %s", (rid,)).fetchone() is None:
        raise HTTPException(status_code=404, detail="Recording not found")


@app.get("/healthz")
def healthz():
    with pool.connection() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


@app.post("/recordings", status_code=201, dependencies=[Depends(require_key)])
def create_recording(body: RecordingIn):
    try:
        with pool.connection() as conn:
            dev_id = conn.execute(
                """INSERT INTO devices (serial_number, model) VALUES (%s, %s)
                   ON CONFLICT (serial_number) DO UPDATE SET model = EXCLUDED.model
                   RETURNING id""",
                (body.device_serial, body.device_model),
            ).fetchone()[0]
            rec_id = conn.execute(
                """INSERT INTO recordings (device_id, subject_code, source_record,
                     lead_name, sampling_rate_hz, started_at)
                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                (
                    dev_id,
                    body.subject_code,
                    body.source_record,
                    body.lead_name,
                    body.sampling_rate_hz,
                    body.started_at,
                ),
            ).fetchone()[0]
    except errors.UniqueViolation as exc:
        raise HTTPException(
            409, "This device already has a recording for that source record and lead"
        ) from exc
    return {"id": rec_id}


@app.post("/recordings/{rid}/segments", status_code=201, dependencies=[Depends(require_key)])
def add_segments(rid: int, batch: SegmentBatch):
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO signal_segments
                   (recording_id, segment_index, start_sample, samples)
                   VALUES (%s, %s, %s, %s)""",
                [(rid, s.segment_index, s.start_sample, s.samples) for s in batch.items],
            )
    except errors.ForeignKeyViolation as exc:
        raise HTTPException(404, "Recording not found") from exc
    except errors.UniqueViolation as exc:
        raise HTTPException(409, "Segment index already exists") from exc
    return {"inserted": len(batch.items)}


@app.post("/recordings/{rid}/annotations", status_code=201, dependencies=[Depends(require_key)])
def add_annotations(rid: int, batch: AnnotationBatch):
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            with cur.copy(
                """COPY annotations (recording_id, sample_index, symbol, aami_class)
                   FROM STDIN"""
            ) as copy:
                for a in batch.items:
                    copy.write_row((rid, a.sample_index, a.symbol, a.aami_class))
    except errors.ForeignKeyViolation as exc:
        raise HTTPException(404, "Recording not found") from exc
    return {"inserted": len(batch.items)}


@app.get("/recordings/{rid}/annotations", dependencies=[Depends(require_key)])
def list_annotations(
    rid: int,
    start: int = Query(0, ge=0),
    end: int | None = Query(None, ge=0),
    aami_class: AamiClass | None = None,
    limit: int = Query(1000, ge=1, le=5000),
):
    with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        ensure_recording(conn, rid)
        cur.execute(
            LIST_ANNOTATIONS_SQL,
            {"rid": rid, "start": start, "end": end, "cls": aami_class, "limit": limit},
        )
        return cur.fetchall()


@app.get("/recordings/{rid}/heart-rate", dependencies=[Depends(require_key)])
def heart_rate(rid: int):
    with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        ensure_recording(conn, rid)
        cur.execute(HEART_RATE_SQL, {"rid": rid})
        return cur.fetchall()
