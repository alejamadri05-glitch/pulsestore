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
