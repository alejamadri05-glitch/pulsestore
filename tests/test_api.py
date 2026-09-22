import psycopg
import pytest


def beats(n: int, every_samples: int = 360, cls: str = "N") -> list[dict]:
    return [
        {"sample_index": every_samples * i, "symbol": cls, "aami_class": cls}
        for i in range(1, n + 1)
    ]


def new_recording_body(**overrides) -> dict:
    body = {
        "device_serial": "SIM-X",
        "device_model": "Holter-Sim",
        "subject_code": "TEST-002",
        "source_record": "x",
        "lead_name": "MLII",
        "sampling_rate_hz": 360,
        "started_at": "2026-01-01T00:00:00Z",
    }
    return body | overrides


# --- authentication -------------------------------------------------------------


@pytest.mark.parametrize("headers", [{}, {"x-api-key": "wrong-key"}])
def test_missing_or_wrong_api_key_is_401(client, recording, headers):
    r = client.get(f"/recordings/{recording}/heart-rate", headers=headers)
    assert r.status_code == 401


def test_healthz_needs_no_key(client):
    body = client.get("/healthz").json()
    assert body["status"] == "ok"


# --- error mapping: database errors become meaningful HTTP codes, never a 500 ---


def test_annotations_for_unknown_recording_is_404(client, auth):
    r = client.post("/recordings/999999/annotations", json={"items": beats(3)}, headers=auth)
    assert r.status_code == 404


def test_reads_for_unknown_recording_are_404_not_empty(client, auth):
    assert client.get("/recordings/999999/heart-rate", headers=auth).status_code == 404
    assert client.get("/recordings/999999/annotations", headers=auth).status_code == 404


def test_same_segment_index_twice_is_409(client, auth, recording):
    batch = {"items": [{"segment_index": 0, "start_sample": 0, "samples": [0.1] * 3600}]}
    assert (
        client.post(f"/recordings/{recording}/segments", json=batch, headers=auth).status_code
        == 201
    )
    assert (
        client.post(f"/recordings/{recording}/segments", json=batch, headers=auth).status_code
        == 409
    )


def test_duplicate_recording_is_409(client, auth):
    body = new_recording_body(device_serial="SIM-DUP", source_record="dup")
    assert client.post("/recordings", json=body, headers=auth).status_code == 201
    assert client.post("/recordings", json=body, headers=auth).status_code == 409


# --- input validation -------------------------------------------------------------


def test_invalid_aami_class_is_422(client, auth, recording):
    bad = [{"sample_index": 1, "symbol": "X", "aami_class": "X"}]
    r = client.post(f"/recordings/{recording}/annotations", json={"items": bad}, headers=auth)
    assert r.status_code == 422
    r = client.get(f"/recordings/{recording}/annotations?aami_class=X", headers=auth)
    assert r.status_code == 422


def test_oversized_batch_is_422(client, auth, recording):
    too_many = {
        "items": [{"segment_index": i, "start_sample": 0, "samples": [0.0]} for i in range(101)]
    }
    r = client.post(f"/recordings/{recording}/segments", json=too_many, headers=auth)
    assert r.status_code == 422


@pytest.mark.parametrize("code", ["Juan Perez", "juan-perez", "AB", "A" * 33])
def test_subject_code_that_is_not_a_pseudonym_is_rejected_by_the_api(client, auth, code):
    r = client.post("/recordings", json=new_recording_body(subject_code=code), headers=auth)
    assert r.status_code == 422


def test_database_rejects_a_name_even_if_the_api_let_it_through(database_url):
    """ADR 0002 promises two layers. This checks the second one on its own."""
    with psycopg.connect(database_url) as conn:
        dev = conn.execute(
            "INSERT INTO devices (serial_number, model) VALUES ('SIM-DB', 'm') RETURNING id"
        ).fetchone()[0]
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                """INSERT INTO recordings (device_id, subject_code, lead_name,
                     sampling_rate_hz, started_at)
                   VALUES (%s, 'Juan Perez', 'MLII', 360, now())""",
                (dev,),
            )


# --- analytics --------------------------------------------------------------------


