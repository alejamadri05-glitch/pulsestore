# ADR 0001: Store the raw signal as 10-second array segments

- **Status:** accepted
- **Date:** 2026-09-19

## Context

A MIT-BIH record is 30 minutes of two-lead ECG at 360 Hz: about 650,000 samples per lead. The
full database is 48 records, so roughly 31 million samples for the one lead PulseStore stores.
The service needs to (a) ingest signal from devices in batches, (b) return a time window of
signal for display, and (c) never do per-sample analytics in SQL. Beat-level analytics run on
the `annotations` table, not on the raw signal.

## Options considered

| Option | Rows for 48 records | Read a 10 s window | Notes |
|---|---|---|---|
| One row per sample `(recording_id, sample_index, value)` | ~31 million | Index range scan over 3,600 rows | Row overhead (~24 bytes header + alignment) dwarfs a 4-byte value; the table is mostly overhead |
| **Array per fixed segment** `(recording_id, segment_index, samples REAL[])` | ~8,700 | One row, one primary-key lookup | A 3,600-sample array is ~14 kB, above the ~2 kB TOAST threshold: it is stored out of line, and compressed only if it compresses (noisy floats often don't) |
| One blob per recording (`bytea` or large object) | 48 | Fetch and slice the whole blob | Cheapest to store, but every read pulls 30 minutes to show 10 seconds, and partial ingest is awkward |

## Decision

Store the signal as **10-second segments** (3,600 samples at 360 Hz) in a `REAL[]` column, keyed
by `(recording_id, segment_index)`.

## Consequences

- **Good:** row count drops by a factor of 3,600, a display window is a single primary-key
  lookup, and batched ingest maps naturally onto segments.
- **Good:** values stay in physical units (mV), so the schema is independent of each device's
  ADC gain and baseline.
- **Bad:** SQL cannot filter or aggregate individual samples efficiently. That is acceptable
  because nothing in the product needs it; if it ever does, `unnest()` works at a cost.
- **Bad:** a window that crosses a segment boundary needs two rows. Clients ask for whole
  segments, so this has not come up.
- **Revisit if:** sampling rates vary a lot across devices (a fixed sample count would then
  mean different durations), or per-sample queries become a requirement.

## Alternative noted, not taken

MIT-BIH samples are 11-bit integers. Storing the raw ADC units as `SMALLINT[]` plus the gain
and baseline on `recordings` would be lossless and about half the size of `REAL[]`. It was not
taken because the API accepts physical units from any device, and conversion would push each
device's calibration into the database. The storage difference can be measured if size becomes
a constraint.
