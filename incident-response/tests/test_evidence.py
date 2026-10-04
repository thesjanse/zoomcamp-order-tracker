from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx

from incident_response.agent import (
    _TRUNCATED_AT,
    AgentRunner,
    _application_frames,
    _branch_name,
    _is_truncated,
    _parse_events,
)
from incident_response.evidence import EvidenceCollector
from incident_response.models import GrafanaWebhook

LOKI_RESPONSE = {
    "status": "success",
    "data": {
        "resultType": "streams",
        "result": [
            {
                "stream": {
                    "service_name": "order-tracker",
                    "http_route": "/api/orders/{order_id}",
                    "http_request_method": "GET",
                    "http_response_status_code": "500",
                    "order_lookup_outcome": "error",
                    "detected_level": "error",
                    "error_type": "ValueError",
                    "order_id": "express-1002",
                    "trace_id": "9c20142499b0bc306632651a2ef5bfb4",
                    "span_id": "d45a959885f4d535",
                    "severity_text": "ERROR",
                },
                "values": [["1791124800845990656", "order lookup completed"]],
            }
        ],
    },
}

TEMPO_RESPONSE = {
    "trace": {
        "resourceSpans": [
            {
                "resource": {"attributes": [
                    {"key": "service.name", "value": {"stringValue": "order-tracker"}},
                ]},
                "scopeSpans": [
                    {"spans": [
                        {
                            "name": "GET /api/orders/{order_id}",
                            "kind": 2,
                            "startTimeUnixNano": "1791124800800000000",
                            "endTimeUnixNano": "1791124800863000000",
                            "status": {"code": "STATUS_CODE_ERROR"},
                            "attributes": [
                                {"key": "http.route", "value": {"stringValue": "/api/orders/{order_id}"}},
                                {"key": "http.target", "value": {"stringValue": "/api/orders/express-1002"}},
                                {"key": "http.method", "value": {"stringValue": "GET"}},
                                {"key": "http.status_code", "value": {"intValue": "500"}},
                            ],
                            "events": [
                                {"name": "exception", "attributes": [
                                    {"key": "exception.type", "value": {"stringValue": "ValueError"}},
                                    {"key": "exception.message",
                                     "value": {"stringValue": "day is out of range for month"}},
                                    {"key": "exception.stacktrace", "value": {"stringValue": (
                                        'Traceback (most recent call last):\n'
                                        '  File "/app/.venv/lib/python3.12/site-packages/fastapi/routing.py", line 1734, in app\n'
                                        '    response = await func(request)\n'
                                        '  File "/app/app/main.py", line 60, in order_detail\n'
                                        '    estimated_at = placed_at.replace(day=placed_at.day + 2)\n'
                                        'ValueError: day is out of range for month'
                                    )}},
                                ]},
                            ],
                        }
                    ]},
                ],
            }
        ],
    },
}

PROM_RESPONSE = {
    "status": "success",
    "data": {
        "resultType": "vector",
        "result": [{"metric": {"http_response_status_code": "500"}, "value": [1791127286.3, "3"]}],
    },
}


def _collector(handler, **overrides):
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    kwargs = dict(
        loki_url="http://loki:3100", tempo_url="http://tempo:3200",
        prometheus_url="http://prom:9090", lookback_seconds=900,
        max_traces=3, max_log_records=100,
    )
    kwargs.update(overrides)
    return client, EvidenceCollector(client, **kwargs)


def _handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.startswith("/loki/"):
        return httpx.Response(200, json=LOKI_RESPONSE)
    if request.url.path.startswith("/api/v2/traces/"):
        return httpx.Response(200, json=TEMPO_RESPONSE)
    if request.url.path == "/api/v1/query":
        return httpx.Response(200, json=PROM_RESPONSE)
    return httpx.Response(404)


