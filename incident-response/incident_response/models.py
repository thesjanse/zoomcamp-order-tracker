from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from pydantic import BaseModel, Field


class GrafanaAlert(BaseModel):
    status: str = "firing"
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    values: dict[str, float] = Field(default_factory=dict)
    startsAt: str | None = None
    endsAt: str | None = None
    generatorURL: str | None = None
    fingerprint: str | None = None

    @property
    def fingerprint_key(self) -> str:
        """Grafana's own fingerprint when present, else a stable hash of labels.

        Rules.yaml carries no explicit alertname label, so labels alone are enough
        to tell two endpoints' alert instances apart.
        """
        if self.fingerprint:
            return self.fingerprint
        blob = json.dumps(self.labels, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    @property
    def route(self) -> str:
        return self.labels.get("http_route", "")

    @property
    def method(self) -> str:
        return self.labels.get("http_request_method", "")

    @property
    def alertname(self) -> str:
        return self.labels.get("alertname") or self.labels.get("__alert_rule_uid__") or "alert"

    @property
    def service(self) -> str:
        return self.labels.get("service_name", "")


class GrafanaWebhook(BaseModel):
    receiver: str = ""
    status: str = "firing"
    alerts: list[GrafanaAlert] = Field(default_factory=list)
    groupLabels: dict[str, str] = Field(default_factory=dict)
    commonLabels: dict[str, str] = Field(default_factory=dict)
    commonAnnotations: dict[str, str] = Field(default_factory=dict)
    externalURL: str = ""
    title: str = ""
    state: str = ""
    orgId: int = 1

    @staticmethod
    def parse_time(value: str | None) -> datetime | None:
        """Grafana sends RFC 3339 with an offset, and 0001-01-01 for open-ended."""
        if not value or value.startswith("0001-01-01"):
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    @property
    def started_at(self) -> datetime:
        for alert in self.alerts:
            parsed = self.parse_time(alert.startsAt)
            if parsed:
                return parsed
        return datetime.now(timezone.utc)

    @property
    def is_firing(self) -> bool:
        if self.status == "resolved":
            return False
        if self.alerts:
            return any(a.status != "resolved" for a in self.alerts)
        return True