# Incident Response

Turns a Grafana alert into a saved evidence bundle and a headless coding-assistant
investigation.

It listens on port `8001`, receives the alert webhook Grafana sends when the
*5xx responses on an order lookup endpoint* rule fires, and does four things:

1. Reads the alert's labels and annotations to work out which endpoint broke.
2. Pulls the three signals for that endpoint out of the running stack — the failing
   log records from Loki, the matching traces from Tempo, the counters from
   Prometheus — and saves them.
3. Writes that evidence to disk as both JSON and flat files.
4. Starts [opencode](https://opencode.ai) headless with the evidence and the failing
   stacktrace, so it can find the root cause, fix it, test it, and commit the fix on
   a branch of its own.

Nothing here reads Grafana. Once the webhook has arrived, everything the responder
needs comes from Loki, Tempo and Prometheus directly, using the `trace_id` that the
app already puts on every log record and span.

## Run it

The service runs on the host, not in Compose, so that it starts the coding assistant
with your own opencode credentials and against your own checkout.

```bash
cd incident-response
uv sync --frozen
uv run --frozen uvicorn incident_response.main:app --host 0.0.0.0 --port 8001
```

It must bind `0.0.0.0`, not `127.0.0.1`: Grafana runs in a container and reaches the
host through `host.docker.internal`, which the `grafana` service resolves via the
`extra_hosts` entry in `compose.yaml`.

Run the tests with `uv run --frozen pytest -q`.

## Try it without Grafana

```bash
cd incident-response
INCIDENT_RESPONSE_DRY_RUN=1 uv run --frozen uvicorn incident_response.main:app --port 8001
```

Then, in another shell, break the app and post the alert by hand:

```bash
curl -s 127.0.0.1:8000/api/orders/express-1002    # 500
curl -s -X POST 127.0.0.1:8001/alerts -H 'content-type: application/json' -d '{
  "status": "firing",
  "alerts": [{
    "status": "firing",
    "fingerprint": "manual-test",
    "labels": {
      "alertname": "5xx responses on an order lookup endpoint",
      "severity": "critical",
      "service_name": "order-tracker",
      "http_request_method": "GET",
      "http_route": "/api/orders/{order_id}"
    },
    "annotations": {
      "summary": "5xx responses on GET /api/orders/{order_id}",
      "description": "1 5xx response(s) so far."
    },
    "values": {"B": 1},
    "startsAt": "'"$(date -u +%Y-%m-%dT%H:%M:%SZ)"'",
    "endsAt": "0001-01-01T00:00:00Z"
  }]
}'
```

`INCIDENT_RESPONSE_DRY_RUN=1` captures and saves the evidence but never starts the
assistant and never touches git. Use it to check the wiring first.

Then look at the result:

```bash
curl -s 127.0.0.1:8001/incidents | python3 -m json.tool
curl -s 127.0.0.1:8001/incidents/<id>/evidence | python3 -m json.tool
ls incident-response/data/incidents/<id>/
```

## Through Grafana

`observability/grafana/provisioning/alerting/incident-response.yaml` provisions both
the contact point and the notification policy tree that routes to it. Applying it:

```bash
docker compose up -d --wait
curl -s 127.0.0.1:8000/api/orders/express-1002   # 500
```

The rule fires 30 to 60 seconds later, and the webhook lands on `/alerts` within
about 15 seconds after that. Grafana's Alerting → Contact points page shows the
receiver, and the incident shows up in the responder's own log.

If Grafana never reaches the service, the usual cause is that `extra_hosts` is
missing from the `grafana` service. Check it from inside the container:

```bash
docker exec order-tracker-grafana-1 getent hosts host.docker.internal
```

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/alerts` | Grafana webhook. Returns `202` immediately |
| GET | `/healthz` | Liveness, queue depth, dry-run flag |
| GET | `/incidents` | Recent incidents, without the evidence blob |
| GET | `/incidents/{id}` | One incident, with the full evidence |
| GET | `/incidents/{id}/evidence` | Just the evidence, as JSON |
| GET | `/incidents/{id}/agent` | The assistant's raw JSON event stream |
| GET | `/incidents/{id}/agent?format=prompt` | The exact prompt it was given |

`POST /alerts` answers in milliseconds and investigates in the background. A webhook
receiver that blocks gets cut off by Grafana, and an investigation takes minutes.

## Repeat notifications

The alert rule stays firing for five minutes after the last 5xx, and Grafana
re-notifies on every `group_interval`. Each notification carries a `fingerprint`, and
a firing alert whose fingerprint is already covered by an incident inside
`INCIDENT_RESPONSE_COOLDOWN_SECONDS` is acknowledged and dropped. Without that, one
fault would queue several investigations and several model runs.

`status: resolved` is also accepted. It marks the incident resolved and starts
nothing. The lookup for a resolve is deliberately *not* bounded by the cooldown:
this rule stays firing for five minutes after the last 5xx, so a resolve routinely
lands long after the notification it closes, and a cooldown-bounded lookup would
silently drop it and leave the incident open forever.

Two consequences worth knowing. A re-fire inside the cooldown window is dropped
even if the alert resolved in between, so a flapping fault is investigated once per
cooldown rather than once per flap. And if Grafana never delivers the resolve at
all, the incident stays `firing`; nothing expires it automatically, because
guessing when a fault is over is worse than showing it as open.

One worker handles the queue, so investigations never overlap.

## What gets saved

```
data/incidents.db                                   SQLite, one row per incident
data/incidents/<id>/evidence.json                   everything below, as one document
data/incidents/<id>/logs.log                        failing records, tab separated
data/incidents/<id>/traces.log                      spans and the root-cause stacktrace
data/incidents/<id>/metrics.log                     the PromQL results
data/incidents/<id>/prompt.md                       the exact prompt the assistant got
data/incidents/<id>/agent.jsonl                     raw opencode JSON events
data/incidents/<id>/agent.stderr.log                only if the subprocess wrote to stderr
```

`data/` is disposable: deleting it loses history but the stack keeps running.

## Evidence sources

| Signal | Source | Query |
| --- | --- | --- |
| Logs | Loki | `{service_name="order-tracker"} \| http_route="…" \| order_lookup_outcome="error" \| detected_level="error"` |
| Traces | Tempo | `/api/v2/traces/<trace_id>`, for the ids found in those log records |
| Metrics | Prometheus | `order_lookup_requests_total` for the endpoint, by status class, total and `increase(…[5m])` |

Two details are worth knowing if you change these.

The OpenTelemetry attributes are **structured metadata** in Loki, not indexed
labels: `/loki/api/v1/labels` only reports `service_name` and `service_instance_id`.
Putting `order_lookup_outcome="error"` inside the stream selector returns nothing;
it has to come after a pipe.

Traces are fetched **by trace id**, not with a TraceQL search. With local block
storage, Tempo's search path only covers recent data and comes up empty for a minute
or so after startup, while a direct id lookup resolves immediately. The trace id
comes from the log record, which is the correlation the app already sets up.

Each source is collected independently. If Loki is down you still get traces and
metrics, and the gaps are listed under `errors` in the evidence and in the prompt.

## The coding assistant

`opencode run --agent incident-responder --format json` is started as a subprocess in
the repository root, with stdin closed so it can never block on an interactive
prompt. Its stdout is streamed to `agent.jsonl`, and its final message is recorded on
the incident as `agent_summary`.

The agent is defined in `.opencode/agent/incident-responder.md` at the repository
root, and its permissions are an allow-list: it may edit files, and run the test
suite and a small set of read-only git commands. `git push`, `git reset`, `git
clean`, `git checkout`, `git rebase`, `git merge`, `docker`, `curl` and `sudo` are
all denied by the permission rules, so they fail rather than needing to be caught by
the prompt. There is no `--auto`.

Being denied is not the same as being told to stop, and a model will sometimes retry
a rejected command instead of working around it. One live run spent 189 events
re-issuing the same denied `cd` before it was killed. So there are two guards: the
watchdog fails the incident with `agent_status=stalled` after
`INCIDENT_RESPONSE_STALL_TIMEOUT_SECONDS` of no new output at all, and
`INCIDENT_RESPONSE_AGENT_TIMEOUT_SECONDS` remains the outer bound. Because there is
one worker, a stalled assistant delays every later incident, which is why the stall
check exists at all.

Note that a denied `curl` means the agent cannot reproduce the fault over HTTP. It
will fall back to the application's own test client and say so in its final message.

Before the assistant starts, the service puts the checkout on a branch named
`incident/<route>-<timestamp>`. The fix therefore never lands on whatever branch you
happened to have checked out. The timestamp is the full `YYYYMMDDTHHMMSS`, not just
the date, so two faults on the same endpoint on the same day get separate branches
instead of stacking on one. Any uncommitted work in your tree carries over to the new
branch and is left alone; the agent stages only the files it edited.

If the tree was already dirty when the alert arrived, the prompt warns the agent
that `git add <file>` will also stage whatever else was already in that file. That is
worth watching for: git cannot stage part of a file, so a file that had unrelated
uncommitted edits will bring them along. The agent is asked to report anything
unrelated that got swept in.

The checkout stays on that branch afterwards. The incident records `git_branch` and
`commit_sha`, so you can review and merge it yourself:

```bash
git show <commit-sha>
git switch -        # back to where you were
git merge incident/<route>-<timestamp>
```

## Configuration

Every setting is an environment variable prefixed `INCIDENT_RESPONSE_`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `INCIDENT_RESPONSE_HOST` | `0.0.0.0` | Must include `0.0.0.0` for the container to reach it |
| `INCIDENT_RESPONSE_PORT` | `8001` | |
| `INCIDENT_RESPONSE_LOKI_URL` | `http://127.0.0.1:3100` | |
| `INCIDENT_RESPONSE_TEMPO_URL` | `http://127.0.0.1:3200` | |
| `INCIDENT_RESPONSE_PROMETHEUS_URL` | `http://127.0.0.1:9090` | |
| `INCIDENT_RESPONSE_SERVICE` | `order-tracker` | Grafana does not send `service_name`, so it comes from here |
| `INCIDENT_RESPONSE_APP_URL` | `http://127.0.0.1:8000` | The app under observation, given to the assistant to reproduce with |
| `INCIDENT_RESPONSE_REPO_ROOT` | the repository root | Where the assistant runs and commits |
| `INCIDENT_RESPONSE_DATA_DIR` | `incident-response/data` | |
| `INCIDENT_RESPONSE_OPENCODE_BIN` | `opencode` | |
| `INCIDENT_RESPONSE_OPENCODE_AGENT` | `incident-responder` | |
| `INCIDENT_RESPONSE_OPENCODE_MODEL` | unset | Pin a model, e.g. `anthropic/claude-sonnet-4-6` |
| `INCIDENT_RESPONSE_LOOKBACK_SECONDS` | `900` | History to pull back from the alert's start |
| `INCIDENT_RESPONSE_COOLDOWN_SECONDS` | `900` | Ignore a repeat firing for the same fingerprint |
| `INCIDENT_RESPONSE_AGENT_TIMEOUT_SECONDS` | `1800` | Hard stop on the assistant |
| `INCIDENT_RESPONSE_STALL_TIMEOUT_SECONDS` | `300` | Kill it if it stops producing output, which catches retry loops |
| `INCIDENT_RESPONSE_MAX_TRACES` | `3` | Traces fetched in full |
| `INCIDENT_RESPONSE_MAX_LOG_RECORDS` | `200` | Cap on saved log records |
| `INCIDENT_RESPONSE_DRY_RUN` | `0` | Capture evidence, never start the assistant |

## Caveats

- **`/alerts` is unauthenticated.** It binds `0.0.0.0`, so anything that can reach
  this host on port 8001 can make the service run git commands and start the coding
  assistant. It is meant for a local stack. Put it behind something else before it
  is reachable from anywhere you do not control.
- **The assistant commits unattended.** It is constrained by the permission
  allow-list and never pushes, but it does write commits to your repository. Review
  them.
- **Each investigation costs a model run.** The cooldown and the single worker bound
  that, but they do not make it free.
- **A dirty tree can taint the commit.** Git stages whole files, so unrelated
  uncommitted edits in a file the assistant touches ride along into its commit. The
  agent is told to warn you; review the diff before merging.
- Tempo traces arrive on the collector's export schedule, so the newest failing
  request may not be traceable yet when the alert fires. The prompt says which
  request it found, so the assistant can tell.
- **Tempo truncates `exception.stacktrace` at 2048 bytes.** A Python traceback prints
  the framework frames first, so the frame that actually raised is usually the one
  cut off. The prompt says so explicitly rather than letting the assistant trust a
  partial stack; the exception type and message are separate attributes and survive.
- **Losing a resolve leaves an incident open.** Nothing expires an incident on a
  timer. Grafana normally sends `status: resolved` and that closes it, but if the
  notification is dropped the incident stays `firing` until you resolve it by hand.