def test_collects_all_three_signals_and_correlates_by_trace_id():
    async def go():
        client, collector = _collector(_handler)
        async with client:
            return await collector.collect(
                alert={}, service="order-tracker",
                route="/api/orders/{order_id}", method="GET",
                started_at=datetime.now(timezone.utc),
            )

    evidence = asyncio.run(go())
    assert evidence.errors == []

    # Log records, including the trace id that ties them to Tempo.
    assert evidence.logs["count"] == 1
    record = evidence.logs["records"][0]
    assert record["order_id"] == "express-1002"
    assert record["error_type"] == "ValueError"
    assert record["trace_id"] == "9c20142499b0bc306632651a2ef5bfb4"

    # The trace was fetched by that id, not by a TraceQL search.
    assert evidence.traces["trace_ids"] == ["9c20142499b0bc306632651a2ef5bfb4"]
    trace = evidence.traces["traces"][0]
    assert trace["spans"][0]["duration_ms"] == 63.0
    assert trace["spans"][0]["http_target"] == "/api/orders/express-1002"
    assert trace["root_cause"]["type"] == "ValueError"
    assert "main.py" in trace["root_cause"]["stacktrace"]

    assert evidence.metrics["results"]["server_errors_total"]["series"][0]["value"] == 3.0


def test_loki_query_filters_on_structured_metadata_not_labels():
    """Only service_name and service_instance_id are indexed labels in Loki.

    The OpenTelemetry attributes arrive as structured metadata, so selecting them
    inside the stream selector returns nothing. They have to come after a pipe.
    """
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params["query"])
        return _handler(request)

    async def go():
        client, collector = _collector(handler)
        async with client:
            await collector.collect(
                alert={}, service="order-tracker",
                route="/api/orders/{order_id}", method="GET",
                started_at=datetime.now(timezone.utc),
            )

    asyncio.run(go())
    query = next(q for q in seen if "order_lookup_outcome" in q)
    assert query.startswith('{service_name="order-tracker"} |')
    assert 'http_route="/api/orders/{order_id}"' in query
    assert 'http_request_method="GET"' in query
    assert 'order_lookup_outcome="error"' in query
    assert "order_lookup_outcome" not in query.split("|")[0]


def test_prometheus_queries_are_scoped_to_the_alerting_endpoint():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/query":
            seen.append(request.url.params["query"])
        return _handler(request)

    async def go():
        client, collector = _collector(handler)
        async with client:
            await collector.collect(
                alert={}, service="order-tracker",
                route="/api/orders/{order_id}", method="GET",
                started_at=datetime.now(timezone.utc),
            )

    asyncio.run(go())
    assert seen
    for query in seen:
        assert 'service_name="order-tracker"' in query
        assert 'http_route="/api/orders/{order_id}"' in query


def test_application_frames_drop_the_framework():
    stacktrace = TEMPO_RESPONSE["trace"]["resourceSpans"][0]["scopeSpans"][0]["spans"][0][
        "events"
    ][0]["attributes"][2]["value"]["stringValue"]
    frames = _application_frames(stacktrace)
    rendered = "\n".join(frames)
    assert "/app/app/main.py" in rendered
    assert "site-packages" not in rendered
    assert rendered.rstrip().endswith("ValueError: day is out of range for month")
    # The prologue is not a frame and must not be passed off as the exception.
    assert "Traceback (most recent call last)" not in rendered


def test_truncated_stacktrace_is_detected_and_says_so():
    """Tempo caps a span attribute at 2048 bytes, which drops the failing frame.

    A Python traceback prints frames outermost first, so the app frame that raised
    is the last one in the string. Through FastAPI's and Starlette's middleware the
    framework frames in front of it are already longer than 2048 bytes, so the cap
    always cuts off the interesting part.
    """
    framework = '  File "/app/.venv/lib/python3.12/site-packages/starlette/middleware/base.py", line 193, in __call__\n    response = await self.dispatch_func(request, call_next)\n'
    app_frame = ('  File "/app/app/main.py", line 60, in order_detail\n'
                 "    estimated_at = placed_at.replace(day=placed_at.day + 2)\n"
                 "ValueError: day is out of range for month\n")
    # A real traceback: framework frames, the telemetry middleware, then the app.
    # Only the first 2048 bytes survive.
    full = (
        "Traceback (most recent call last):\n"
        + framework * 7
        + '  File "/app/app/telemetry.py", line 158, in dispatch\n'
        "    response = await call_next(request)\n"
        + framework * 7
        + app_frame
    )
    truncated = full[:_TRUNCATED_AT]

    assert len(truncated) == _TRUNCATED_AT
    assert len(full) > _TRUNCATED_AT
    assert _is_truncated(truncated)
    assert not _is_truncated(full)
    assert not _is_truncated(TEMPO_RESPONSE["trace"]["resourceSpans"][0]["scopeSpans"][0][
        "spans"][0]["events"][0]["attributes"][2]["value"]["stringValue"])

    frames = _application_frames(truncated)
    assert any("telemetry.py" in frame for frame in frames)
    assert not any(frame.startswith("Traceback") for frame in frames)
    # The frame that actually raised was cut off along with the exception line.
    assert not any("main.py" in frame for frame in frames)


