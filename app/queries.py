"""SQL used by the API. Kept in one module so scripts/bench_queries.py measures exactly the
statements the service sends, not a copy that can drift."""

# Optional filters use the "(param IS NULL OR column = param)" pattern: one statement for every
# combination of filters. docs/results.md measures what that costs once a plan is cached.
LIST_ANNOTATIONS_SQL = """
SELECT sample_index, symbol, aami_class FROM annotations
WHERE recording_id = %(rid)s AND sample_index >= %(start)s
  AND (%(end)s::bigint IS NULL OR sample_index < %(end)s)
  AND (%(cls)s::text IS NULL OR aami_class = %(cls)s)
ORDER BY sample_index
LIMIT %(limit)s
"""

HEART_RATE_SQL = """
WITH beats AS (
  SELECT a.sample_index,
         r.sampling_rate_hz AS fs,
         (a.sample_index - lag(a.sample_index) OVER (ORDER BY a.sample_index))::float
           / r.sampling_rate_hz AS rr_s
  FROM annotations a
  JOIN recordings r ON r.id = a.recording_id
  WHERE a.recording_id = %(rid)s AND a.aami_class <> 'Q'
)
SELECT floor(sample_index / (fs * 60.0))::int AS minute,
       round((60.0 / avg(rr_s))::numeric, 1)   AS mean_hr_bpm,
       count(*)                                 AS beats
FROM beats
WHERE rr_s IS NOT NULL
GROUP BY 1
ORDER BY 1;
"""

# Beat distribution: one row per recording, one column per AAMI class. Two statements on
# purpose, both measured (docs/results.md):
#  * All recordings: aggregate the annotations first (48 groups), then join. Joining 109,494
#    rows first and aggregating after was 1.5x slower (32.9 -> 21.6 ms).
#  * One recording: a plain equality, which PostgreSQL pushes inside the aggregation so the
#    index is used (0.28 ms). The optional-filter form "(rid IS NULL OR r.id = rid)" cannot be
#    pushed down and aggregated all 48 recordings to return one (15.3 ms).
# coalesce(): a recording without annotations has no group, so the LEFT JOIN yields NULLs.
# Written out in full rather than composed with f-strings, so that "no string-built SQL" stays
# true without exceptions. The two differ only in the WHERE clause.
BEAT_DISTRIBUTION_ALL_SQL = """
SELECT r.id AS recording_id, r.source_record, r.lead_name,
       coalesce(c.n, 0) AS "N", coalesce(c.s, 0) AS "S", coalesce(c.v, 0) AS "V",
       coalesce(c.f, 0) AS "F", coalesce(c.q, 0) AS "Q", coalesce(c.total, 0) AS total
FROM recordings r
LEFT JOIN (SELECT recording_id,
                  count(*) FILTER (WHERE aami_class = 'N') AS n,
                  count(*) FILTER (WHERE aami_class = 'S') AS s,
                  count(*) FILTER (WHERE aami_class = 'V') AS v,
                  count(*) FILTER (WHERE aami_class = 'F') AS f,
                  count(*) FILTER (WHERE aami_class = 'Q') AS q,
                  count(*) AS total
           FROM annotations GROUP BY recording_id) c ON c.recording_id = r.id
ORDER BY r.id
"""

BEAT_DISTRIBUTION_ONE_SQL = """
SELECT r.id AS recording_id, r.source_record, r.lead_name,
       coalesce(c.n, 0) AS "N", coalesce(c.s, 0) AS "S", coalesce(c.v, 0) AS "V",
       coalesce(c.f, 0) AS "F", coalesce(c.q, 0) AS "Q", coalesce(c.total, 0) AS total
FROM recordings r
LEFT JOIN (SELECT recording_id,
                  count(*) FILTER (WHERE aami_class = 'N') AS n,
                  count(*) FILTER (WHERE aami_class = 'S') AS s,
                  count(*) FILTER (WHERE aami_class = 'V') AS v,
                  count(*) FILTER (WHERE aami_class = 'F') AS f,
                  count(*) FILTER (WHERE aami_class = 'Q') AS q,
                  count(*) AS total
           FROM annotations GROUP BY recording_id) c ON c.recording_id = r.id
WHERE r.id = %(rid)s
"""
