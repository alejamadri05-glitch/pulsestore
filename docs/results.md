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
the first draft of ADR 0001. A likely reason, not measured separately: MIT-BIH samples are
11-bit ADC values divided by a gain of 200, so there are at most 2,048 distinct values and the
bytes repeat a lot, which `pglz` can exploit. ADR 0001 now carries the measured figure.

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
- Signal segments still use `executemany`, as in the guide. Not measured yet: with 20 rows of
  14 kB arrays per request, per-row overhead is probably not what dominates, but that is a guess
  until someone benchmarks it.

## Phase 6: query tuning

`python scripts/bench_queries.py` (with `PYTHONPATH=.`). Each query runs as
`EXPLAIN (ANALYZE, BUFFERS)` once per recording, for all 48, three times after a warm-up;
the table shows the median of the per-call mean. Buffers and plans are for MIT-BIH record 208.
The SQL is imported from `app/queries.py`, so what is measured is what the API sends.

**Buffers are the number to trust across machines.** Milliseconds here are a laptop with the
whole table in cache; buffers touched are a property of the plan and will be the same on Azure.

| Query | No indexes | Composite | Composite + partial | Speed-up | Buffers | Plan change |
|---|---|---|---|---|---|---|
| V beats of a recording | 3.779 ms | 0.188 ms | **0.048 ms** | 79× | 806 → 38 → 31 | Seq Scan → Bitmap (composite) → Index Scan (partial) |
| Abnormal beats of a recording | 4.736 ms | 0.224 ms | **0.050 ms** | 95× | 806 → 38 → 31 | Seq Scan → Bitmap (composite) → Index Scan (partial) |
| API list: one minute of beats | 3.408 ms | **0.018 ms** | 0.018 ms | 189× | 806 → 5 → 5 | Seq Scan → Index Scan (composite) |
| Heart rate per minute | 6.544 ms | 1.767 ms | **1.732 ms** | 3.8× | 807 → 39 → 39 | Seq Scan → Bitmap (composite), still sorts |
| **Write cost:** COPY of 109,494 rows | 490 ms | 790 ms (+61 %) | 797 ms (+63 %) | — | — | Index size 0 → 3.3 → 3.9 MB |

The indexes are in `migrations/002_indexes.sql`.

### Findings

1. **The heart-rate query barely moved (3.8×) while the others improved 80–190×.** Finding
   the rows got cheap (807 → 39 buffers), but the query's cost is the window function, the
   sort and the aggregation, not the scan. The plan is a bitmap scan followed by a sort: with
   the default `random_page_cost = 4`, the planner prefers a bitmap scan over an ordered index
   scan for ~3,000 rows, so it has to sort afterwards. Next experiment: repeat it on Azure and
   with a `random_page_cost` suited to SSD storage, where an ordered index scan could drop the
   sort. Heart rate never changes after ingest, so caching it (Phase 11) may matter more.
2. **The planner used the partial index for `aami_class = 'V'`.** The index is defined for
   `aami_class <> 'N'`, and PostgreSQL proved on its own that `'V'` implies `<> 'N'`.
3. **The composite index cost 61 % on ingest; the partial one cost 1 %.** The partial index
   covers 17 % of rows and is 0.6 MB. For 4.5× faster abnormal-beat queries it is the cheapest
   win in this table.

### The optional-filter pattern and generic plans

The API uses one statement for every filter combination:
`(%(cls)s::text IS NULL OR aami_class = %(cls)s)`. psycopg 3 prepares a statement on the server
after 5 executions, and PostgreSQL may then switch to a generic plan that does not see
parameter values. Measured with `PREPARE` and `plan_cache_mode`, record 208, all V beats:

| Plan | Index used | Execution |
|---|---|---|
| Custom (sees `'V'`) | `idx_ann_abnormal` (partial) | 0.326 ms |
| Generic (sees `$4`) | `idx_ann_rec_sample` + filter | 0.458 ms (1.4×) |

The generic plan cannot prove that an unknown `$4` excludes `'N'`, so it loses the partial
index. Here the cost is 1.4× because the composite index still bounds the scan to one
recording. **Kept as is, on purpose**: splitting the statement per filter combination would
remove a 0.13 ms penalty at the price of more code paths. It would be worth doing if a
recording held millions of beats, or for a filter without a selective leading column.

## Phase 7: security

The API connects as `pulse_app` (`migrations/003_app_role.sql`), never as the admin user. The
whole test suite runs as that role, so every test also checks that the service works without
the privileges it was not given.

**Two corrections to the build guide's role, both verified against PostgreSQL 16:**

| Guide's grant | What happens | Fix |
|---|---|---|
| `SELECT, INSERT` only | `POST /recordings` fails on **every** request: `INSERT … ON CONFLICT DO UPDATE` needs UPDATE privilege even when no conflict occurs. Removing the fix makes 10 tests fail with `permission denied for table devices`. | `GRANT UPDATE (model) ON devices`: column-level. The role can refresh a device's model but cannot rewrite its serial number (tested). |
| `USAGE ON ALL SEQUENCES` | Not needed: identity columns, unlike `serial`, do not require it. | Not granted. Least privilege means not granting what is not used. |

Tested as `pulse_app`, all refused with `InsufficientPrivilege`: `DELETE`, `TRUNCATE`,
`UPDATE devices SET serial_number`, `UPDATE recordings SET subject_code`, `DROP TABLE`,
`CREATE TABLE`, `ALTER TABLE`. The role is not a superuser and owns no relation.

Other controls:

- **SQL is parameterized everywhere.** The only composed statement is `ALTER ROLE … PASSWORD`,
  which cannot take bound parameters; it uses `psycopg.sql.Literal`, not string formatting.
- **API keys are compared in constant time** (`secrets.compare_digest`), and a missing key is
  401, not 422.
- **No secrets in git:** `.env` is ignored and `.env.example` documents every variable. The
  role's password is set outside the migration, from a secret store.
- **Dependabot** watches pip and GitHub Actions weekly.
- **Pending, needs the GitHub side:** secret scanning and branch protection are repository
  settings, not files.