def test_prompt_names_the_concrete_failing_request():
    """The route template alone is not something the assistant can curl."""
    client, collector = _collector(_handler)
    evidence = asyncio.run(_collect(client, collector))
    prompt = AgentRunner(_config()).build_prompt(
        {"id": "20261004T150000-abc12345", "alertname": "5xx", "severity": "critical",
         "route": "/api/orders/{order_id}", "method": "GET", "starts_at": "2026-10-04T15:00:00Z"},
        evidence,
    )
    assert "/api/orders/express-1002" in prompt
    assert "curl -i http://127.0.0.1:8000/api/orders/express-1002" in prompt
    assert "express-1002" in prompt
    # Sections need a blank line before a heading to render as markdown.
    assert "\n\n## Traces (Tempo)" in prompt
    assert "\n\n## Failing log records (Loki)" in prompt


def _collect(client, collector):
    async def go():
        async with client:
            return await collector.collect(
                alert={}, service="order-tracker",
                route="/api/orders/{order_id}", method="GET",
                started_at=datetime.now(timezone.utc),
                app_url="http://127.0.0.1:8000",
            )
    return go()


def _config(tmp_path=None):
    import dataclasses

    from incident_response.config import load_config

    return dataclasses.replace(load_config(), repo_root=tmp_path or "/tmp")


def test_branch_name_is_a_usable_git_branch():
    name = _branch_name({"route": "/api/orders/{order_id}", "id": "20261004T150000-abc123"})
    assert name == "incident/api-orders-order_id-20261004T150000"
    assert "{" not in name and "}" not in name and " " not in name


def test_branch_names_differ_for_two_faults_on_the_same_route_and_day():
    """A date-only suffix put both incidents on one branch and mixed their fixes."""
    first = _branch_name({"route": "/api/orders/{order_id}", "id": "20261004T150000-aaa111"})
    second = _branch_name({"route": "/api/orders/{order_id}", "id": "20261004T162213-bbb222"})
    assert first != second
    assert first.startswith("incident/api-orders-order_id-")


def test_event_parsing_handles_both_opencode_event_shapes(tmp_path):
    events = tmp_path / "agent.jsonl"
    events.write_text(
        "\n".join([
            '{"type":"session.updated","properties":{"info":{"id":"ses_42"}}}',
            '{"type":"message.part.updated","properties":{"part":'
            '{"type":"text","text":"partial"}}}',
            '{"type":"message.updated","properties":{"info":{"role":"assistant",'
            '"parts":[{"type":"text","text":"Root cause: day overflow."}]}}}',
        ]),
        encoding="utf-8",
    )
    result = _parse_events(events)
    assert result.session_id == "ses_42"
    assert result.summary == "Root cause: day overflow."


def test_webhook_parsing_rules():
    payload = GrafanaWebhook.model_validate({
        "status": "firing",
        "alerts": [{
            "status": "firing",
            "labels": {"http_route": "/api/orders/{order_id}",
                       "http_request_method": "GET",
                       "service_name": "order-tracker"},
            "startsAt": "2026-10-04T15:00:00Z",
            "endsAt": "0001-01-01T00:00:00Z",
            "fingerprint": "abc123",
        }],
    })
    assert payload.is_firing
    assert payload.alerts[0].fingerprint_key == "abc123"
    # The open-ended sentinel must not become a timestamp.
    assert payload.alerts[0].endsAt.startswith("0001")
    assert payload.parse_time(payload.alerts[0].endsAt) is None
    assert payload.parse_time(payload.alerts[0].startsAt).year == 2026

    # No fingerprint from Grafana: fall back to a stable hash of the labels.
    other = GrafanaWebhook.model_validate({
        "alerts": [{"labels": {"b": "2", "a": "1"}}],
    })
    same = GrafanaWebhook.model_validate({
        "alerts": [{"labels": {"a": "1", "b": "2"}}],
    })
    assert other.alerts[0].fingerprint_key == same.alerts[0].fingerprint_key
    assert len(other.alerts[0].fingerprint_key) == 16