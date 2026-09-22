"""Traces and metrics to Application Insights, when there is one to send them to.

`setup_telemetry()` does nothing unless `APPLICATIONINSIGHTS_CONNECTION_STRING` is set, so local
runs and CI never import the Azure packages and never try to export anything. Until it is
called, the instruments below are no-ops, which means the request handlers can record metrics
unconditionally without caring whether telemetry is on.

Two instrumentations are attached explicitly rather than trusting auto-instrumentation:
FastAPI, because `configure_azure_monitor()` patches the FastAPI constructor and that produced
no request telemetry here, and psycopg 3, because the Azure package only instruments psycopg2.
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
_enabled = False


def setup_telemetry() -> bool:
    """Wire OpenTelemetry to Application Insights. Returns True when it was enabled."""
    global annotations_ingested, ingest_batch_size, _enabled

    connection_string = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING")
    if not connection_string:
        return False

    from azure.monitor.opentelemetry import configure_azure_monitor
    from opentelemetry import metrics
    from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor

    configure_azure_monitor(connection_string=connection_string)
    # Database calls as dependencies: the Azure package only instruments psycopg2.
    PsycopgInstrumentor().instrument(enable_commenter=False)
    meter = metrics.get_meter("pulsestore")
    annotations_ingested = meter.create_counter(
        "annotations_ingested", unit="1", description="Beat annotations written"
    )
    ingest_batch_size = meter.create_histogram(
        "ingest_batch_size", unit="1", description="Annotations per request"
    )
    _enabled = True
    return True


def instrument_app(app) -> None:
    """Trace HTTP requests of this app.

    `configure_azure_monitor()` instruments the FastAPI class, which only covers apps created
    afterwards; doing it on the instance is unambiguous. Measured: without this call, no request
    telemetry reached Application Insights at all.
    """
    if not _enabled:
        return
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(app)
