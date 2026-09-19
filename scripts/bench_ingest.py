"""Benchmark annotation ingest: COPY vs executemany vs one INSERT per row.

    DATABASE_URL=postgresql://pulse:pulse@localhost:5432/pulsestore python scripts/bench_ingest.py

Uses the annotations already loaded (all 48 MIT-BIH records) as the payload, so every method
writes exactly the same rows into the real table, with its constraints, foreign key and WAL.
Each run inserts into a scratch recording and commits; the rows are then deleted and the table
vacuumed, untimed, so every run starts from the same table state. Methods are interleaved so a
warm cache or a background checkpoint does not favour one of them. Reports the median.
"""

import argparse
import os
import statistics
import time

import psycopg


def scratch_recording(conn) -> int:
    dev = conn.execute(
        """INSERT INTO devices (serial_number, model) VALUES ('SIM-BENCH', 'bench')
           ON CONFLICT (serial_number) DO UPDATE SET model = EXCLUDED.model RETURNING id"""
    ).fetchone()[0]
    rid = conn.execute(
        """INSERT INTO recordings (device_id, subject_code, source_record, lead_name,
             sampling_rate_hz, started_at)
           VALUES (%s, 'BENCH-001', 'bench', 'MLII', 360, now()) RETURNING id""",
        (dev,),
    ).fetchone()[0]
    conn.commit()
    return rid


def insert_copy(conn, rid, rows):
    with (
        conn.cursor() as cur,
        cur.copy(
            "COPY annotations (recording_id, sample_index, symbol, aami_class) FROM STDIN"
        ) as copy,
    ):
        for s, sym, cls in rows:
            copy.write_row((rid, s, sym, cls))


def insert_executemany(conn, rid, rows):
    # psycopg 3 sends executemany through pipeline mode: one network round trip per batch,
    # not per row. This is the realistic "plain INSERT" baseline today.
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO annotations (recording_id, sample_index, symbol, aami_class)
               VALUES (%s, %s, %s, %s)""",
            [(rid, s, sym, cls) for s, sym, cls in rows],
        )


def insert_row_by_row(conn, rid, rows):
    # The naive loop: one statement and one round trip per row.
    with conn.cursor() as cur:
        for s, sym, cls in rows:
            cur.execute(
                """INSERT INTO annotations (recording_id, sample_index, symbol, aami_class)
                   VALUES (%s, %s, %s, %s)""",
                (rid, s, sym, cls),
            )


METHODS = {
    "COPY": insert_copy,
    "executemany (pipeline)": insert_executemany,
    "INSERT per row": insert_row_by_row,
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()

    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        rows = conn.execute(
            """SELECT sample_index, symbol, aami_class FROM annotations
               WHERE recording_id IN (SELECT id FROM recordings WHERE source_record <> 'bench')
               ORDER BY recording_id, sample_index"""
        ).fetchall()
        rid = scratch_recording(conn)
        times: dict[str, list[float]] = {m: [] for m in METHODS}
        try:
            for _ in range(args.runs):
                for name, method in METHODS.items():
                    started = time.perf_counter()
                    method(conn, rid, rows)
                    conn.commit()
                    times[name].append(time.perf_counter() - started)
                    conn.execute("DELETE FROM annotations WHERE recording_id = %s", (rid,))
                    conn.commit()
                    conn.autocommit = True
                    conn.execute("VACUUM annotations")
                    conn.autocommit = False
        finally:
            conn.rollback()
            conn.execute("DELETE FROM annotations WHERE recording_id = %s", (rid,))
            conn.execute("DELETE FROM recordings WHERE id = %s", (rid,))
            conn.commit()

    copy_median = statistics.median(times["COPY"])
    print(f"{len(rows):,} annotation rows, median of {args.runs} runs")
    print(f"{'method':<24} {'median s':>9} {'rows/s':>10} {'vs COPY':>8}   runs")
    for name, runs in times.items():
        med = statistics.median(runs)
        detail = ", ".join(f"{t:.2f}" for t in runs)
        rate, ratio = len(rows) / med, med / copy_median
        print(f"{name:<24} {med:>9.2f} {rate:>10,.0f} {ratio:>7.1f}x   {detail}")


if __name__ == "__main__":
    main()
