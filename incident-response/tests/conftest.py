from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from incident_response.config import Config, load_config


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    """A config pointed at a temp data dir and unreachable backends."""
    base = load_config()
    return dataclasses.replace(
        base,
        repo_root=tmp_path / "repo",
        data_dir=tmp_path / "data",
        cooldown_seconds=900,
        # Never let a test reach a real backend or a real coding assistant.
        dry_run=True,
        loki_url="http://loki.invalid:3100",
        tempo_url="http://tempo.invalid:3200",
        prometheus_url="http://prometheus.invalid:9090",
    )


@pytest.fixture
def firing_payload() -> dict:
    """A webhook shaped like the one the order-tracker 5xx rule produces."""
    return {
        "receiver": "incident-response",
        "status": "firing",
        "orgId": 1,
        "title": "[FIRING:1] 5xx responses on an order lookup endpoint Order Tracker order-tracker-5xx",
        "state": "alerting",
        "externalURL": "http://grafana:3000/",
        "groupLabels": {"alertname": "5xx responses on an order lookup endpoint"},
        "commonLabels": {},
        "commonAnnotations": {},
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "5xx responses on an order lookup endpoint",
                    "grafana_folder": "Order Tracker",
                    "http_request_method": "GET",
                    "http_route": "/api/orders/{order_id}",
                    "severity": "critical",
                    "service_name": "order-tracker",
                },
                "annotations": {
                    "summary": "5xx responses on GET /api/orders/{order_id}",
                    "description": "3 5xx response(s) so far on GET /api/orders/{order_id}",
                    "dashboard_link": "http://127.0.0.1:3000/d/order-tracker-requests?viewPanel=6",
                },
                "values": {"B": 3.0},
                "startsAt": "2026-10-04T15:00:00Z",
                "endsAt": "0001-01-01T00:00:00Z",
                "generatorURL": "http://grafana:3000/alerting/grafana/order-tracker-5xx/view",
                "fingerprint": "a1b2c3d4e5f6",
            }
        ],
    }