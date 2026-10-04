from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id                TEXT PRIMARY KEY,
    fingerprint       TEXT NOT NULL,
    status            TEXT NOT NULL,
    agent_status      TEXT,
    alertname         TEXT,
    severity          TEXT,
    service           TEXT,
    route             TEXT,
    method            TEXT,
    summary           TEXT,
    description       TEXT,
    labels_json       TEXT,
    annotations_json  TEXT,
    dashboard_link    TEXT,
    starts_at         TEXT,
    ends_at           TEXT,
    received_at       TEXT,
    resolved_at       TEXT,
    evidence_dir      TEXT,
    evidence_json     TEXT,
    evidence_error    TEXT,
    git_branch        TEXT,
    commit_sha        TEXT,
    agent_session_id  TEXT,
    agent_exit_code   INTEGER,
    agent_summary     TEXT,
    error             TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS incidents_fingerprint_idx ON incidents (fingerprint);
"""

TERMINAL_AGENT_STATUSES = ("done", "failed", "skipped")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


_COLUMNS = [
    "id", "fingerprint", "status", "agent_status", "alertname", "severity", "service",
    "route", "method", "summary", "description", "labels_json", "annotations_json",
    "dashboard_link", "starts_at", "ends_at", "received_at", "resolved_at",
    "evidence_dir", "evidence_json", "evidence_error", "git_branch", "commit_sha",
    "agent_session_id", "agent_exit_code", "agent_summary", "error",
    "created_at", "updated_at",
]


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def create(self, incident: dict) -> dict:
        record = {**incident, "created_at": _now(), "updated_at": _now()}
        columns = ", ".join(record)
        placeholders = ", ".join("?" for _ in record)
        with self.connect() as db:
            db.execute(
                f"INSERT INTO incidents ({columns}) VALUES ({placeholders})",
                tuple(record.values()),
            )
        return self.get(record["id"])

    def get(self, incident_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM incidents WHERE id = ?", (incident_id,)
            ).fetchone()
        return _decode(row)

    def list(self, limit: int = 50) -> list[dict]:
        """Recent incidents without the evidence blob.

        One incident carries up to a few hundred log records plus full stacktraces,
        so including it here would make the index endpoint unusable.
        """
        columns = ", ".join(
            name for name in _COLUMNS if name != "evidence_json"
        )
        with self.connect() as db:
            rows = db.execute(
                f"SELECT {columns} FROM incidents ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_decode(row) for row in rows]

    def recent_for_fingerprint(self, fingerprint: str, cooldown_seconds: int) -> dict | None:
        """Newest still-relevant incident for this fingerprint, if inside the cooldown.

        The alert rule stays firing for 5 minutes after the last 5xx and Grafana
        re-notifies on every group_interval, so without this the same fault would
        queue a fresh investigation each time.

        Timestamps are compared in Python rather than in SQL: created_at is stored
        as offset-aware ISO 8601, which does not compare correctly against SQLite's
        space-separated datetime() output.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=cooldown_seconds)
        with self.connect() as db:
            rows = db.execute(
                """SELECT * FROM incidents
                   WHERE fingerprint = ?
                   ORDER BY created_at DESC LIMIT 20""",
                (fingerprint,),
            ).fetchall()
        for row in rows:
            created = _parse_time(row["created_at"])
            if created is not None and created >= cutoff:
                return _decode(row)
        return None

    def latest_for_fingerprint(self, fingerprint: str) -> dict | None:
        """Newest incident for this fingerprint, however old.

        Deliberately unbounded, unlike recent_for_fingerprint. A resolve can arrive
        long after the firing notification: this rule stays firing for five minutes
        after the last 5xx, and group_interval adds more on top of that. The cooldown
        exists to avoid duplicate investigations, not to lose resolutions.
        """
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM incidents WHERE fingerprint = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (fingerprint,),
            ).fetchone()
        return _decode(row)

    def update(self, incident_id: str, **fields) -> dict | None:
        if not fields:
            return self.get(incident_id)
        fields["updated_at"] = _now()
        assignments = ", ".join(f"{name} = ?" for name in fields)
        with self.connect() as db:
            db.execute(
                f"UPDATE incidents SET {assignments} WHERE id = ?",
                (*fields.values(), incident_id),
            )
        return self.get(incident_id)


def _decode(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    record = dict(row)
    for field in ("labels_json", "annotations_json", "evidence_json"):
        raw = record.pop(field, None)
        record[field.removesuffix("_json")] = json.loads(raw) if raw else None
    return record