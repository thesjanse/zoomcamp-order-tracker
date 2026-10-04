from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

log = logging.getLogger("incident_response.evidence")


@dataclass
class Evidence:
    """Everything needed to understand the fault, and nothing that needs Grafana."""

    alert: dict
    endpoint: dict
    # The base URL the affected service is reachable on from this host, so the
    # assistant can reproduce the failure itself.
    app_url: str = ""
    logs: dict = field(default_factory=dict)
    traces: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "alert": self.alert,
            "endpoint": self.endpoint,
            "app_url": self.app_url,
            "logs": self.logs,
            "traces": self.traces,
            "metrics": self.metrics,
            "errors": self.errors,
        }


class EvidenceCollector:
    def __init__(self, client: httpx.AsyncClient, *, loki_url: str, tempo_url: str,
                 prometheus_url: str, lookback_seconds: int, max_traces: int,
                 max_log_records: int):
        self.client = client
        self.loki_url = loki_url.rstrip("/")
        self.tempo_url = tempo_url.rstrip("/")
        self.prometheus_url = prometheus_url.rstrip("/")
        self.lookback_seconds = lookback_seconds
        self.max_traces = max_traces
        self.max_log_records = max_log_records

    async def collect(self, *, alert: dict, service: str, route: str, method: str,
                      started_at: datetime, app_url: str = "") -> Evidence:
        # Pull a little history before the alert's start: the rule aggregates over a
        # 5m window and fires up to 30s late, so the first failing request is
        # normally already in the past.
        start = started_at.timestamp() - self.lookback_seconds
        end = datetime.now(timezone.utc).timestamp()

        evidence = Evidence(
            alert=alert,
            endpoint={"service": service, "route": route, "method": method},
            app_url=app_url.rstrip("/"),
        )
        logs = await self._collect_logs(start, end, service, route, method, evidence)
        await self._collect_traces(logs, evidence)
        await self._collect_metrics(service, route, method, evidence)
        return evidence

    # Loki ---------------------------------------------------------------

    async def _collect_logs(self, start: float, end: float, service: str, route: str,
                            method: str, evidence: Evidence) -> list[dict]:
        """Failing log records for the endpoint, plus the trace ids they point at.

        The OpenTelemetry attributes arrive as Loki *structured metadata*, not
        indexed labels: /loki/api/v1/labels only reports service_name and
        service_instance_id. So they are selected with a pipeline filter rather
        than inside the stream selector.
        """
        selector = f'{{service_name="{service}"}}'
        filters = [
            f'http_route="{route}"',
            f'order_lookup_outcome="error"',
            f'detected_level="error"',
        ]
        if method:
            filters.insert(1, f'http_request_method="{method}"')
        query = selector + " | " + " | ".join(filters)

        records: list[dict] = []
        try:
            response = await self.client.get(
                f"{self.loki_url}/loki/api/v1/query_range",
                params={
                    "query": query,
                    "start": int(start * 1e9),
                    "end": int(end * 1e9),
                    "limit": self.max_log_records,
                    "direction": "backward",
                },
            )
            response.raise_for_status()
            for stream in response.json().get("data", {}).get("result", []):
                labels = stream.get("stream", {})
                for timestamp_ns, line in stream.get("values", []):
                    records.append({
                        "timestamp": int(timestamp_ns) / 1e9,
                        "timestamp_iso": datetime.fromtimestamp(
                            int(timestamp_ns) / 1e9, timezone.utc
                        ).isoformat(),
                        "message": line,
                        "trace_id": labels.get("trace_id", ""),
                        "span_id": labels.get("span_id", ""),
                        "error_type": labels.get("error_type", ""),
                        "order_id": labels.get("order_id", ""),
                        "severity_text": labels.get("severity_text", ""),
                        "http_response_status_code": labels.get("http_response_status_code", ""),
                    })
        except Exception as exc:  # noqa: BLE001 - one dead backend must not sink the incident
            evidence.errors.append(f"loki: {type(exc).__name__}: {exc}")
            log.warning("loki query failed: %s", exc)
            return []

        records.sort(key=lambda r: r["timestamp"], reverse=True)
        evidence.logs = {
            "query": query,
            "window": {"start": start, "end": end},
            "count": len(records),
            "records": records,
        }
        return records

    # Tempo --------------------------------------------------------------

    async def _collect_traces(self, logs: list[dict], evidence: Evidence) -> None:
        """Fetch the full trace for the most recent failing requests, by trace id.

        Tempo's TraceQL search endpoint is not used: with local block storage the
        search path only covers recent data and comes up empty for a minute after
        startup, whereas /api/v2/traces/<id> resolves immediately. The trace ids
        come from the log records, which is the correlation the app already sets up.
        """
        trace_ids: list[str] = []
        for record in logs:
            trace_id = record.get("trace_id")
            if trace_id and trace_id not in trace_ids:
                trace_ids.append(trace_id)
            if len(trace_ids) >= self.max_traces:
                break

        traces: list[dict] = []
        for trace_id in trace_ids:
            try:
                response = await self.client.get(
                    f"{self.tempo_url}/api/v2/traces/{trace_id}",
                    headers={"Accept": "application/json"},
                )
                if response.status_code == 404:
                    evidence.errors.append(f"tempo: trace {trace_id} not found")
                    continue
                response.raise_for_status()
                traces.append(_summarise_trace(trace_id, response.json()))
            except Exception as exc:  # noqa: BLE001
                evidence.errors.append(f"tempo {trace_id}: {type(exc).__name__}: {exc}")
                log.warning("tempo trace %s failed: %s", trace_id, exc)

        evidence.traces = {"trace_ids": trace_ids, "count": len(traces), "traces": traces}

    # Prometheus ---------------------------------------------------------

    async def _collect_metrics(self, service: str, route: str, method: str,
                               evidence: Evidence) -> None:
        # Scoped to the alerting endpoint: the rule groups by http_route, so a
        # service-wide 5xx total would be noise next to the endpoint's own count.
        def selector(extra: str = "") -> str:
            labels = [f'service_name="{service}"']
            if method:
                labels.append(f'http_request_method="{method}"')
            if route:
                labels.append(f'http_route="{route}"')
            if extra:
                labels.append(extra)
            return "order_lookup_requests_total{%s}" % ",".join(labels)

        queries = {
            "server_errors_by_status": 'sum by (http_response_status_code) (%s)'
                                      % selector('http_response_status_code=~"5.."'),
            "server_errors_increase_5m": 'sum(increase(%s[5m]))' % selector('http_response_status_code=~"5.."'),
            "server_errors_total": "sum(%s)" % selector('http_response_status_code=~"5.."'),
            "lookups_total": "sum(%s)" % selector(),
            "errors_increase_5m": 'sum(increase(%s[5m]))' % selector('http_response_status_code=~"4.."'),
        }

        results: dict = {}
        for name, promql in queries.items():
            try:
                response = await self.client.get(
                    f"{self.prometheus_url}/api/v1/query", params={"query": promql}
                )
                response.raise_for_status()
                results[name] = _prometheus_vector(response.json(), promql)
            except Exception as exc:  # noqa: BLE001
                evidence.errors.append(f"prometheus {name}: {type(exc).__name__}: {exc}")
                log.warning("prometheus query %s failed: %s", name, exc)

        results["affected_endpoint"] = {"http_route": route, "http_request_method": method}
        evidence.metrics = {"queries": queries, "results": results}


