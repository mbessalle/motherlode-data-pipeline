"""OpenTelemetry tracing.

Every article is one trace: scrape -> extract -> validate -> store/review, with the LLM calls as child spans
that carry the GenAI semantic-convention attributes (model, input/output tokens).

Where spans go is decided by the standard environment variables:
  OTEL_EXPORTER_OTLP_ENDPOINT set  -> OTLP/HTTP to that collector (Jaeger, Tempo, Langfuse, Phoenix, ...)
  PIPELINE_TRACE_CONSOLE=1         -> printed to stdout
  PIPELINE_TRACE_FILE=path         -> appended to that file, one JSON object per finished span, for code to query
  none of these                    -> spans are created but not exported (no overhead worth measuring)

PIPELINE_TRACE_CONTENT=1 also records the prompt and the reply of each model call on its span (cut at
CONTENT_CHARS), as the GenAI conventions' opt-in `gen_ai.input.messages` / `gen_ai.output.messages`. It is off by
default because announcements are long and a production trace backend need not hold them.
"""
import json
import os
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)

CONTENT_CHARS = 6000
_provider: TracerProvider | None = None


class JsonlSpanExporter(SpanExporter):
    """Each finished span as one JSON line: name, ids, parent, duration, status, attributes, events."""

    def __init__(self, path: str):
        self.file = open(path, "a", encoding="utf-8")

    def export(self, spans) -> SpanExportResult:
        for s in spans:
            self.file.write(json.dumps({
                "name": s.name, "trace_id": format(s.context.trace_id, "032x"), "span_id": format(s.context.span_id, "016x"),
                "parent_id": format(s.parent.span_id, "016x") if s.parent else None,
                "duration_ms": round((s.end_time - s.start_time) / 1e6, 1),
                "status": s.status.status_code.name, "attributes": dict(s.attributes),
                "events": [{"name": e.name, "attributes": dict(e.attributes)} for e in s.events],
            }, ensure_ascii=False, default=str) + "\n")
        self.file.flush()
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        self.file.close()


def record_content() -> bool:
    return os.getenv("PIPELINE_TRACE_CONTENT") == "1"


def clip(text: str) -> str:
    return text if len(text) <= CONTENT_CHARS else text[:CONTENT_CHARS] + f"... [{len(text)} chars]"


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
        if os.getenv("PIPELINE_TRACE_FILE"):
            _provider.add_span_processor(SimpleSpanProcessor(JsonlSpanExporter(os.environ["PIPELINE_TRACE_FILE"])))
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
