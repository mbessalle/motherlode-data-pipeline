"""OpenTelemetry tracing.

Every article is one trace: scrape -> extract -> validate -> store/review, with the LLM calls as child spans
that carry the GenAI semantic-convention attributes (model, input/output tokens).

Where spans go is decided by the standard environment variables:
  OTEL_EXPORTER_OTLP_ENDPOINT set  -> OTLP/HTTP to that collector (Jaeger, Tempo, Langfuse, Phoenix, ...)
  PIPELINE_TRACE_CONSOLE=1         -> printed to stdout
  neither                          -> spans are created but not exported (no overhead worth measuring)
"""
import os
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SimpleSpanProcessor

_provider: TracerProvider | None = None


def setup(service_name: str = "motherlode-data-pipeline", extra_processor=None) -> None:
    """Create the tracer provider once. `extra_processor` adds one more destination (tests capture spans this way)."""
    global _provider
    if _provider is None:
        _provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
        if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            _provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        if os.getenv("PIPELINE_TRACE_CONSOLE") == "1":
            _provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    if extra_processor is not None:
        _provider.add_span_processor(extra_processor)


def tracer():
    return (_provider or trace.get_tracer_provider()).get_tracer("motherlode.pipeline")


@contextmanager
def span(name: str, **attributes):
    """A span whose attributes skip None values, and that records the exception if the block fails."""
    with tracer().start_as_current_span(name) as s:
        for key, value in attributes.items():
            if value is not None:
                s.set_attribute(key, value)
        yield s


def progress(article_id, step: str, detail: str = "") -> None:
    """One line per step, printed as it happens, so a run can be followed live in the log.
    Traces answer "why" afterwards; this answers "what is it doing right now"."""
    print(f"[pipeline] {article_id} | {step:<9} | {detail}", flush=True)


def shutdown() -> None:
    """Flush and stop exporting. Call once at the end of a run."""
    if _provider is not None:
        _provider.shutdown()
