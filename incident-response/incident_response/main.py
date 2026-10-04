from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, HTTPException, Response

from .agent import AgentRunner
from .config import Config, load_config
from .evidence import Evidence, EvidenceCollector
from .models import GrafanaAlert, GrafanaWebhook
from .store import Store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
)
log = logging.getLogger("incident_response")


def _incident_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


def create_app(config: Config | None = None) -> FastAPI:
    config = config or load_config()
    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.incidents_dir.mkdir(parents=True, exist_ok=True)

    store = Store(config.database_path)
    agent = AgentRunner(config)
    # One investigation at a time. The alert rule stays firing for 5 minutes and
    # Grafana re-notifies on every group_interval, so a queue with a single worker
    # is also what keeps a flapping alert from piling up agent runs.
    queue: asyncio.Queue[str] = asyncio.Queue()
    runner: dict = {"task": None, "client": None}

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # One shared client so connection pooling covers all three backends.
        runner["client"] = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=60.0))
        runner["task"] = asyncio.create_task(_worker())
        log.info(
            "incident-response ready: data=%s repo=%s dry_run=%s",
            config.data_dir, config.repo_root, config.dry_run,
        )
        try:
            yield
        finally:
            if runner["task"]:
                runner["task"].cancel()
                try:
                    await runner["task"]
                except asyncio.CancelledError:
                    pass
            await runner["client"].aclose()

    app = FastAPI(
        title="Incident Response",
        version="0.1.0",
        description="Receives Grafana alerts, saves the evidence, and runs a coding assistant.",
        lifespan=lifespan,
    )
    app.state.config = config
    app.state.store = store
    app.state.queue = queue

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "queued": queue.qsize(), "dry_run": config.dry_run}

    @app.post("/alerts", status_code=202)
    async def receive_alert(payload: GrafanaWebhook):
        alerts = payload.alerts or []
        if not alerts:
            raise HTTPException(400, "no alerts in payload")

        accepted: list[dict] = []
        for alert in alerts:
            accepted.append(await _handle_alert(payload, alert))
        return {"received": len(alerts), "incidents": accepted}

    @app.get("/incidents")
    async def list_incidents(limit: int = 50):
        return store.list(limit=min(max(limit, 1), 500))

    @app.get("/incidents/{incident_id}")
    async def get_incident(incident_id: str):
        incident = store.get(incident_id)
        if incident is None:
            raise HTTPException(404, "incident not found")
        return incident

    @app.get("/incidents/{incident_id}/evidence", response_class=Response)
    async def get_evidence(incident_id: str):
        incident = store.get(incident_id)
        if incident is None:
            raise HTTPException(404, "incident not found")
        return Response(
            content=json.dumps(incident.get("evidence"), indent=2, default=str),
            media_type="application/json",
        )

    @app.get("/incidents/{incident_id}/agent", response_class=Response)
    async def get_agent_output(incident_id: str, format: str = "jsonl"):
        incident = store.get(incident_id)
        if incident is None:
            raise HTTPException(404, "incident not found")
        path = _evidence_path(config, incident_id) / (
            "agent.jsonl" if format == "jsonl" else "prompt.md"
        )
        if not path.exists():
            raise HTTPException(404, f"{path.name} not available for this incident")
        return Response(content=path.read_text(encoding="utf-8"), media_type="text/plain")

    # ------------------------------------------------------------------

    async def _handle_alert(payload: GrafanaWebhook, alert: GrafanaAlert) -> dict:
        fingerprint = alert.fingerprint_key

        if not payload.is_firing or alert.status == "resolved":
            # Not cooldown-bounded: the resolve for an alert that fired minutes ago
            # still has to find it, otherwise the incident never closes.
            existing = store.latest_for_fingerprint(fingerprint)
            if existing and existing["status"] != "resolved":
                store.update(existing["id"], status="resolved",
                             resolved_at=datetime.now(timezone.utc).isoformat())
                log.info("incident %s resolved", existing["id"])
            return {"fingerprint": fingerprint, "action": "resolved",
                    "incident_id": existing["id"] if existing else None}

        recent = store.recent_for_fingerprint(fingerprint, config.cooldown_seconds)
        if recent and recent["agent_status"] not in ("failed",):
            # Grafana re-notifies on every group_interval while the rule stays
            # firing, and this rule stays firing for 5 minutes after the last 5xx.
            log.info(
                "incident %s already covers fingerprint %s (agent_status=%s), skipping",
                recent["id"], fingerprint, recent["agent_status"],
            )
            return {"fingerprint": fingerprint, "action": "skipped",
                    "incident_id": recent["id"]}

        incident_id = _incident_id()
        record = {
            "id": incident_id,
            "fingerprint": fingerprint,
            "status": "firing",
            "agent_status": "queued",
            "alertname": alert.alertname,
            "severity": alert.labels.get("severity"),
            "service": alert.service or config.service,
            "route": alert.route,
            "method": alert.method,
            "summary": alert.annotations.get("summary"),
            "description": alert.annotations.get("description"),
            "labels_json": json.dumps(alert.labels),
            "annotations_json": json.dumps(alert.annotations),
            "dashboard_link": alert.annotations.get("dashboard_link"),
            "starts_at": alert.startsAt or payload.started_at.isoformat(),
            "ends_at": alert.endsAt,
            "received_at": datetime.now(timezone.utc).isoformat(),
            "evidence_dir": str(_evidence_path(config, incident_id)),
        }
        store.create(record)
        queue.put_nowait(incident_id)
        log.info(
            "queued incident %s: %s %s (%s)", incident_id,
            alert.method, alert.route, alert.alertname,
        )
        return {"fingerprint": fingerprint, "action": "queued", "incident_id": incident_id}

    async def _worker():
        while True:
            incident_id = await queue.get()
            try:
                await _investigate(incident_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the worker must outlive one bad incident
                log.exception("incident %s crashed", incident_id)
                store.update(incident_id, status="failed", agent_status="failed",
                             error=f"{type(exc).__name__}: {exc}")
            finally:
                queue.task_done()

    async def _investigate(incident_id: str):
        incident = store.get(incident_id)
        if incident is None:
            return

        store.update(incident_id, agent_status="investigating")
        collector = EvidenceCollector(
            runner["client"],
            loki_url=config.loki_url,
            tempo_url=config.tempo_url,
            prometheus_url=config.prometheus_url,
            lookback_seconds=config.lookback_seconds,
            max_traces=config.max_traces,
            max_log_records=config.max_log_records,
        )
        try:
            evidence = await collector.collect(
                alert={"summary": incident.get("summary"),
                       "description": incident.get("description"),
                       "annotations": incident.get("annotations"),
                       "labels": incident.get("labels")},
                service=incident.get("service") or config.service,
                route=incident.get("route") or "",
                method=incident.get("method") or "",
                started_at=GrafanaWebhook.parse_time(incident.get("starts_at"))
                or datetime.now(timezone.utc),
                app_url=config.app_url,
            )
        except Exception as exc:  # noqa: BLE001
            store.update(incident_id, agent_status="failed",
                         error=f"evidence collection failed: {type(exc).__name__}: {exc}")
            return

        evidence_dir = _evidence_path(config, incident_id)
        evidence_dir.mkdir(parents=True, exist_ok=True)
        _write_bundle(evidence_dir, evidence, agent.build_prompt(incident, evidence))

        store.update(
            incident_id,
            evidence_json=json.dumps(evidence.to_dict(), default=str),
            evidence_dir=str(evidence_dir),
            evidence_error="; ".join(evidence.errors) or None,
        )

        result = await agent.run(incident=incident, evidence=evidence,
                                 evidence_dir=evidence_dir)
        store.update(
            incident_id,
            agent_status=result.status,
            agent_exit_code=result.exit_code,
            agent_session_id=result.session_id,
            agent_summary=result.summary,
            commit_sha=result.commit_sha,
            git_branch=result.git_branch,
            error=result.error,
        )
        log.info(
            "incident %s finished: agent_status=%s commit=%s",
            incident_id, result.status, result.commit_sha,
        )

    return app


def _evidence_path(config: Config, incident_id: str):
    return config.incidents_dir / incident_id


def _write_bundle(evidence_dir, evidence: Evidence, prompt: str):
    """Write the evidence as both JSON and flat files, so it reads without parsing.

    Also writes the exact prompt handed to the coding assistant, so an incident can
    be reviewed after the fact without reconstructing what it was told.
    """
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "evidence.json").write_text(
        json.dumps(evidence.to_dict(), indent=2, default=str), encoding="utf-8"
    )
    (evidence_dir / "prompt.md").write_text(prompt, encoding="utf-8")

    log_lines = []
    for record in evidence.logs.get("records", []):
        log_lines.append(
            f"{record['timestamp_iso']}\t{record['severity_text']}\t"
            f"order_id={record['order_id']}\terror_type={record['error_type']}\t"
            f"status={record['http_response_status_code']}\t"
            f"trace_id={record['trace_id']}\t{record['message']}"
        )
    (evidence_dir / "logs.log").write_text("\n".join(log_lines) + "\n", encoding="utf-8")

    trace_lines = []
    for trace in evidence.traces.get("traces", []):
        root_cause = trace.get("root_cause") or {}
        trace_lines.append(
            f"trace {trace['trace_id']}\n"
            f"  root cause: {root_cause.get('type')}: {root_cause.get('message')}\n"
            f"  stacktrace:\n{_indent(root_cause.get('stacktrace') or '(none)')}\n"
        )
    (evidence_dir / "traces.log").write_text("\n".join(trace_lines) + "\n", encoding="utf-8")

    metric_lines = [
        f"{name} = {_scalar_text(value)}" for name, value in evidence.metrics.get("results", {}).items()
    ]
    (evidence_dir / "metrics.log").write_text("\n".join(metric_lines) + "\n", encoding="utf-8")


def _indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


def _scalar_text(value) -> str:
    if isinstance(value, dict) and "series" in value:
        return ", ".join(
            f"{labels.get('http_response_status_code', 'total')}={item['value']:g}"
            for item in value["series"]
            for labels in [item["labels"]]
        ) or "no series"
    return str(value)


app = create_app()


def main():
    import uvicorn

    config = load_config()
    uvicorn.run("incident_response.main:app", host=config.host, port=config.port)


if __name__ == "__main__":
    main()