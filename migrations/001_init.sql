-- PulseStore initial schema.
-- Design rationale: docs/adr/0001-signal-storage.md and docs/adr/0002-no-phi.md

CREATE TABLE devices (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  serial_number TEXT NOT NULL UNIQUE,
  model         TEXT NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE recordings (
  id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  device_id        BIGINT NOT NULL REFERENCES devices(id),
  -- Pseudonymous code only. Never a name, MRN or date of birth (ADR 0002).
  subject_code     TEXT NOT NULL CHECK (subject_code ~ '^[A-Z0-9-]{3,32}$'),
  source_record    TEXT,                    -- e.g. MIT-BIH '100'
  lead_name        TEXT NOT NULL,           -- e.g. 'MLII'
  sampling_rate_hz INT  NOT NULL CHECK (sampling_rate_hz > 0),
  started_at       TIMESTAMPTZ NOT NULL,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (device_id, source_record, lead_name)
);

-- Raw signal in fixed 10-second windows, one array per row (ADR 0001).
CREATE TABLE signal_segments (
  recording_id  BIGINT NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
  segment_index INT    NOT NULL CHECK (segment_index >= 0),
  start_sample  BIGINT NOT NULL CHECK (start_sample >= 0),
  samples       REAL[] NOT NULL CHECK (cardinality(samples) > 0),
  PRIMARY KEY (recording_id, segment_index)
);

-- One row per annotated beat.
-- text + CHECK instead of char(1): char(n) pads with spaces and compares with
-- surprising semantics; the PostgreSQL wiki lists it under "Don't Do This".
CREATE TABLE annotations (
  id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  recording_id BIGINT NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
  sample_index BIGINT NOT NULL CHECK (sample_index >= 0),
  symbol       TEXT   NOT NULL CHECK (length(symbol) = 1),
  aami_class   TEXT   NOT NULL CHECK (aami_class IN ('N', 'S', 'V', 'F', 'Q'))
);

-- Deliberately NO secondary indexes yet. They are added in 002_indexes.sql
-- after measuring the queries without them (docs/results.md).
--
-- Known consequence, kept on purpose until then: PostgreSQL does not index
-- foreign keys automatically, so DELETE on recordings (ON DELETE CASCADE)
-- has to scan the whole annotations table.
