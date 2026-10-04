from __future__ import annotations

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from incident_response.agent import AgentResult, AgentRunner
from incident_response.evidence import Evidence, EvidenceCollector
from incident_response.main import create_app


@pytest.fixture
def canned_evidence(monkeypatch: pytest.MonkeyPatch):
    """Replace network collection and the agent subprocess with fixed results.

    Two separate stubs: the collector so no HTTP leaves the test, and the runner so
    no git branch is created and no coding assistant is started.
    """
    evidence = Evidence(
        alert={"summary": "5xx responses on GET /api/orders/{order_id}"},
        endpoint={"service": "order-tracker", "route": "/api/orders/{order_id}",
                  "method": "GET"},
        logs={"query": "logql", "window": {"start": 0, "end": 1}, "count": 1,
              "records": [{"timestamp": 1.0, "timestamp_iso": "2026-10-04T15:00:00+00:00",
                           "message": "order lookup completed", "trace_id": "abc",
                           "span_id": "def", "error_type": "ValueError",
                           "order_id": "express-1002", "severity_text": "ERROR",
                           "http_response_status_code": "500"}]},
        traces={"trace_ids": ["abc"], "count": 1, "traces": [
            {"trace_id": "abc", "spans": [], "exceptions": [], "root_cause": None}]},
        metrics={"queries": {"q": "up"}, "results": {"server_errors_total": {
            "promql": "q", "result_type": "vector",
            "series": [{"labels": {}, "value": 3.0}]}}},
    )

    async def fake_collect(self, **kwargs):
        return evidence

    async def fake_run(self, *, incident, evidence, evidence_dir):
        (evidence_dir / "agent.jsonl").write_text(
            json.dumps({"type": "message.updated", "properties": {
                "info": {"role": "assistant", "parts": [
                    {"type": "text", "text": "Root cause: day overflow."}]}}}) + "\n",
            encoding="utf-8",
        )
        return AgentResult(status="done", exit_code=0, session_id="ses_1",
                           summary="Root cause: day overflow.",
                           commit_sha="deadbeef", git_branch="incident/api-orders-abc")

    monkeypatch.setattr(EvidenceCollector, "collect", fake_collect)
    monkeypatch.setattr(AgentRunner, "run", fake_run)


@pytest.fixture
def client(config, canned_evidence) -> TestClient:
    with TestClient(create_app(config)) as test_client:
        yield test_client


