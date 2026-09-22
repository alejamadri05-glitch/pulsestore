"""Telemetry is optional: the service must work, and record, with it off."""

from app import telemetry


def test_disabled_without_a_connection_string(monkeypatch):
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)
    assert telemetry.setup_telemetry() is False


def test_instruments_are_safe_to_call_while_disabled():
    telemetry.annotations_ingested.add(5)
    telemetry.ingest_batch_size.record(5)


def test_ingest_records_the_batch_size(client, auth, recording, monkeypatch):
    """The counter is fed by the endpoint, whether or not telemetry is exporting."""

    class Spy:
        def __init__(self):
            self.values = []

        def add(self, value, *args, **kwargs):
            self.values.append(value)

        record = add

    counter, histogram = Spy(), Spy()
    monkeypatch.setattr(telemetry, "annotations_ingested", counter)
    monkeypatch.setattr(telemetry, "ingest_batch_size", histogram)
    items = [{"sample_index": 360 * i, "symbol": "N", "aami_class": "N"} for i in range(1, 8)]
    r = client.post(f"/recordings/{recording}/annotations", json={"items": items}, headers=auth)
    assert r.status_code == 201
    assert counter.values == [7]
    assert histogram.values == [7]


def test_healthz_reports_whether_telemetry_is_on(client):
    assert client.get("/healthz").json() == {"status": "ok", "telemetry": False}
