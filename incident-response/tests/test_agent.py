from __future__ import annotations

import asyncio
import dataclasses
import subprocess
from pathlib import Path

import pytest

from incident_response.agent import AgentRunner, _branch_name, _parse_events
from incident_response.config import Config
from incident_response.evidence import Evidence


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway git repository with one commit."""
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    (root / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)
    return root


@pytest.fixture
def live_config(config: Config, repo: Path) -> Config:
    """The same config with dry_run off and a real repo, so the git path runs."""
    return dataclasses.replace(config, repo_root=repo, dry_run=False)


@pytest.fixture
def incident() -> dict:
    return {
        "id": "20261004T161515-801252",
        "fingerprint": "abc123",
        "route": "/api/orders/{order_id}",
        "method": "GET",
        "service": "order-tracker",
        "alertname": "5xx responses on an order lookup endpoint",
        "started_at": "2026-10-04T16:15:00+00:00",
    }


@pytest.fixture
def evidence(config: Config) -> Evidence:
    return Evidence(
        alert={"summary": "5xx on GET /api/orders/{order_id}"},
        endpoint={"method": "GET", "route": "/api/orders/{order_id}", "service": "order-tracker"},
        app_url="http://127.0.0.1:8000",
        logs={"query": '{service_name="order-tracker"}', "count": 0, "records": []},
        traces={"traces": []},
        metrics={"queries": {}, "results": {}},
    )


def current_branch(root: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=root, check=True, capture_output=True, text=True,
    ).stdout.strip()


def test_branch_name_is_a_usable_git_ref():
    name = _branch_name({
        "id": "20261004T161515-801252",
        "route": "/api/orders/{order_id}",
        "method": "GET",
    })
    assert name == "incident/api-orders-order_id-20261004T161515"
    assert "{" not in name and "}" not in name and not name.endswith("-")


@pytest.mark.asyncio
async def test_ensure_branch_creates_and_reuses_a_branch(live_config, incident):
    runner = AgentRunner(live_config)

    created = await runner._ensure_branch(incident)
    assert created == "incident/api-orders-order_id-20261004T161515"
    assert current_branch(live_config.repo_root) == created

    # A second incident on the same branch must switch, not fail.
    (live_config.repo_root / "other.txt").write_text("x\n")
    again = await runner._ensure_branch({**incident, "git_branch": created})
    assert again == created
    assert current_branch(live_config.repo_root) == created


@pytest.mark.asyncio
async def test_ensure_branch_is_idempotent_on_the_same_branch(live_config, incident):
    runner = AgentRunner(live_config)
    branch = await runner._ensure_branch(incident)
    assert await runner._ensure_branch(incident) == branch


@pytest.mark.asyncio
async def test_ensure_branch_returns_none_instead_of_raising(live_config, incident, monkeypatch):
    """A git failure must degrade to 'no branch', never crash the investigation."""

    async def boom(*args, **kwargs):
        raise FileNotFoundError("git")

    runner = AgentRunner(live_config)
    monkeypatch.setattr(runner, "_git", boom)
    assert await runner._ensure_branch(incident) is None


@pytest.mark.asyncio
async def test_dry_run_never_touches_git_or_the_assistant(config: Config, incident, evidence, tmp_path):
    runner = AgentRunner(config)
    calls: list = []

    async def fail(*args, **kwargs):
        calls.append(args)
        raise AssertionError("dry_run must not run git or the assistant")

    runner._git = fail
    runner._head_sha = fail
    result = await runner.run(incident=incident, evidence=evidence, evidence_dir=tmp_path / "ev")
    assert result.status == "skipped"
    assert result.git_branch is None
    assert calls == []


@pytest.mark.asyncio
async def test_run_invokes_the_assistant_and_records_the_commit(live_config, incident, evidence, tmp_path):
    """End to end through a fake assistant binary that commits like the real one."""
    fake = tmp_path / "fake-opencode"
    fake.write_text(
        "#!/bin/sh\n"
        'echo \'{"type":"message.updated","properties":{"info":{"id":"ses_1",'
        '"role":"assistant"}}}\'\n'
        'echo \'{"type":"message.part.updated","properties":{"part":{"id":"prt_1",'
        '"type":"text","text":"Fixed the date rollover and added a regression test."}}}\'\n'
        "mkdir -p app tests\n"
        "echo 'fixed' > app/main.py\n"
        "echo 'test' > tests/test_dates.py\n"
        "git add app/main.py tests/test_dates.py\n"
        "git commit -qm 'fix: month rollover'\n"
    )
    fake.chmod(0o755)
    runner = AgentRunner(dataclasses.replace(live_config, opencode_bin=str(fake)))

    evidence_dir = tmp_path / "ev"
    result = await runner.run(incident=incident, evidence=evidence, evidence_dir=evidence_dir)

    assert result.status == "done", result.error
    assert result.exit_code == 0
    assert result.session_id == "ses_1"
    assert result.git_branch == "incident/api-orders-order_id-20261004T161515"
    assert result.summary and "rollover" in result.summary
    head = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"],
        cwd=live_config.repo_root, check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert head == "fix: month rollover"
    assert result.commit_sha and len(result.commit_sha) == 40
    assert (evidence_dir / "prompt.md").exists()
    assert (evidence_dir / "agent.jsonl").exists()
    # The prompt is rewritten once the branch exists, so it can name the branch.
    assert "incident/api-orders-order_id-20261004T161515" in (evidence_dir / "prompt.md").read_text()


@pytest.mark.asyncio
async def test_run_reports_a_failing_assistant(live_config, incident, evidence, tmp_path):
    fake = tmp_path / "failing-opencode"
    fake.write_text('#!/bin/sh\necho "boom" >&2\nexit 3\n')
    fake.chmod(0o755)
    runner = AgentRunner(dataclasses.replace(live_config, opencode_bin=str(fake)))

    result = await runner.run(incident=incident, evidence=evidence, evidence_dir=tmp_path / "ev")
    assert result.status == "failed"
    assert result.exit_code == 3
    assert "boom" in (result.error or "")


@pytest.mark.asyncio
async def test_run_reports_a_missing_assistant_binary(live_config, incident, evidence, tmp_path):
    runner = AgentRunner(
        dataclasses.replace(live_config, opencode_bin="/nonexistent/opencode-binary")
    )
    result = await runner.run(incident=incident, evidence=evidence, evidence_dir=tmp_path / "ev")
    assert result.status == "failed"
    assert "not found" in (result.error or "")


@pytest.mark.asyncio
async def test_run_captures_the_branch_even_when_the_assistant_fails(live_config, incident, evidence, tmp_path):
    fake = tmp_path / "failing-opencode"
    fake.write_text("#!/bin/sh\nexit 1\n")
    fake.chmod(0o755)
    runner = AgentRunner(dataclasses.replace(live_config, opencode_bin=str(fake)))

    result = await runner.run(incident=incident, evidence=evidence, evidence_dir=tmp_path / "ev")
    assert result.git_branch == "incident/api-orders-order_id-20261004T161515"
    assert current_branch(live_config.repo_root) == result.git_branch


@pytest.mark.asyncio
async def test_run_kills_an_assistant_stuck_in_a_retry_loop(live_config, incident, evidence, tmp_path):
    """A denied command can make the model re-issue it forever.

    Observed live: 189 events, no progress, one worker held for the full timeout.
    The stall watchdog has to break that, and leave the reason on the incident.
    """
    fake = tmp_path / "looping-opencode"
    fake.write_text(
        "#!/bin/sh\n"
        'echo \'{"type":"text","sessionID":"ses_x","part":{"type":"text",'
        '"text":"trying again"}}\'\n'
        # Keep appending to the event stream but never finish, mimicking a model
        # that keeps retrying one rejected tool call.
        "i=0\n"
        "while [ $i -lt 1000 ]; do\n"
        '  echo "{\\"type\\":\\"tool_use\\",\\"sessionID\\":\\"ses_x\\",\\"part\\":{\\"type\\":\\"tool\\"}}" >> agent.jsonl\n'
        "  i=$((i+1)); sleep 0.2\n"
        "done\n"
    )
    fake.chmod(0o755)
    runner = AgentRunner(dataclasses.replace(
        live_config, opencode_bin=str(fake), stall_timeout_seconds=2
    ))

    evidence_dir = tmp_path / "ev"
    result = await asyncio.wait_for(
        runner.run(incident=incident, evidence=evidence, evidence_dir=evidence_dir), timeout=30
    )
    assert result.status == "stalled"
    assert "retry loop" in (result.error or "")
    assert result.git_branch == "incident/api-orders-order_id-20261004T161515"


@pytest.mark.asyncio
async def test_a_chatty_assistant_is_not_mistaken_for_a_stalled_one(
    live_config, incident, evidence, tmp_path
):
    """Output that keeps growing is progress, however slow the model is."""
    fake = tmp_path / "slow-opencode"
    fake.write_text(
        "#!/bin/sh\n"
        "i=0\n"
        "while [ $i -lt 6 ]; do\n"
        '  echo "{\\"type\\":\\"text\\",\\"sessionID\\":\\"ses_y\\",\\"part\\":{\\"type\\":\\"text\\",'
        '\\"text\\":\\"step $i\\"}}" >> agent.jsonl\n'
        "  i=$((i+1)); sleep 0.4\n"
        "done\n"
        'echo \'{"type":"text","sessionID":"ses_y","part":{"type":"text","text":"all done"}}\'\n'
    )
    fake.chmod(0o755)
    runner = AgentRunner(dataclasses.replace(
        live_config, opencode_bin=str(fake), stall_timeout_seconds=2
    ))

    result = await asyncio.wait_for(
        runner.run(incident=incident, evidence=evidence, evidence_dir=tmp_path / "ev"), timeout=30
    )
    assert result.status == "done", result.error
    assert result.summary == "all done"


def test_parse_events_reads_the_real_opencode_stream(tmp_path: Path):
    """The exact shape `opencode run --format json` v1.18.34 emits.

    Flat objects, session id at the top level, text under part.text. Regression
    guard: this parser was first written against an invented nested shape and read
    a real successful run as "no assistant message in agent output".
    """
    path = tmp_path / "agent.jsonl"
    path.write_text("\n".join([
        '{"type":"step_start","sessionID":"ses_ef84","part":{"id":"prt_1",'
        '"messageID":"msg_1","type":"step-start"}}',
        '{"type":"text","sessionID":"ses_ef84","part":{"id":"prt_2","messageID":"msg_1",'
        '"type":"text","text":"I will start by exploring the repository."}}',
        '{"type":"tool_use","sessionID":"ses_ef84","part":{"type":"tool","tool":"read",'
        '"state":{"status":"completed","input":{"filePath":"app/main.py"}}}}',
        '{"type":"text","sessionID":"ses_ef84","part":{"id":"prt_9","messageID":"msg_2",'
        '"type":"text","text":"Root cause: month rollover. Fixed and committed."}}',
        '{"type":"step_finish","sessionID":"ses_ef84","part":{"type":"step-finish",'
        '"reason":"stop","cost":0}}',
    ]))
    result = _parse_events(path)
    assert result.status == "done", result.error
    assert result.session_id == "ses_ef84"
    # The last text event is the answer; the running commentary is not the summary.
    assert result.summary == "Root cause: month rollover. Fixed and committed."


def test_parse_events_reads_a_legacy_nested_stream(tmp_path: Path):
    path = tmp_path / "agent.jsonl"
    path.write_text("\n".join([
        '{"type":"session.updated","properties":{"info":{"id":"ses_42"}}}',
        '{"type":"message.part.updated","properties":{"part":'
        '{"type":"text","text":"partial"}}}',
        '{"type":"message.updated","properties":{"info":{"role":"assistant",'
        '"parts":[{"type":"text","text":"Root cause: day overflow."}]}}}',
    ]))
    result = _parse_events(path)
    assert result.session_id == "ses_42"
    assert result.summary == "Root cause: day overflow."


def test_parse_events_tolerates_noise_and_shapes(tmp_path: Path):
    path = tmp_path / "agent.jsonl"
    path.write_text(
        "\n".join([
            "",
            "not json at all",
            '{"type":"message.updated","properties":{"info":{"role":"assistant"}}}',
            '{"type":"message.part.updated","properties":{"part":{"type":"text",'
            '"text":"part one "}}}',
            '{"type":"message.part.updated","properties":{"part":{"type":"text",'
            '"text":"part two"}}}',
            '{"type":"step.finish"}',
        ])
    )
    result = _parse_events(path)
    assert result.status == "done"
    # Two part-level texts with no complete message: the last one wins.
    assert result.summary == "part two"
    assert result.error is None


def test_parse_events_flags_an_explicit_error(tmp_path: Path):
    path = tmp_path / "agent.jsonl"
    path.write_text(
        '{"type":"error","properties":{"error":{"name":"AuthError",'
        '"data":{"message":"no credentials"}}}}'
    )
    result = _parse_events(path)
    assert result.status == "failed"
    assert "no credentials" in (result.error or "")


def test_parse_events_on_an_empty_stream(tmp_path: Path):
    """No assistant text at all is a failure, not a silent success."""
    path = tmp_path / "agent.jsonl"
    path.write_text("")
    result = _parse_events(path)
    assert result.status == "failed"
    assert "no assistant message" in (result.error or "")