def test_heart_rate_is_60_bpm_for_beats_one_second_apart(client, auth, recording):
    client.post(f"/recordings/{recording}/annotations", json={"items": beats(180)}, headers=auth)
    rows = client.get(f"/recordings/{recording}/heart-rate", headers=auth).json()
    assert rows, "no heart-rate rows"
    assert {float(r["mean_hr_bpm"]) for r in rows} == {60.0}


def test_heart_rate_ignores_non_beat_q_annotations(client, auth, recording):
    items = beats(120)
    items += [
        {"sample_index": 360 * i + 180, "symbol": "Q", "aami_class": "Q"} for i in range(1, 120)
    ]
    client.post(f"/recordings/{recording}/annotations", json={"items": items}, headers=auth)
    rows = client.get(f"/recordings/{recording}/heart-rate", headers=auth).json()
    assert {float(r["mean_hr_bpm"]) for r in rows} == {60.0}  # would be ~120 if Q counted


def test_annotation_filters_by_class_and_range(client, auth, recording):
    items = beats(20) + [
        {"sample_index": 360 * i + 100, "symbol": "V", "aami_class": "V"} for i in (3, 8, 15)
    ]
    client.post(f"/recordings/{recording}/annotations", json={"items": items}, headers=auth)
    r = client.get(
        f"/recordings/{recording}/annotations",
        params={"aami_class": "V", "start": 0, "end": 360 * 10},
        headers=auth,
    )
    assert [a["sample_index"] for a in r.json()] == [360 * 3 + 100, 360 * 8 + 100]


# --- beat distribution --------------------------------------------------------------


def post_mixed_beats(client, auth, rid):
    """5 N, 1 S, 2 V, 0 F and 3 Q, at distinct sample positions."""
    classes = ["N"] * 5 + ["S"] + ["V"] * 2 + ["Q"] * 3
    items = [
        {"sample_index": 360 * (i + 1), "symbol": c, "aami_class": c} for i, c in enumerate(classes)
    ]
    r = client.post(f"/recordings/{rid}/annotations", json={"items": items}, headers=auth)
    assert r.status_code == 201


def test_beat_distribution_counts_each_class(client, auth, recording):
    post_mixed_beats(client, auth, recording)
    r = client.get("/stats/beat-distribution", params={"recording_id": recording}, headers=auth)
    assert r.status_code == 200
    [row] = r.json()
    assert {k: row[k] for k in ("N", "S", "V", "F", "Q", "total")} == {
        "N": 5,
        "S": 1,
        "V": 2,
        "F": 0,
        "Q": 3,
        "total": 11,
    }
    assert row["recording_id"] == recording and row["lead_name"] == "MLII"


def test_recording_without_annotations_reports_zeros_not_one(client, auth, recording):
    """No annotations means no group: the LEFT JOIN yields NULLs, which must become 0."""
    r = client.get("/stats/beat-distribution", params={"recording_id": recording}, headers=auth)
    [row] = r.json()
    assert row["total"] == 0
    assert all(row[c] == 0 for c in "NSVFQ")


def test_beat_distribution_lists_every_recording_consistently(client, auth, recording):
    post_mixed_beats(client, auth, recording)
    rows = client.get("/stats/beat-distribution", headers=auth).json()
    ids = [row["recording_id"] for row in rows]
    assert recording in ids
    assert ids == sorted(ids)
    for row in rows:
        assert row["total"] == sum(row[c] for c in "NSVFQ")


def test_beat_distribution_for_unknown_recording_is_404(client, auth):
    r = client.get("/stats/beat-distribution", params={"recording_id": 999999}, headers=auth)
    assert r.status_code == 404


def test_beat_distribution_rejects_invalid_ids_and_missing_keys(client, auth):
    assert client.get("/stats/beat-distribution?recording_id=0", headers=auth).status_code == 422
    assert client.get("/stats/beat-distribution").status_code == 401


def test_healthz_is_503_when_the_database_is_unreachable(client, monkeypatch):
    """Discovered by load testing: the app must stay up and explain itself, not crash-loop."""
    from psycopg_pool import ConnectionPool

    import app.main as main

    dead = ConnectionPool("host=127.0.0.1 port=1 dbname=nope", min_size=0, max_size=1, open=False)
    monkeypatch.setattr(main, "pool", dead)
    r = client.get("/healthz")
    assert r.status_code == 503
    assert "database unreachable" in r.json()["detail"]
