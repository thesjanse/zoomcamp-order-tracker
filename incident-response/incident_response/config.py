from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_ENV_PREFIX = "INCIDENT_RESPONSE_"


def _env(name: str, default: str) -> str:
    return os.getenv(_ENV_PREFIX + name, default)


def _flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(_ENV_PREFIX + name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(_ENV_PREFIX + name)
    return int(raw) if raw else default


@dataclass(frozen=True)
class Config:
    # Binds 0.0.0.0 by default: Grafana reaches this from inside a container, so
    # loopback would make the webhook undeliverable.
    host: str
    port: int
    loki_url: str
    tempo_url: str
    prometheus_url: str
    # Grafana does not copy metric labels onto the alert, so the rule in
    # observability/ produces no service label. This is the fallback used both for
    # querying and for what is recorded on the incident.
    service: str
    # Where the affected service is reachable from this host, handed to the
    # assistant so it can reproduce the failing request.
    app_url: str
    repo_root: Path
    data_dir: Path
    opencode_bin: str
    opencode_agent: str
    opencode_model: str | None
    # Seconds of log/trace/metric history to pull back from the alert's start.
    lookback_seconds: int
    # Ignore a repeat firing for the same fingerprint inside this window.
    cooldown_seconds: int
    # Hard stop on the agent subprocess.
    agent_timeout_seconds: int
    stall_timeout_seconds: int
    # Cap on how many traces to pull in full from Tempo.
    max_traces: int
    # Cap on how many log records to keep in the bundle.
    max_log_records: int
    # Capture evidence and write the bundle, but never spawn the agent.
    dry_run: bool

    @property
    def incidents_dir(self) -> Path:
        return self.data_dir / "incidents"

    @property
    def database_path(self) -> Path:
        return self.data_dir / "incidents.db"


def load_config() -> Config:
    package_dir = Path(__file__).resolve().parent
    # incident_response/ -> incident-response/ -> repo root
    default_repo_root = package_dir.parent.parent

    return Config(
        host=_env("HOST", "0.0.0.0"),
        port=_int("PORT", 8001),
        loki_url=_env("LOKI_URL", "http://127.0.0.1:3100").rstrip("/"),
        tempo_url=_env("TEMPO_URL", "http://127.0.0.1:3200").rstrip("/"),
        prometheus_url=_env("PROMETHEUS_URL", "http://127.0.0.1:9090").rstrip("/"),
        service=_env("SERVICE", "order-tracker"),
        app_url=_env("APP_URL", "http://127.0.0.1:8000"),
        repo_root=Path(_env("REPO_ROOT", str(default_repo_root))).resolve(),
        data_dir=Path(_env("DATA_DIR", str(package_dir.parent / "data"))).resolve(),
        opencode_bin=_env("OPENCODE_BIN", "opencode"),
        opencode_agent=_env("OPENCODE_AGENT", "incident-responder"),
        opencode_model=os.getenv(_ENV_PREFIX + "OPENCODE_MODEL") or None,
        lookback_seconds=_int("LOOKBACK_SECONDS", 900),
        cooldown_seconds=_int("COOLDOWN_SECONDS", 900),
        agent_timeout_seconds=_int("AGENT_TIMEOUT_SECONDS", 1800),
        stall_timeout_seconds=_int("STALL_TIMEOUT_SECONDS", 300),
        max_traces=_int("MAX_TRACES", 3),
        max_log_records=_int("MAX_LOG_RECORDS", 200),
        dry_run=_flag("DRY_RUN", False),
    )