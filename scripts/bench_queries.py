"""Measure the API's queries without indexes, with a composite index, and with a partial index.

    DATABASE_URL=postgresql://pulse:pulse@localhost:5432/pulsestore python scripts/bench_queries.py

For every index state and query it runs EXPLAIN (ANALYZE, BUFFERS) once per recording (all 48),
three times after a warm-up, and reports the median of the per-call mean execution time. It also
reports buffers touched and the plan for one representative recording: buffer counts, unlike
milliseconds, do not depend on the machine.

It also measures the write cost of each index state (COPY of all annotations, median of 3), and
drops and recreates the indexes itself, so run it on a database you can modify.
"""

import argparse
import json
import os
import statistics
import time

import psycopg

from app.queries import HEART_RATE_SQL, LIST_ANNOTATIONS_SQL

INDEX_STATES = {
    "no indexes": [],
    "composite": [
        "CREATE INDEX idx_ann_rec_sample ON annotations (recording_id, sample_index)",
    ],
    "composite + partial": [
        "CREATE INDEX idx_ann_rec_sample ON annotations (recording_id, sample_index)",
        """CREATE INDEX idx_ann_abnormal ON annotations (recording_id, sample_index)
           WHERE aami_class <> 'N'""",
    ],
}

QUERIES = {
    "V beats of a recording": (
        """SELECT sample_index, symbol FROM annotations
           WHERE recording_id = %(rid)s AND aami_class = 'V' ORDER BY sample_index""",
        {},
    ),
    "Abnormal beats of a recording": (
        """SELECT sample_index, symbol, aami_class FROM annotations
           WHERE recording_id = %(rid)s AND aami_class <> 'N' ORDER BY sample_index""",
        {},
    ),
    "API list: one minute of beats": (
        LIST_ANNOTATIONS_SQL,
        {"start": 360 * 600, "end": 360 * 660, "cls": None, "limit": 1000},
    ),
    "Heart rate per minute": (HEART_RATE_SQL, {}),
}


def plan_nodes(plan: dict) -> list[str]:
    """Scan node types in the plan, e.g. ['Seq Scan'] or ['Index Scan using idx_...']."""
    found = []

    def walk(node):
        kind = node["Node Type"]
        if "Scan" in kind:
            index = node.get("Index Name")
            found.append(f"{kind} ({index})" if index else kind)
        for child in node.get("Plans", []):
            walk(child)

    walk(plan)
    return found


def explain(cur, sql: str, params: dict) -> dict:
    # ClientCursor inlines the parameters, so this is the plan for these concrete values
    # (a "custom plan"), which is what EXPLAIN in psql shows too.
    cur.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, params)
    result = cur.fetchone()[0]
    return result[0] if isinstance(result, list) else json.loads(result)[0]


def set_indexes(conn, statements: list[str]) -> None:
    conn.execute("DROP INDEX IF EXISTS idx_ann_rec_sample")
    conn.execute("DROP INDEX IF EXISTS idx_ann_abnormal")
    for statement in statements:
        conn.execute(statement)
    conn.execute("ANALYZE annotations")


def copy_all(conn, rid: int, rows) -> float:
    started = time.perf_counter()
    with (
        conn.cursor() as cur,
        cur.copy(
            "COPY annotations (recording_id, sample_index, symbol, aami_class) FROM STDIN"
        ) as copy,
    ):
        for s, sym, cls in rows:
            copy.write_row((rid, s, sym, cls))
    conn.commit()
    return time.perf_counter() - started


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--representative", default="208", help="MIT-BIH record for the plans")
    args = parser.parse_args()

    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        recordings = [
            r[0]
            for r in conn.execute(
                "SELECT id FROM recordings WHERE source_record <> 'bench' ORDER BY id"
            )
        ]
        rep = conn.execute(
            "SELECT id FROM recordings WHERE source_record = %s", (args.representative,)
        ).fetchone()[0]
        rows = conn.execute(
            """SELECT sample_index, symbol, aami_class FROM annotations
               WHERE recording_id = ANY(%s)""",
            (recordings,),
        ).fetchall()

        results = []
        for state, statements in INDEX_STATES.items():
            set_indexes(conn, statements)
            index_bytes = conn.execute(
                """SELECT coalesce(sum(pg_relation_size(indexrelid)), 0) FROM pg_index
                   WHERE indrelid = 'annotations'::regclass AND NOT indisprimary"""
            ).fetchone()[0]
            cur = psycopg.ClientCursor(conn)
            for name, (sql, extra) in QUERIES.items():
                for rid in recordings:  # warm-up: every page in cache, same as later runs
                    explain(cur, sql, {"rid": rid, **extra})
                means = []
                for _ in range(args.runs):
                    per_call = [
                        explain(cur, sql, {"rid": rid, **extra})["Execution Time"]
                        for rid in recordings
                    ]
                    means.append(statistics.mean(per_call))
                rep_plan = explain(cur, sql, {"rid": rep, **extra})
                buffers = rep_plan["Plan"].get("Shared Hit Blocks", 0) + rep_plan["Plan"].get(
                    "Shared Read Blocks", 0
                )
                results.append(
                    {
                        "state": state,
                        "query": name,
                        "ms": statistics.median(means),
                        "runs": means,
                        "buffers": buffers,
                        "plan": plan_nodes(rep_plan["Plan"]),
                    }
                )

            # Write cost for this index state: COPY every annotation into a scratch recording.
            conn.autocommit = False
            dev = conn.execute(
                """INSERT INTO devices (serial_number, model) VALUES ('SIM-BENCH', 'bench')
                   ON CONFLICT (serial_number) DO UPDATE SET model = EXCLUDED.model
                   RETURNING id"""
            ).fetchone()[0]
            scratch = conn.execute(
                """INSERT INTO recordings (device_id, subject_code, source_record, lead_name,
                     sampling_rate_hz, started_at)
                   VALUES (%s, 'BENCH-001', 'bench', 'MLII', 360, now()) RETURNING id""",
                (dev,),
            ).fetchone()[0]
            conn.commit()
            writes = []
            for _ in range(args.runs):
                writes.append(copy_all(conn, scratch, rows))
                conn.execute("DELETE FROM annotations WHERE recording_id = %s", (scratch,))
                conn.commit()
                conn.autocommit = True
                conn.execute("VACUUM annotations")
                conn.autocommit = False
            conn.execute("DELETE FROM recordings WHERE id = %s", (scratch,))
            conn.commit()
            conn.autocommit = True
            results.append(
                {
                    "state": state,
                    "query": "COPY all annotations (write cost)",
                    "ms": statistics.median(writes) * 1000,
                    "runs": [w * 1000 for w in writes],
                    "buffers": None,
                    "plan": [f"indexes: {index_bytes / 1024 / 1024:.1f} MB"],
                }
            )

    print(f"{len(recordings)} recordings, {len(rows):,} annotations; median of {args.runs} runs")
    print(f"plans and buffers for MIT-BIH record {args.representative}\n")
    for r in results:
        runs = ", ".join(f"{x:.3f}" for x in r["runs"])
        buffers = "" if r["buffers"] is None else f"{r['buffers']:>5} buffers"
        print(
            f"{r['state']:<20} | {r['query']:<34} | {r['ms']:>9.3f} ms | {buffers:<13} | "
            f"{'; '.join(r['plan'])}  [{runs}]"
        )


if __name__ == "__main__":
    main()
