# Drill: firing `pg-cpu-high` on purpose

- **Date:** 2026-09-22 (times in UTC)
- **Type:** planned drill, not an incident. No user impact; the service kept answering.
- **Goal:** prove that a database problem reaches a human, and that the runbook is usable.

## What was done

Six, then eight, concurrent connections ran a query with no index to help it — a join of all
109,494 annotations with `recordings`, grouped — in a loop against the Burstable B1ms server:
**4,245 queries in 482 s** on the first run, and a second run of eight connections for nine
minutes.

## Timeline

| Time | Event |
|---|---|
| 02:33 | Baseline: CPU 14–17 % |
| 02:35 | First load run pushes CPU to 84.9 % for one minute |
| 02:37 | Alert threshold lowered from 80 % to 40 % to shorten the drill; second load run starts |
| 02:38–02:41 | CPU sustained: 80.0, 84.7, 85.7, 86.5 %; burstable credits fall 149 → 147 |
| 02:40:49 | **`pg-cpu-high` fires**, Sev2, target `pulsestore-pg-sjlnu`, email to the action group |
| 02:5x | Load ends; CPU returns to baseline; threshold restored to 80 % |

## Was the drill honest?

Partly, and the difference matters. The threshold was lowered to 40 % to avoid a long load, so
the alert that fired was evaluating 40 %. But the same window averaged **83.3 %** over five
minutes (79.8, 80.0, 84.7, 85.7, 86.5), which is above the real threshold: **the original rule
would have fired too**, about a minute later.

## What the runbook's steps returned

- `/healthz` stayed 200 throughout: the API was healthy while the database was saturated. An
  availability check alone would have shown nothing.
- `pg_stat_statements`, step 2 of the runbook, named the culprit immediately: 6,024 calls,
  **670 ms mean**, 4,038 s total — two orders of magnitude above everything else. Second place
  was a 6 ms monitoring query, third the ingest `COPY` at 156 ms.
- Application Insights showed the request side unaffected, which is consistent: no API traffic
  was running, the load came from outside the service.

## What was learned

1. **A five-minute average hides short spikes.** The first run peaked at 84.9 % for a single
   minute and would never have alerted. That is the right behaviour for CPU (nobody should be
   paged for one busy minute), but it means the alert is not a latency alarm. A p95 latency
   alert on `AppRequests` would catch what this one deliberately ignores.
2. **Burstable credits are the metric to watch on this tier.** CPU sat near 85 % while
   `cpu_credits_remaining` drifted from 149 to 147. Under a longer load, running out of credits
   throttles the server hard, and CPU percentage alone would not explain the slowdown.
3. **The expensive query was not one of the API's.** It was written for this drill, and
   `pg_stat_statements` still found it in seconds. The value of having it enabled, with
   `track = top` set explicitly, was proven by using it.
4. **`/healthz` is not a health check for the database's capacity.** It answers `SELECT 1`,
   which stays fast under CPU pressure.

## What to automate next

- A second alert on **p95 request duration** from Application Insights, which is what a user
  would actually feel.
- An alert on **`cpu_credits_remaining` below a floor**, which is the burstable-specific failure
  mode this tier can hit.
- Both are cheap (about USD 0.10 per rule per month) and neither existed before this drill.