def _prometheus_vector(payload: dict, promql: str) -> dict:
    data = payload.get("data", {})
    series = [
        {
            "labels": {k: v for k, v in item.get("metric", {}).items()},
            "value": float(item["value"][1]),
        }
        for item in data.get("result", [])
    ]
    return {"promql": promql, "result_type": data.get("resultType"), "series": series}


def _attrs(attributes: list[dict]) -> dict:
    """Flatten OTLP key/value pairs into plain Python values."""
    flat: dict = {}
    for attribute in attributes or []:
        value = attribute.get("value", {})
        flat[attribute["key"]] = next(iter(value.values()), None)
    return flat


def _summarise_trace(trace_id: str, payload: dict) -> dict:
    """Pull the interesting parts out of a Tempo trace.

    The exception event is the point of this: it carries the Python stacktrace,
    which names the file and line that raised.
    """
    resource_spans = payload.get("trace", {}).get("resourceSpans", [])
    spans: list[dict] = []
    exceptions: list[dict] = []

    for resource_span in resource_spans:
        resource = _attrs(resource_span.get("resource", {}).get("attributes", []))
        for scope in resource_span.get("scopeSpans", []):
            for span in scope.get("spans", []):
                start = int(span.get("startTimeUnixNano", 0))
                end = int(span.get("endTimeUnixNano", 0))
                attributes = _attrs(span.get("attributes", []))
                spans.append({
                    "name": span.get("name"),
                    "kind": span.get("kind"),
                    "duration_ms": round((end - start) / 1e6, 2) if end and start else None,
                    "status": (span.get("status") or {}).get("code"),
                    "service_name": resource.get("service.name"),
                    "http_route": attributes.get("http.route"),
                    "http_target": attributes.get("http.target"),
                    "http_method": attributes.get("http.method"),
                    "http_status_code": attributes.get("http.status_code"),
                })
                for event in span.get("events", []):
                    event_attributes = _attrs(event.get("attributes", []))
                    if "exception.type" not in event_attributes:
                        continue
                    exceptions.append({
                        "span_name": span.get("name"),
                        "type": event_attributes.get("exception.type"),
                        "message": event_attributes.get("exception.message"),
                        "stacktrace": event_attributes.get("exception.stacktrace"),
                    })

    return {
        "trace_id": trace_id,
        "spans": spans,
        "exceptions": exceptions,
        "root_cause": exceptions[0] if exceptions else None,
    }