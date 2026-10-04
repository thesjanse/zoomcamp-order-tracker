import logging
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from app import main
from app import telemetry


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "orders.db")
    with TestClient(main.app) as test_client:
        yield test_client


def test_health_and_seeded_orders(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    orders = client.get("/api/orders").json()
    assert len(orders) == 3
    assert {order["priority"] for order in orders} == {"standard", "express"}


def test_create_and_update_order(client):
    response = client.post(
        "/api/orders",
        json={"customer": "Taylor", "item": "Mug", "priority": "standard"},
    )
    assert response.status_code == 201
    order_id = response.json()["id"]
    assert client.get(f"/api/orders/{order_id}").json()["status"] == "received"
    updated = client.patch(f"/api/orders/{order_id}", json={"status": "shipped"})
    assert updated.status_code == 200
    assert updated.json()["status"] == "shipped"


def test_missing_order(client):
    assert client.get("/api/orders/missing").status_code == 404


def test_seeded_express_order_lookup_returns_estimate(client):
    response = client.get("/api/orders/express-1002")
    assert response.status_code == 200
    order = response.json()
    assert order["priority"] == "express"
    placed_on = datetime.fromisoformat(order["created_at"]).date()
    assert order["estimated_delivery"] == (placed_on + timedelta(days=2)).isoformat()


@pytest.mark.parametrize(
    ("created_at", "expected_estimate"),
    [
        ("2026-01-31T12:00:00+00:00", "2026-02-02"),
        ("2026-09-30T23:59:59+00:00", "2026-10-02"),
        ("2026-02-28T08:30:00+00:00", "2026-03-02"),
        ("2024-02-29T00:00:00+00:00", "2024-03-02"),
        ("2026-04-17T09:00:00+00:00", "2026-04-19"),
    ],
)
def test_express_estimate_is_two_days_after_placement(created_at, expected_estimate):
    row = {
        "id": "express-regression",
        "customer": "Sam",
        "item": "Headphones",
        "priority": "express",
        "status": "preparing",
        "created_at": created_at,
    }
    assert main.order_detail(row)["estimated_delivery"] == expected_estimate


def test_order_logger_records_successes_at_info_level():
    assert telemetry.get_order_logger().isEnabledFor(logging.INFO)


@pytest.fixture
def lookup_points(client, monkeypatch):
    reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[reader])
    meter = meter_provider.get_meter("tests")
    monkeypatch.setattr(
        telemetry,
        "get_lookup_instruments",
        lambda: telemetry.LookupInstruments(
            requests=meter.create_counter("order.lookup.requests", unit="{lookup}"),
            duration=meter.create_histogram("order.lookup.duration", unit="s"),
        ),
    )

    def collect():
        collected = {}
        data = reader.get_metrics_data()
        for resource_metrics in data.resource_metrics:
            for scope_metrics in resource_metrics.scope_metrics:
                for metric in scope_metrics.metrics:
                    collected[metric.name] = [
                        (dict(point.attributes), point) for point in metric.data.data_points
                    ]
        return collected

    yield collect
    meter_provider.shutdown()


def test_lookup_metrics_carry_route_and_status(client, lookup_points):
    assert client.get("/api/orders/standard-1001").status_code == 200
    assert client.get("/api/orders/missing").status_code == 404
    created = client.post(
        "/api/orders",
        json={"customer": "Jordan", "item": "Lamp", "priority": "standard"},
    )
    assert created.status_code == 201

    collected = lookup_points()

    counted = {
        (attributes["http.route"], attributes["http.response.status_code"]): point.value
        for attributes, point in collected["order.lookup.requests"]
    }
    assert counted == {("/api/orders/{order_id}", 200): 1, ("/api/orders/{order_id}", 404): 1}
    assert {a["http.request.method"] for a, _ in collected["order.lookup.requests"]} == {"GET"}

    timed = collected["order.lookup.duration"]
    assert {(a["http.route"], a["http.response.status_code"]) for a, _ in timed} == {
        ("/api/orders/{order_id}", 200),
        ("/api/orders/{order_id}", 404),
    }
    assert all(point.count == 1 and point.sum >= 0 for _, point in timed)
