import logging
import os
import re
from dataclasses import dataclass
from time import perf_counter

from fastapi import FastAPI
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.metrics import Counter, Histogram, get_meter_provider, set_meter_provider
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import (
    BatchLogRecordProcessor,
    ConsoleLogRecordExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    ConsoleMetricExporter,
    MetricReader,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)
from opentelemetry.trace import set_tracer_provider
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request


INSTRUMENTATION_NAME = "app.telemetry"
SERVICE_NAME = "order-tracker"
ORDER_LOGGER_NAME = "app.orders"
ORDER_LOOKUP_ROUTE = "/api/orders/{order_id}"
ORDER_LOOKUP_PATH = re.compile(r"^/api/orders/[^/]+$")
TELEMETRY_ENV = "ORDER_TRACKER_TELEMETRY"
EXPORT_INTERVAL_ENV = "OTEL_METRIC_EXPORT_INTERVAL"
OTLP_ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_ENDPOINT"
OTLP_INSECURE_ENV = "OTEL_EXPORTER_OTLP_INSECURE"
SERVICE_NAME_ENV = "OTEL_SERVICE_NAME"
DISABLED_MODES = {"none", "off", "disabled"}
OTLP_MODE = "otlp"
DEFAULT_EXPORT_INTERVAL_MS = 5000.0
DEFAULT_OTLP_ENDPOINT = "http://localhost:4317"
EXCLUDED_URLS = "healthz"


@dataclass(frozen=True)
class LookupInstruments:
    requests: Counter
    duration: Histogram


_tracer_provider = None
_meter_provider = None
_logger_provider = None
_log_handler = logging.NullHandler()
_logger = None
_instruments = None
_configured = False


def _telemetry_mode():
    return os.getenv(TELEMETRY_ENV, "console").strip().lower()


def _telemetry_disabled():
    return _telemetry_mode() in DISABLED_MODES


def _use_otlp():
    return _telemetry_mode() == OTLP_MODE


def _service_name():
    return os.getenv(SERVICE_NAME_ENV, "").strip() or SERVICE_NAME


def _otlp_endpoint():
    return os.getenv(OTLP_ENDPOINT_ENV, "").strip() or DEFAULT_OTLP_ENDPOINT


def _otlp_insecure(endpoint):
    raw = os.getenv(OTLP_INSECURE_ENV, "").strip().lower()
    if raw in {"true", "false"}:
        return raw == "true"
    return endpoint.startswith("http://")


def _export_interval_millis():
    raw = os.getenv(EXPORT_INTERVAL_ENV, "").strip()
    if not raw:
        return DEFAULT_EXPORT_INTERVAL_MS
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_EXPORT_INTERVAL_MS


def get_lookup_instruments() -> LookupInstruments:
    global _instruments
    if _instruments is None:
        meter = get_meter_provider().get_meter(INSTRUMENTATION_NAME)
        _instruments = LookupInstruments(
            requests=meter.create_counter(
                "order.lookup.requests",
                unit="{lookup}",
                description="Order lookup requests by route and HTTP status code.",
            ),
            duration=meter.create_histogram(
                "order.lookup.duration",
                unit="s",
                description="Order lookup latency by route and HTTP status code.",
            ),
        )
    return _instruments


def get_order_logger() -> logging.Logger:
    global _logger
    if _logger is None:
        logger = logging.getLogger(ORDER_LOGGER_NAME)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logger.addHandler(_log_handler)
        _logger = logger
    return _logger


def _severity_for(status_code):
    if status_code >= 500:
        return logging.ERROR
    if status_code >= 400:
        return logging.WARNING
    return logging.INFO


def _outcome_for(status_code, error):
    if error is not None or status_code >= 500:
        return "error"
    return "found" if status_code < 400 else "not_found"


class OrderLookupTelemetryMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method != "GET" or not ORDER_LOOKUP_PATH.match(request.scope.get("path", "")):
            return await call_next(request)

        started = perf_counter()
        try:
            response = await call_next(request)
        except BaseException as error:
            self._record(request, started, 500, error)
            raise
        self._record(request, started, response.status_code)
        return response

    def _record(self, request, started, status_code, error=None):
        attributes = {
            "http.route": ORDER_LOOKUP_ROUTE,
            "http.request.method": request.method,
            "http.response.status_code": status_code,
        }
        if error is not None:
            attributes["error.type"] = type(error).__qualname__

        instruments = get_lookup_instruments()
        instruments.requests.add(1, attributes)
        instruments.duration.record(perf_counter() - started, attributes)

        log_attributes = dict(attributes)
        log_attributes["order.lookup.outcome"] = _outcome_for(status_code, error)
        order_id = request.path_params.get("order_id")
        if order_id is not None:
            log_attributes["order.id"] = order_id
        get_order_logger().log(
            _severity_for(status_code),
            "order lookup completed",
            extra=log_attributes,
        )


def _build_span_processor(endpoint, insecure):
    if _use_otlp():
        return BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=insecure))
    return SimpleSpanProcessor(ConsoleSpanExporter())


def _build_metric_exporter(endpoint, insecure):
    if _use_otlp():
        return OTLPMetricExporter(endpoint=endpoint, insecure=insecure)
    return ConsoleMetricExporter()


def _build_log_processor(endpoint, insecure):
    if _use_otlp():
        return BatchLogRecordProcessor(OTLPLogExporter(endpoint=endpoint, insecure=insecure))
    return SimpleLogRecordProcessor(ConsoleLogRecordExporter())


def configure_telemetry(metric_reader: MetricReader | None = None):
    global _configured, _tracer_provider, _meter_provider, _logger_provider, _log_handler

    if _configured:
        return
    _configured = True

    if _telemetry_disabled():
        return

    resource = Resource.create({"service.name": _service_name()})
    endpoint = _otlp_endpoint()
    insecure = _otlp_insecure(endpoint)

    _tracer_provider = TracerProvider(resource=resource)
    _tracer_provider.add_span_processor(_build_span_processor(endpoint, insecure))
    set_tracer_provider(_tracer_provider)

    reader = metric_reader or PeriodicExportingMetricReader(
        _build_metric_exporter(endpoint, insecure),
        export_interval_millis=_export_interval_millis(),
    )
    _meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
    set_meter_provider(_meter_provider)

    _logger_provider = LoggerProvider(resource=resource)
    _logger_provider.add_log_record_processor(_build_log_processor(endpoint, insecure))
    set_logger_provider(_logger_provider)

    _log_handler = LoggingHandler(level=logging.NOTSET, logger_provider=_logger_provider)


def instrument_app(app: FastAPI):
    configure_telemetry()
    app.add_middleware(OrderLookupTelemetryMiddleware)
    FastAPIInstrumentor.instrument_app(app, excluded_urls=EXCLUDED_URLS)


def shutdown_telemetry(app: FastAPI):
    global _configured, _tracer_provider, _meter_provider, _logger_provider
    FastAPIInstrumentor.uninstrument_app(app)
    for provider in (_tracer_provider, _meter_provider, _logger_provider):
        if provider is not None:
            provider.shutdown()
    _tracer_provider = None
    _meter_provider = None
    _logger_provider = None
    _configured = False