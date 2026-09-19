# Measured results

Every number here comes from a script in this repository and can be reproduced. Estimates are
labelled as estimates.

## Environment

| | |
|---|---|
| Machine | Apple M2, 8 GB RAM |
| Database | PostgreSQL 16.15 in Docker Desktop (arm64 VM, 8 vCPU, 3.8 GB) |
| Settings | Defaults: `shared_buffers=128MB`, `work_mem=4MB`, `synchronous_commit=on`, `random_page_cost=4`, TOAST compression `pglz` |
| Client | psycopg 3.3 over localhost TCP |

Local numbers are for comparing options against each other on the same machine. They are not a
prediction of cloud latency; Azure numbers go in their own column once the service is deployed.

## Phase 4: loading MIT-BIH through the API

`python scripts/load_mitbih.py --all`, through the public HTTP API, one record at a time.

| | |
|---|---|
| Records | 48 |
| Signal segments (10 s each) | 8,688 |
| Annotations | 109,494 (N 90,631 · S 2,781 · V 7,236 · F 803 · Q 8,043) |
| Wall time, end to end | 40.3 s |
| Database size | 74 MB |
| `signal_segments`, table + TOAST | 58 MB |
| `annotations`, table | 8.9 MB |

**Storage surprise.** The raw signal is 31.2 million samples × 4 bytes = 125 MB, but the table
takes 58 MB: TOAST compressed it 2.1×. I expected noisy floats not to compress and wrote so in
the first draft of ADR 0001. MIT-BIH samples are 11-bit ADC values scaled by a gain of 200, so
after rounding to four decimals there are few distinct values and long repeated byte patterns,
which `pglz` handles well. ADR 0001 now carries the measured figure.

**Data finding.** Records 102 and 104 have no MLII lead (only V5 and V2). The loader prefers
MLII, falls back to the first lead, and stores the lead name it actually used, which is what
the `lead_name` column is for. A classifier trained on MLII would be wrong to read those two
recordings as if they were MLII; the schema makes that visible instead of hiding it.

### Ingest: COPY vs INSERT

`python scripts/bench_ingest.py`: the same 109,494 annotation rows into the real table (with
its check constraints, foreign key and WAL), committed, median of 3 interleaved runs.

| Method | Median | Rows/s | Relative to COPY | Runs (s) |
|---|---|---|---|---|
| **COPY** | **0.52 s** | **210,419** | **1.0×** | 0.54, 0.52, 0.52 |
| `executemany` (psycopg 3 pipeline mode) | 2.06 s | 53,033 | 4.0× | 2.73, 2.06, 1.99 |
| One `INSERT` per row in a loop | 17.44 s | 6,277 | 33.5× | 17.44, 17.61, 16.72 |

- **COPY is 4× faster than the best plain-INSERT path**, which is why the API ingests
  annotations with COPY.
- **The naive loop is the real anti-pattern**, and it is 8.5× slower than `executemany`.
  psycopg 3 sends `executemany` in pipeline mode, one network round trip per batch rather than
  per row, so most of the "COPY is 30× faster" folklore is really "round trips are expensive".
- Signal segments still use `executemany`: at 20 segments per request the batch is small and
  each row is a 14 kB array, so the per-row overhead that COPY removes is not what dominates.
