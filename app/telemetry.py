"""Traces and metrics to Application Insights, when there is one to send them to.

`setup_telemetry()` does nothing unless `APPLICATIONINSIGHTS_CONNECTION_STRING` is set, so local
runs and CI never import the Azure packages and never try to export anything. Until it is
called, the instruments below are no-ops, which means the request handlers can record metrics
unconditionally without caring whether telemetry is on.

Known gap: azure-monitor-opentelemetry auto-instruments psycopg2, and this service uses
psycopg 3, so database calls do not show up as dependencies on their own. Request duration and
failures do, and the counters here cover the ingest path.
"""

import os


class _NoOpInstrument:
    """Stands in for a counter or histogram while telemetry is off."""

    def add(self, *args, **kwargs) -> None:
        pass

    def record(self, *args, **kwargs) -> None:
        pass


annotations_ingested: object = _NoOpInstrument()
ingest_batch_size: object = _NoOpInstrument()


def setup_telemetry() -> bool:
    """Wire OpenTelemetry to Application Insights. Returns True when it was enabled."""
    global annotations_ingested, ingest_batch_size

    connection_string = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING")
    if not connection_string:
        return False

    from azure.monitor.opentelemetry import configure_azure_monitor
    from opentelemetry import metrics

    # Also instruments FastAPI, so request duration, status and failures arrive without code.
    configure_azure_monitor(connection_string=connection_string)
    meter = metrics.get_meter("pulsestore")
    annotations_ingested = meter.create_counter(
        "annotations_ingested", unit="1", description="Beat annotations written"
    )
    ingest_batch_size = meter.create_histogram(
        "ingest_batch_size", unit="1", description="Annotations per request"
    )
    return True
