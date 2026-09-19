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
    assert client.get("/healthz").json() == {"status": "ok"}


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
