from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shlex
import signal
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .evidence import Evidence

log = logging.getLogger("incident_response.agent")

# Frames inside the container's site-packages are not actionable; keep the frames
# that point at the service's own source.
_FRAMEWORK_PATH = re.compile(r"site-packages|/\.venv/|/usr/lib/python")
# Prologue and chaining notes, never the failing line itself.
_TRACE_PROLOGUE = re.compile(
    r"^(Traceback \(most recent call last\)|During handling of the above exception|"
    r"The above exception was the direct cause)"
)
# An exception line: ValueError, KeyError, pydantic_core.ValidationError, ...
_EXCEPTION_LINE = re.compile(r"^[A-Za-z_][\w.]*(Error|Exception|Exit|Interrupt|Warning)\b")

# Tempo stores span attribute values with a 2048-byte cap, so a long Python
# stacktrace arrives cut off mid-frame. Detected rather than assumed, because a
# truncated stacktrace that looks complete is worse than none.
_STALL_POLL_SECONDS = 5
_TRUNCATED_AT = 2048


@dataclass
class AgentResult:
    status: str
    exit_code: int | None = None
    session_id: str | None = None
    summary: str | None = None
    commit_sha: str | None = None
    git_branch: str | None = None
    error: str | None = None


class AgentRunner:
    def __init__(self, config: Config):
        self.config = config

    def build_prompt(self, incident: dict, evidence: Evidence) -> str:
        sections = [
            _TASK_HEADER,
            _render_alert(incident, evidence),
            _render_logs(evidence),
            _render_traces(evidence),
            _render_metrics(evidence),
        ]
        notes = _render_git_notes(incident)
        if notes:
            sections.append(notes)
        sections.append(_TASK_FOOTER)
        return "\n\n".join(section.strip() for section in sections if section.strip()) + "\n"

    async def run(self, *, incident: dict, evidence: Evidence, evidence_dir: Path) -> AgentResult:
        # main.py has already written a prompt as part of the evidence bundle. Rewritten
        # here only after the branch exists, so the prompt can name the branch the
        # agent is expected to commit onto.
        if self.config.dry_run:
            log.info("dry run: not spawning the coding assistant")
            return AgentResult(status="skipped", summary="dry run: agent not started")

        evidence_dir.mkdir(parents=True, exist_ok=True)
        branch = await self._ensure_branch(incident)
        incident["tree_dirty"] = await self._tree_dirty()
        if branch or incident["tree_dirty"]:
            incident["git_branch"] = branch
            (evidence_dir / "prompt.md").write_text(
                self.build_prompt(incident, evidence), encoding="utf-8"
            )

        command = [self.config.opencode_bin, "run", "--format", "json"]
        if self.config.opencode_agent:
            command += ["--agent", self.config.opencode_agent]
        if self.config.opencode_model:
            command += ["--model", self.config.opencode_model]
        route = evidence.endpoint.get("route") or "alert"
        command += ["--title", f"incident {incident['id']}: {route}"]
        command.append(self.build_prompt(incident, evidence))

        log.info("starting coding assistant: %s", shlex.join(command[:6]) + " ...")
        started_at = datetime.now(timezone.utc)
        events_path = evidence_dir / "agent.jsonl"
        stderr = b""
        exit_code: int | None = None
        stall_reason: str | None = None

        try:
            with events_path.open("w", encoding="utf-8") as events:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    cwd=self.config.repo_root,
                    stdout=events,
                    stderr=asyncio.subprocess.PIPE,
                    # The agent has its own permission allow-list; it must not be
                    # able to wait on an interactive approval prompt.
                    stdin=asyncio.subprocess.DEVNULL,
                    # Its own process group, so the watchdog can take down anything
                    # it spawned rather than leaking orphans.
                    start_new_session=True,
                )
                watchdog = asyncio.create_task(
                    _watch_for_stall(process, events_path, self.config.stall_timeout_seconds)
                )
                try:
                    _, stderr = await asyncio.wait_for(
                        process.communicate(), timeout=self.config.agent_timeout_seconds
                    )
                finally:
                    watchdog.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        stall_reason = await watchdog
                exit_code = process.returncode
        except asyncio.TimeoutError:
            log.error("coding assistant timed out after %ss", self.config.agent_timeout_seconds)
            return AgentResult(
                status="failed", git_branch=branch,
                error=f"timed out after {self.config.agent_timeout_seconds}s",
            )
        except FileNotFoundError:
            return AgentResult(
                status="failed", git_branch=branch,
                error=f"{self.config.opencode_bin} not found on PATH",
            )

        if stall_reason:
            log.error("coding assistant stalled: %s", stall_reason)
            result = _parse_events(events_path)
            result.status = "stalled"
            result.error = stall_reason
            result.exit_code = exit_code
            result.git_branch = branch
            result.commit_sha = await self._head_sha()
            return result

        result = _parse_events(events_path)
        result.exit_code = exit_code
        result.git_branch = branch
        if stderr:
            (evidence_dir / "agent.stderr.log").write_bytes(stderr)
        if exit_code != 0:
            # The process failed, so its own stderr explains more than a generic
            # "no assistant message" from parsing an empty stream.
            result.error = _tail(stderr) or result.error or f"exit code {exit_code}"

        result.commit_sha = await self._head_sha()
        log.info(
            "coding assistant finished: status=%s exit=%s branch=%s commit=%s elapsed=%ss",
            result.status, result.exit_code, branch or "(unchanged)",
            result.commit_sha or "(none)",
            int((datetime.now(timezone.utc) - started_at).total_seconds()),
        )
        return result

    async def _ensure_branch(self, incident: dict) -> str | None:
        """Move the checkout onto a dedicated incident branch.

        Nothing is committed by this service itself, but the agent will commit, and
        a fault fix should not land on whatever branch happens to be checked out.
        Uncommitted work in the tree carries over to the new branch untouched.
        """
        branch = incident.get("git_branch") or _branch_name(incident)
        try:
            current = await self._git_out("rev-parse", "--abbrev-ref", "HEAD")
            exists = await self._git("show-ref", "--verify", "--quiet", f"refs/heads/{branch}")
            # show-ref exits 0 when the ref exists, so a miss means we must create it.
            creating = exists.returncode != 0
            args = ("switch", "-c", branch) if creating else ("switch", branch)
            result = await self._git(*args)
        except OSError as exc:
            # A missing or unusable git must not take the investigation down with it.
            # The assistant still runs, just without a dedicated branch.
            log.warning("could not prepare an incident branch: %s", exc)
            return None
        if not current:
            return None
        if current == branch:
            return branch
        if result.returncode != 0:
            log.warning("could not switch to branch %s: %s", branch, _tail(result.stderr))
            return None
        log.info("%s branch %s", "created" if creating else "switched to", branch)
        return branch

    async def _tree_dirty(self) -> bool:
        """Whether the checkout already had uncommitted changes."""
        result = await self._git("status", "--porcelain")
        return result.returncode == 0 and bool((result.stdout or "").strip())

    async def _git_out(self, *args: str) -> str:
        """Stripped stdout of a git call, or "" when it fails."""
        result = await self._git(*args)
        if result.returncode != 0:
            log.warning("git %s failed: %s", " ".join(args), _tail(result.stderr))
            return ""
        return (result.stdout or "").strip()

    async def _git(self, *args: str) -> subprocess.CompletedProcess:
        process = await asyncio.create_subprocess_exec(
            "git", *args, cwd=self.config.repo_root,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        return subprocess.CompletedProcess(
            args, process.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")
        )

    async def _head_sha(self) -> str | None:
        result = await self._git("rev-parse", "HEAD")
        if result.returncode != 0:
            return None
        return (result.stdout or "").strip() or None


async def _watch_for_stall(
    process: asyncio.subprocess.Process, events_path: Path, stall_timeout: int
) -> str | None:
    """Kill the assistant if it stops making progress.

    A permission denial can put the model into a retry loop: it re-issues the same
    rejected command indefinitely, which burns tokens and, because there is a single
    worker, holds up every later incident until the full timeout expires. Progress is
    measured as the event stream growing, so a legitimately slow model is fine as long
    as it is still saying something.

    Returns the reason it gave up, or None when the process finished on its own.
    """
    last_size = -1
    last_change = time.monotonic()
    while True:
        await asyncio.sleep(_STALL_POLL_SECONDS)
        if process.returncode is not None:
            return None
        try:
            size = events_path.stat().st_size
        except OSError:
            size = 0
        if size != last_size:
            last_size = size
            last_change = time.monotonic()
            continue
        if time.monotonic() - last_change < stall_timeout:
            continue
        reason = (
            f"no output for {stall_timeout}s, killed to stop a retry loop "
            "(a denied command is the usual cause; check agent.stderr.log)"
        )
        _kill_process_group(process)
        return reason


def _kill_process_group(process: asyncio.subprocess.Process) -> None:
    """SIGKILL the assistant and anything it spawned, ignoring an already-dead group."""
    if process.returncode is not None:
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        with contextlib.suppress(ProcessLookupError):
            process.kill()


def _branch_name(incident: dict) -> str:
    route = (incident.get("route") or "alert").strip("/").replace("/", "-").replace("{", "").replace("}", "")
    slug = re.sub(r"-+", "-", route).strip("-") or "alert"
    # The id is 20261004T162213-b95470. The date alone is not enough: two faults on
    # the same endpoint on the same day would land on one branch and mix their fixes.
    stamp = str(incident.get("id") or "").split("-", 1)[0] or "unknown"
    return f"incident/{slug}-{stamp}"


def _parse_events(events_path: Path) -> AgentResult:
    """Pull the session id and the assistant's final answer out of opencode's JSON events.

    Verified against `opencode run --format json` v1.18.34, which emits one flat JSON
    object per line with no `properties` wrapper:

        {"type": "text", "sessionID": "ses_...", "part": {"type": "text", "text": "..."}}
        {"type": "tool_use", "part": {"type": "tool", "tool": "bash", "state": {...}}}
        {"type": "step_start" | "step_finish", "part": {...}}

    The older nested message.updated / message.part.updated shapes are still handled,
    because the raw stream is kept in agent.jsonl and a shape change here should
    degrade the summary rather than hide a run that actually happened.
    """
    session_id: str | None = None
    if not events_path.exists():
        return AgentResult(status="failed", error="no agent output captured")

    texts: list[str] = []
    complete: str | None = None
    errors: list[str] = []

    for line in events_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue

        properties = event.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        info = properties.get("info")
        if isinstance(info, dict) and isinstance(info.get("id"), str):
            session_id = info["id"]

        # The current flat shape carries the session id at the top level.
        if isinstance(event.get("sessionID"), str):
            session_id = event["sessionID"]

        part = event.get("part")
        part = part if isinstance(part, dict) else properties.get("part")
        part = part if isinstance(part, dict) else {}

        event_type = event.get("type")
        if event_type == "error":
            message = _error_message(properties) or _error_message(event)
            if message:
                errors.append(message)
        elif event_type == "text" or part.get("type") == "text":
            # Covers the flat "text" events and the nested message.part.updated
            # stream alike, so both shapes end up in the same place.
            text = part.get("text") or ""
            if text.strip():
                texts.append(text)
        elif event_type == "message.updated" and isinstance(info, dict):
            if info.get("role") == "assistant":
                text = _message_text(info.get("parts"))
                if text:
                    # The whole message, so it supersedes any part-level stream.
                    complete = text

    if errors:
        return AgentResult(status="failed", session_id=session_id, error=errors[-1])

    # The last text event is the assistant's final answer; the earlier ones are
    # running commentary like "let me look at the file", which is not a summary.
    summary = (complete or (texts[-1] if texts else "")).strip() or None
    if summary:
        return AgentResult(status="done", session_id=session_id, summary=summary)
    return AgentResult(
        status="failed", session_id=session_id,
        error="no assistant message in agent output",
    )


def _error_message(properties: dict) -> str | None:
    """Dig a human-readable message out of an error event, whatever shape it takes."""
    candidates = [properties.get("message"), properties.get("error")]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate:
            return candidate
        if isinstance(candidate, dict):
            name = candidate.get("name")
            data = candidate.get("data")
            if isinstance(data, dict):
                for key in ("message", "detail", "error"):
                    if isinstance(data.get(key), str) and data[key]:
                        return f"{name}: {data[key]}" if name else data[key]
            for key in ("message", "detail"):
                if isinstance(candidate.get(key), str) and candidate[key]:
                    return candidate[key]
    return None


def _message_text(parts: object) -> str:
    if isinstance(parts, str):
        return parts
    if isinstance(parts, list):
        texts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text"]
        return "\n".join(t for t in texts if t)
    return ""


def _tail(raw: bytes | str | None, limit: int = 2000) -> str:
    if not raw:
        return ""
    text = raw.decode(errors="replace") if isinstance(raw, bytes) else raw
    return text[-limit:].strip()


# Prompt sections ---------------------------------------------------------


_TASK_HEADER = """\
An alert fired on a service in this repository and you are being invoked
unattended to diagnose and fix it. Everything you need is below; you do not need
to ask questions and there is nobody to answer them.

Work inside the current checkout, which is already on a branch dedicated to this
incident."""

_TASK_FOOTER = """\
What to do:

1. Reproduce it if you can: the concrete request above hits the failing endpoint.
2. Read the failing code and identify the root cause. State it in one sentence
   before changing anything. The exception message is usually enough to place it.
3. Write a regression test that fails on the current code and passes after the fix.
4. Make the smallest change that fixes the root cause. Do not refactor unrelated
   code, and do not change the public API or the telemetry behaviour.
5. Run `uv run --frozen pytest -q` from the repository root and make it pass.
6. Commit: `git add` only the files you changed, never `git add -A` or
   `git commit -a`, then `git commit` with a message that names the root cause and
   references the incident id.

Hard rules, these exist because you are running unattended:

- Never run `git push`, `git commit --amend`, `git reset --hard`, `git clean`,
  `git checkout -- .`, `git stash`, or any other destructive git command.
- Never modify files outside this repository.
- Never edit or weaken an existing test to make it pass.
- If you cannot fix the fault, commit nothing and say so in your final message."""


def _render_alert(incident: dict, evidence: Evidence) -> str:
    endpoint = evidence.endpoint
    lines = [
        "## Alert",
        "",
        f"- id: {incident['id']}",
        f"- rule: {incident.get('alertname')}",
        f"- severity: {incident.get('severity')}",
        f"- started: {incident.get('starts_at')}",
        f"- affected endpoint: {endpoint['method']} {endpoint['route']}"
        f" (service_name={endpoint['service']})",
    ]
    if evidence.alert.get("summary"):
        lines.append(f"- summary: {evidence.alert['summary']}")
    if evidence.alert.get("description"):
        lines += ["", evidence.alert["description"].strip()]

    # The route template does not tell the assistant what to curl. The concrete
    # target is in the span and in the order_id on the log record.
    targets = sorted({
        span["http_target"] for trace in evidence.traces.get("traces", [])
        for span in trace["spans"] if span.get("http_target")
    })
    order_ids = sorted({
        record["order_id"] for record in evidence.logs.get("records", [])
        if record.get("order_id")
    })
    if targets:
        lines += ["", "Concrete requests that failed:"]
        lines += [f"- `{endpoint['method']} {target}`" for target in targets[:5]]
        if order_ids:
            lines.append(
                f"\nFailing order ids: {', '.join(order_ids[:10])}. The app runs on "
                f"`{evidence.app_url}` and can be reproduced with "
                f"`curl -i {evidence.app_url}{targets[0]}`."
            )
    if evidence.errors:
        lines += ["", "Evidence gaps (some backends did not answer):"]
        lines += [f"- {error}" for error in evidence.errors]
    return "\n".join(lines)


def _render_logs(evidence: Evidence) -> str:
    logs = evidence.logs
    lines = ["## Failing log records (Loki)", ""]
    records = logs.get("records") or []
    if not records:
        return "\n".join(lines + ["No matching log records in the window."])
    if logs.get("query"):
        lines.append(f"LogQL: `{logs['query']}`")
    lines.append(f"{logs.get('count', len(records))} record(s), newest first.")
    lines.append("")
    for record in records[:20]:
        lines.append(
            f"- {record.get('timestamp_iso', '?')} `{record.get('message', '')}`"
            f" order_id={record.get('order_id') or '-'}"
            f" error_type={record.get('error_type') or '-'}"
            f" status={record.get('http_response_status_code') or '-'}"
            f" trace_id={record.get('trace_id') or '-'}"
        )
    return "\n".join(lines)


def _render_traces(evidence: Evidence) -> str:
    traces = evidence.traces
    lines = ["## Traces (Tempo)", ""]
    if not traces.get("traces"):
        return "\n".join(lines + ["No traces retrieved."])

    truncated = [
        trace for trace in traces["traces"]
        if any(_is_truncated(e.get("stacktrace") or "") for e in trace["exceptions"])
    ]
    if truncated:
        lines += [
            f"> Note: Tempo caps a span attribute value at {_TRUNCATED_AT} bytes. In "
            f"{len(truncated)} of {len(traces['traces'])} of these traces the Python "
            "stacktrace was cut off before the failing frame, because a traceback "
            "prints the framework frames first. The exception type and message below "
            "are separate attributes and are never truncated. Work from those, the "
            "concrete request target, and the log records.",
            "",
        ]

    for trace in traces["traces"]:
        lines.append(f"### trace {trace['trace_id']}")
        for span in trace["spans"]:
            lines.append(
                f"- span `{span['name']}` {span['duration_ms']}ms status={span['status']}"
                f" target={span['http_target']} route={span['http_route']}"
                f" http_status={span['http_status_code']}"
            )
        for exception in trace["exceptions"]:
            lines += ["", f"Exception `{exception['type']}`: {exception['message']}", ""]
            frames = _application_frames(exception.get("stacktrace") or "")
            if frames:
                lines += ["Frames from this repository:", "```"]
                lines += frames
                lines += ["```"]
            else:
                lines.append("(no frames from this repository in the retained part)")
        lines.append("")
    return "\n".join(lines)


def _application_frames(stacktrace: str) -> list[str]:
    """Stack frames that point at this repository's source, not at the framework.

    The container's own site-packages frames are noise: they say FastAPI failed to
    handle an exception, not where the exception came from.
    """
    lines = stacktrace.splitlines()
    frames: list[str] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("File ") or _FRAMEWORK_PATH.search(stripped):
            continue
        source_line = lines[index + 1].strip() if index + 1 < len(lines) else ""
        frames.append(f"{stripped}\n    {source_line}")

    if not frames:
        frames = [line.strip() for line in lines
                  if line.strip() and not _TRACE_PROLOGUE.match(line.strip())]

    # The exception itself is the last top-level line, e.g. "ValueError: day is out
    # of range for month". It is often cut off by the attribute length cap, so this
    # is best-effort and the exception attributes are rendered separately.
    error_line = next(
        (line.strip() for line in reversed(lines) if _EXCEPTION_LINE.match(line.strip())),
        None,
    )
    if error_line:
        frames.append(error_line)
    return frames


def _is_truncated(stacktrace: str) -> bool:
    """True when the stacktrace was cut off rather than ending on an exception.

    Tempo caps a span attribute value at 2048 bytes. A Python stacktrace through
    FastAPI and Starlette middleware is longer than that, so the failing frame and
    the exception line are usually the part that gets dropped, while the frame that
    raised is still available as its own untruncated attribute.
    """
    if len(stacktrace) < _TRUNCATED_AT:
        return False
    lines = [line for line in stacktrace.splitlines() if line.strip()]
    return not lines or not _EXCEPTION_LINE.match(lines[-1].strip())


def _render_metrics(evidence: Evidence) -> str:
    metrics = evidence.metrics
    lines = ["## Metrics (Prometheus)", ""]
    if not metrics:
        return "\n".join(lines + ["No metrics retrieved."])
    results = metrics.get("results", {})
    lines += [
        f"- server_errors_total: {_scalar(results, 'server_errors_total')}",
        f"- server_errors_increase_5m: {_scalar(results, 'server_errors_increase_5m')}",
        f"- errors_increase_5m (4xx): {_scalar(results, 'errors_increase_5m')}",
        f"- lookups_total: {_scalar(results, 'lookups_total')}",
        "",
        "`increase()` extrapolates over the scrape interval, so its values are "
        "fractional and only approximate. The totals are exact.",
        "",
        "PromQL:",
    ]
    lines += [f"- `{promql}`" for promql in metrics.get("queries", {}).values()]
    return "\n".join(lines)


def _scalar(results: dict, name: str) -> str:
    series = results.get(name, {}).get("series") or []
    if not series:
        return "n/a"
    return ", ".join(f"{value:g}" for item in series for value in [item["value"]])


def _render_git_notes(incident: dict) -> str:
    branch = incident.get("git_branch")
    dirty = incident.get("tree_dirty")
    if not branch and not dirty:
        return ""
    lines = []
    if branch:
        lines.append(f"Commit to branch `{branch}`.")
    if dirty:
        lines.append(
            "The working tree already had uncommitted changes when this incident "
            "started, so `git add <file>` stages everything else that is in that "
            "file too. Run `git diff --cached` before committing and mention in "
            "your final message anything unrelated that got swept in."
        )
    return "\n" + "\n".join(lines) + "\n"