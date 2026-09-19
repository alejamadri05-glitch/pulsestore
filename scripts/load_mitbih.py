"""Load MIT-BIH into PulseStore through its public API, the way a device fleet would.

    python scripts/load_mitbih.py            # records 100, 101, 103
    python scripts/load_mitbih.py --all      # all 48 records
    python scripts/load_mitbih.py 114 208    # specific records

Reads the records from a local copy (MITDB_DIR, default data/mitdb). With --download it first
fetches PhysioNet's official zip in a single request and verifies every file against the
SHA256SUMS.txt shipped inside it. Fetching record by record from PhysioNet is avoided on
purpose: it issues hundreds of requests and PhysioNet answers bursts with 502 Bad Gateway.
"""

import argparse
import hashlib
import io
import os
import sys
import time
import urllib.request
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import requests
import wfdb

API = os.getenv("API_URL", "http://localhost:8000")
MITDB_DIR = Path(os.getenv("MITDB_DIR", "data/mitdb"))
MITDB_ZIP = (
    "https://physionet.org/static/published-projects/mitdb/mit-bih-arrhythmia-database-1.0.0.zip"
)
PREFERRED_LEAD = "MLII"
SEGMENT_SECONDS = 10
SEGMENTS_PER_REQUEST = 20

AAMI = {
    **dict.fromkeys("NLRej", "N"),
    **dict.fromkeys("AaJS", "S"),
    **dict.fromkeys("VE", "V"),
    "F": "F",
    **dict.fromkeys("/fQ", "Q"),
}


def download(dest: Path) -> None:
    """Official zip, one request, every file checked against PhysioNet's SHA-256 manifest."""
    if (dest / "234.atr").exists():
        return
    print(f"Downloading {MITDB_ZIP} (77 MB)…", flush=True)
    with urllib.request.urlopen(MITDB_ZIP, timeout=120) as resp:  # noqa: S310 - fixed https URL
        archive = zipfile.ZipFile(io.BytesIO(resp.read()))
    root = archive.namelist()[0].split("/")[0]
    expected = {}
    for line in archive.read(f"{root}/SHA256SUMS.txt").decode().splitlines():
        if line.strip():
            digest, name = line.split(maxsplit=1)
            expected[name] = digest
    dest.mkdir(parents=True, exist_ok=True)
    for name, digest in expected.items():
        wanted = name == "RECORDS" or Path(name).suffix in {".dat", ".hea", ".atr"}
        if "/" in name or not wanted:
            continue
        content = archive.read(f"{root}/{name}")
        if hashlib.sha256(content).hexdigest() != digest:
            raise ValueError(f"{name}: SHA-256 does not match PhysioNet's manifest")
        (dest / name).write_bytes(content)


def load(record: str, session: requests.Session) -> dict:
    rec = wfdb.rdrecord(str(MITDB_DIR / record))
    ann = wfdb.rdann(str(MITDB_DIR / record), "atr")
    # Look the lead up by name: in record 114 MLII is the second channel, not the first.
    # Records 102 and 104 have no MLII at all (V5 and V2 only). A storage service keeps what
    # the device recorded and says which lead it is, so those fall back to their first lead.
    lead = PREFERRED_LEAD if PREFERRED_LEAD in rec.sig_name else rec.sig_name[0]
    signal = rec.p_signal[:, rec.sig_name.index(lead)]
    fs = int(rec.fs)
    seg_len = SEGMENT_SECONDS * fs

    r = session.post(
        f"{API}/recordings",
        json={
            "device_serial": f"SIM-{record}",
            "device_model": "Holter-Sim",
            "subject_code": f"MITBIH-{record}",
            "source_record": record,
            "lead_name": lead,
            "sampling_rate_hz": fs,
            "started_at": datetime(2026, 1, 1, tzinfo=UTC).isoformat(),
        },
    )
    r.raise_for_status()
    rid = r.json()["id"]

    segments = [
        {
            "segment_index": i,
            "start_sample": i * seg_len,
            "samples": [round(float(x), 4) for x in signal[i * seg_len : (i + 1) * seg_len]],
        }
        for i in range((len(signal) + seg_len - 1) // seg_len)
    ]
    for b in range(0, len(segments), SEGMENTS_PER_REQUEST):
        session.post(
            f"{API}/recordings/{rid}/segments",
            json={"items": segments[b : b + SEGMENTS_PER_REQUEST]},
        ).raise_for_status()

    items = [
        {"sample_index": int(s), "symbol": sym, "aami_class": AAMI[sym]}
        for s, sym in zip(ann.sample, ann.symbol, strict=True)
        if sym in AAMI  # drops non-beat marks such as '+' (rhythm change) and '~' (noise)
    ]
    session.post(f"{API}/recordings/{rid}/annotations", json={"items": items}).raise_for_status()
    return {
        "record": record,
        "id": rid,
        "lead": lead,
        "segments": len(segments),
        "beats": len(items),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("records", nargs="*")
    parser.add_argument("--all", action="store_true", help="load all 48 records")
    parser.add_argument("--download", action="store_true", help="fetch the verified zip first")
    args = parser.parse_args()

    if args.download:
        download(MITDB_DIR)
    if args.all:
        listing = MITDB_DIR / "RECORDS"
        records = (
            listing.read_text().split()
            if listing.exists()
            else sorted(p.stem for p in MITDB_DIR.glob("*.hea"))
        )
    else:
        records = args.records or ["100", "101", "103"]

    session = requests.Session()
    session.headers["x-api-key"] = os.environ["API_KEY"]
    started = time.perf_counter()
    total_beats = 0
    for record in records:
        info = load(record, session)
        total_beats += info["beats"]
        print(
            f"record {record}: id={info['id']}, lead {info['lead']}, "
            f"{info['segments']} segments, {info['beats']} beats"
        )
    elapsed = time.perf_counter() - started
    print(f"{len(records)} records, {total_beats} beats in {elapsed:.1f} s", file=sys.stderr)


if __name__ == "__main__":
    main()