def _wait_for_incident(client: TestClient, incident_id: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/incidents/{incident_id}")
        assert response.status_code == 200
        incident = response.json()
        if incident["agent_status"] in {"done", "failed", "skipped"}:
            return incident
        time.sleep(0.02)
    raise AssertionError(f"incident {incident_id} never reached a terminal state")


def test_healthz(client: TestClient):
    body = client.get("/healthz").json()
    assert body["status"] == "ok"
    assert body["dry_run"] is True


def test_alert_creates_incident_and_saves_evidence(client: TestClient, firing_payload: dict):
    response = client.post("/alerts", json=firing_payload)
    assert response.status_code == 202

    queued = response.json()["incidents"][0]
    assert queued["action"] == "queued"
    incident_id = queued["incident_id"]

    incident = _wait_for_incident(client, incident_id)
    assert incident["status"] == "firing"
    assert incident["route"] == "/api/orders/{order_id}"
    assert incident["method"] == "GET"
    assert incident["service"] == "order-tracker"
    assert incident["severity"] == "critical"
    assert incident["alertname"] == "5xx responses on an order lookup endpoint"
    assert incident["summary"] == "5xx responses on GET /api/orders/{order_id}"

    # The evidence is persisted and readable without the service running.
    evidence = client.get(f"/incidents/{incident_id}/evidence").json()
    assert evidence["endpoint"]["route"] == "/api/orders/{order_id}"
    assert evidence["logs"]["records"][0]["trace_id"] == "abc"
    assert evidence["metrics"]["results"]["server_errors_total"]["series"][0]["value"] == 3.0
    # The incident record carries the parsed bundle too.
    assert incident["evidence"]["traces"]["trace_ids"] == ["abc"]


def test_evidence_bundle_is_written_to_disk(client: TestClient, firing_payload: dict):
    incident_id = client.post("/alerts", json=firing_payload).json()["incidents"][0]["incident_id"]
    incident = _wait_for_incident(client, incident_id)

    bundle = client.get(f"/incidents/{incident_id}/agent?format=prompt").text
    assert "order-tracker" in bundle

    from pathlib import Path

    directory = Path(incident["evidence_dir"])
    assert (directory / "evidence.json").exists()
    assert (directory / "logs.log").exists()
    assert (directory / "traces.log").exists()
    assert (directory / "metrics.log").exists()
    # Order id, error type and trace id all survive into the flat log file.
    flat = (directory / "logs.log").read_text()
    assert "express-1002" in flat and "ValueError" in flat and "trace_id=abc" in flat


def test_agent_result_is_recorded(client: TestClient, firing_payload: dict):
    incident_id = client.post("/alerts", json=firing_payload).json()["incidents"][0]["incident_id"]
    incident = _wait_for_incident(client, incident_id)

    assert incident["agent_status"] == "done"
    assert incident["agent_exit_code"] == 0
    assert incident["agent_session_id"] == "ses_1"
    assert incident["commit_sha"] == "deadbeef"
    assert incident["git_branch"] == "incident/api-orders-abc"
    assert "day overflow" in incident["agent_summary"]

    events = client.get(f"/incidents/{incident_id}/agent").text
    assert "message.updated" in events


def test_repeat_firing_is_deduped(client: TestClient, firing_payload: dict):
    first = client.post("/alerts", json=firing_payload).json()["incidents"][0]
    assert first["action"] == "queued"
    _wait_for_incident(client, first["incident_id"])

    # Grafana re-notifies on every group_interval while the rule stays firing.
    second = client.post("/alerts", json=firing_payload).json()["incidents"][0]
    assert second["action"] == "skipped"
    assert second["incident_id"] == first["incident_id"]

    assert len(client.get("/incidents").json()) == 1


def test_resolved_marks_the_incident_without_a_new_one(client: TestClient, firing_payload: dict):
    first = client.post("/alerts", json=firing_payload).json()["incidents"][0]
    _wait_for_incident(client, first["incident_id"])

    resolved = {**firing_payload, "status": "resolved",
                "alerts": [{**firing_payload["alerts"][0], "status": "resolved"}]}
    response = client.post("/alerts", json=resolved)
    assert response.status_code == 202
    assert response.json()["incidents"][0]["action"] == "resolved"

    incidents = client.get("/incidents").json()
    assert len(incidents) == 1
    assert incidents[0]["status"] == "resolved"
    assert incidents[0]["resolved_at"] is not None


def test_resolve_finds_an_incident_older_than_the_cooldown(
    client: TestClient, firing_payload: dict, monkeypatch: pytest.MonkeyPatch
):
    """A resolve must not be dropped by the investigation cooldown.

    The rule stays firing for five minutes after the last 5xx and group_interval
    adds more on top, so the resolve for a notification can land long after it. The
    cooldown bounds duplicate investigations; it must not bound resolutions.
    """
    import dataclasses

    incident_id = client.post("/alerts", json=firing_payload).json()["incidents"][0]["incident_id"]
    _wait_for_incident(client, incident_id)

    store = client.app.state.store
    # Pretend the firing happened far longer ago than the cooldown window.
    monkeypatch.setattr(
        store, "recent_for_fingerprint",
        lambda *a, **k: None,
    )
    long_ago = "2020-01-01T00:00:00+00:00"
    store.update(incident_id, created_at=long_ago, received_at=long_ago)

    resolved = {**firing_payload, "status": "resolved",
                "alerts": [{**firing_payload["alerts"][0], "status": "resolved"}]}
    response = client.post("/alerts", json=resolved)
    assert response.json()["incidents"][0] == {
        "fingerprint": firing_payload["alerts"][0]["fingerprint"],
        "action": "resolved",
        "incident_id": incident_id,
    }
    assert client.get(f"/incidents/{incident_id}").json()["status"] == "resolved"


def test_resolve_without_a_matching_incident_is_a_noop(client: TestClient, firing_payload: dict):
    resolved = {**firing_payload, "status": "resolved",
                "alerts": [{**firing_payload["alerts"][0], "status": "resolved",
                            "fingerprint": "never-seen"}]}
    body = client.post("/alerts", json=resolved).json()["incidents"][0]
    assert body["action"] == "resolved"
    assert body["incident_id"] is None
    assert client.get("/incidents").json() == []


def test_empty_alert_list_is_rejected(client: TestClient):
    assert client.post("/alerts", json={"status": "firing", "alerts": []}).status_code == 400


def test_unknown_incident_is_404(client: TestClient):
    assert client.get("/incidents/nope").status_code == 404
    assert client.get("/incidents/nope/evidence").status_code == 404


def test_collector_survives_a_dead_backend(config):
    """A Loki or Tempo outage must not stop an incident from being recorded."""
    import httpx

    async def go():
        transport = httpx.MockTransport(lambda request: httpx.Response(503, text="down"))
        async with httpx.AsyncClient(transport=transport) as http:
            collector = EvidenceCollector(
                http, loki_url="http://loki:3100", tempo_url="http://tempo:3200",
                prometheus_url="http://prom:9090", lookback_seconds=60,
                max_traces=1, max_log_records=10,
            )
            return await collector.collect(
                alert={}, service="order-tracker", route="/api/orders/{order_id}",
                method="GET", started_at=_now(),
            )

    evidence = asyncio.run(go())
    assert evidence.logs == {} or evidence.logs.get("count") is None
    assert evidence.traces.get("traces") == []
    assert len(evidence.errors) == 6  # loki + 1 tempo attempt + prometheus x 4


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)