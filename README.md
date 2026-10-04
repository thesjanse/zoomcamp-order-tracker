# Order Tracker

A small order tracking app for the AI Dev Tools Zoomcamp observability homework. It includes a web page, API, tests, and a Docker Compose setup. You add telemetry, alerts, and an incident responder in Homework 4.

The main user flow is creating an order and checking its status. Three sample orders are created on first startup.

## Run it

You need Docker with Compose. To run the tests, you also need Python 3.11+ and `uv`.

```bash
docker compose up --build -d --wait
```

Open <http://127.0.0.1:8000>. The API is at `/api/orders`, and the health check is at `/healthz`. Data is stored in a Docker volume and survives container recreation.

If port 8000 is occupied, set `ORDER_TRACKER_PORT`, for example:

```bash
ORDER_TRACKER_PORT=18080 docker compose up --build -d --wait
```

Run tests with `uv run --frozen pytest -q`. Stop everything with `docker compose down`. Add `-v` only if you also want to delete the data volumes.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` | Web page |
| GET | `/healthz` | Database health check |
| GET | `/api/orders` | List orders |
| POST | `/api/orders` | Create an order |
| GET | `/api/orders/{id}` | Check an order |
| PATCH | `/api/orders/{id}` | Change an order status |

The app uses SQLite to keep setup small. Run one app container at a time. The course exercise is about detecting and handling an incident, not scaling the database.

## Telemetry

Order lookups (`GET /api/orders/{id}`) emit OpenTelemetry metrics, logs, and traces. Under Docker Compose all three go to the OpenTelemetry Collector, which forwards them to Prometheus, Loki, and Tempo. See [Observability stack](#observability-stack) for the UIs.

Generate some signals and watch them appear:

```bash
curl 127.0.0.1:8000/api/orders/standard-1001   # 200
curl 127.0.0.1:8000/api/orders/missing        # 404
curl 127.0.0.1:8000/api/orders/express-1002   # 500, see below
```

| Signal | Name | Notes |
| --- | --- | --- |
| Metric | `order.lookup.requests` | Counter of lookups |
| Metric | `order.lookup.duration` | Lookup latency in seconds |
| Log | `order lookup completed` | INFO for 2xx, WARN for 4xx, ERROR for 5xx |
| Span | `GET /api/orders/{order_id}` | Server span, ERROR status on 5xx |

Metrics and log records carry `http.route` (the route template, not the concrete path, to keep cardinality bounded), `http.request.method`, and `http.response.status_code`. Failures also carry `error.type`. Log records add `order.id` and `order.lookup.outcome` (`found`, `not_found`, or `error`), and each record links back to its span through `trace_id`, so you can follow one lookup across all three signals.

Telemetry is keyed off the matched HTTP route rather than the `get_order` handler, because `POST /api/orders` and `PATCH /api/orders/{id}` both call that handler internally and are not order lookups.

`ORDER_TRACKER_TELEMETRY` picks where the three signals go:

| Value | Behaviour |
| --- | --- |
| `otlp` | Send to the collector over OTLP. Compose sets this. |
| `console` | Write to stdout. The default when the variable is unset. |
| `none` | No exporters. The test suite uses this. |

To read telemetry straight out of the terminal instead, run the app locally in console mode:

```bash
ORDER_TRACKER_TELEMETRY=console uv run --frozen uvicorn app.main:app --reload
```

`OTEL_METRIC_EXPORT_INTERVAL` sets the metrics export interval in milliseconds (5000 by default), and `OTEL_EXPORTER_OTLP_ENDPOINT` overrides the collector address.

### Known issue

`GET /api/orders/express-1002` returns 500. Estimating delivery for an express order adds two days to the creation date without rolling over the month, so orders placed near the end of a month fail. That is deliberate: it is the incident the telemetry above is meant to surface.

## Observability stack

`docker compose up --build -d --wait` also starts an OpenTelemetry Collector, Prometheus, Loki, Tempo, and Grafana. The app only ever talks to the collector, so the backends can be changed without touching app code.

```
app ──OTLP──▶ otel-collector ──┬─ prometheus exporter :8889 ─▶ Prometheus ──┐
                               ├─ OTLP ─▶ Tempo                          ├─▶ Grafana
                               └─ OTLP ─▶ Loki                           ┘
```

| Service | URL | Purpose |
| --- | --- | --- |
| Grafana | <http://127.0.0.1:3000> | Datasources and the provisioned dashboard |
| Prometheus | <http://127.0.0.1:9090> | Metric storage and PromQL |
| Loki | <http://127.0.0.1:3100> | Log storage and LogQL |
| Tempo | <http://127.0.0.1:3200> | Trace storage and TraceQL |
| Collector OTLP | `127.0.0.1:4317` (gRPC), `127.0.0.1:4318` (HTTP) | Where other services send telemetry |
| Collector metrics | <http://127.0.0.1:8889/metrics> | App metrics as the collector exposes them |

Grafana has anonymous admin access, so there is no login. Every port is bound to `127.0.0.1`.

### The dashboard

Open Grafana and pick **Order Tracker — requests and errors**. It is provisioned from the repository, so it comes back after `docker compose down -v`.

It covers request counts (lookups, rate by status code, a route/method/status table) and errors (5xx total, error rate, failed lookups by route, and the same failures counted independently out of Tempo and Loki so you can compare the three signals against each other). Latency quantiles and a panel for collector export failures sit alongside, and the bottom row opens the failing log records and traces.

To fill it, run the curls from [Telemetry](#telemetry). Metrics show up at the next Prometheus scrape (15s), logs immediately, and Tempo needs roughly a minute after a cold start before its traces become searchable.

### The alert

One rule is provisioned in the *Order Tracker* alert folder: **5xx responses on an order lookup endpoint**. It fires when any endpoint returned a 5xx in the last 5 minutes:

```bash
curl -i 127.0.0.1:8000/api/orders/express-1002   # 500, alert fires within about a minute
```

The rule is evaluated every 30 seconds with no pending delay, so it fires within 30 to 60 seconds of the failing request (5s metric export, 15s Prometheus scrape, 30s evaluation) and clears itself 5 minutes after the last 5xx. Open Grafana's **Alerting → Alert rules** to watch it, or `GET /api/prometheus/grafana/api/v1/rules` for the state in a script. The incident clears on its own, so leave the tab open if you want to see the transition.

The alert text carries what you need to act on it: which endpoint failed, how many 5xx that endpoint has produced in total, the window and how often the rule looks at it, and a link to the *Server errors by route* panel with that endpoint pre-selected. Grouping by `http.route` gives one alert instance per endpoint, and grouping by the route template rather than the concrete path keeps it to a single instance for lookups.

Quiet periods are the normal case, not an error. Before an endpoint has ever returned a 5xx the query returns nothing at all, and afterwards it returns zero, and the rule sits at **Normal** in both cases: `noDataState: OK` keeps the empty result from being reported as NoData. The trade-off is that a stopped app or a broken telemetry pipeline is quiet in exactly the same way, which an `absent()` rule would be needed to catch.

The query takes some care, and the rule file explains why in full. `order.lookup.requests` is a cumulative counter that the app re-exports every 5 seconds for as long as it runs, so the obvious queries do not work: `increase()` reads 0 for a first 5xx, because the series is created already at 1, and `count_over_time()` never goes back to 0, because the counter is still being exported. The rule needs one branch for each of those cases.

Where the alert goes is [`incident-response/`](incident-response/README.md): a service on port 8001 that receives the webhook, saves the endpoint's logs, traces and metrics, and starts a coding assistant headless to fix the fault.

Editing `observability/grafana/provisioning/alerting/rules.yaml` needs `docker compose restart grafana`, and the rule comes back after `docker compose down -v` like the dashboard does.

## Incident response

[`incident-response/`](incident-response/README.md) is the responder for the alert above. It listens on port `8001`:

```bash
cd incident-response
uv sync --frozen
uv run --frozen uvicorn incident_response.main:app --host 0.0.0.0 --port 8001
```

It runs on the host rather than in Compose, so that it can start [opencode](https://opencode.ai) headless with your own credentials, against your own checkout.

When the alert fires, Grafana posts it to `/alerts` and the service:

- reads the affected endpoint out of the alert's `http_route` and `http_request_method` labels,
- pulls that endpoint's failing log records from Loki, follows the `trace_id` on them into Tempo, and gets the counters from Prometheus,
- saves all of it under `incident-response/data/incidents/<id>/`, along with the exact prompt it is about to hand the assistant,
- switches the checkout to an `incident/<route>-<id>` branch, runs `opencode run --agent incident-responder`, and lets it fix the fault, test it, and commit.

The correlation is the app's own: every failing log record carries the `trace_id` of the span that raised, and that trace carries the Python stacktrace naming the line at fault. Nothing in the responder reads Grafana after the webhook arrives.

The assistant runs unattended, so its permissions are an allow-list rather than `--auto`: it may edit files and run the test suite, and `git push`, `git reset`, `git clean`, `docker`, `curl` and `sudo` are denied outright by `.opencode/agent/incident-responder.md`. It commits to its own branch and never pushes.

```bash
curl -s 127.0.0.1:8001/incidents | python3 -m json.tool
curl -s 127.0.0.1:8001/incidents/<id>/evidence | python3 -m json.tool
```

Two things make it work from inside Compose:

- `observability/grafana/provisioning/alerting/incident-response.yaml` provisions the contact point **and** the notification policy tree. Both are needed: Grafana's default policy points at a receiver named `empty`, so a contact point on its own would still be silent.
- The `grafana` service has `extra_hosts: host.docker.internal:host-gateway`, which is how the container reaches the responder on the host. Without it the name does not resolve and every webhook fails.

`INCIDENT_RESPONSE_DRY_RUN=1` captures the evidence and saves it but never starts the assistant and never touches git, which is the quickest way to check the wiring. Its `README.md` has the details, the endpoint list, and the caveats.

### Configuration

Every config file is in `observability/` and mounted read-only:

| File | Configures |
| --- | --- |
| `otel-collector-config.yaml` | Receivers, processors, and the exporters for each signal |
| `prometheus/prometheus.yml` | Scrape jobs |
| `loki/loki-config.yaml` | Single-binary Loki with filesystem storage |
| `tempo/tempo.yaml` | Single-binary Tempo, plus the metrics-generator that derives span metrics and a service graph |
| `grafana/provisioning/` | Datasources, the dashboard file provider, the alert rules, and the alert contact point |
| `grafana/dashboards/` | The dashboard JSON |

Image tags and host ports have environment variable overrides, following the existing `ORDER_TRACKER_PORT` convention:

```bash
GRAFANA_PORT=13000 docker compose up --build -d --wait
```

Tempo's local storage and Loki's chunks are on named volumes. Tempo, Loki, and the collector images ship without a shell, so they have no container healthcheck; `docker compose up --wait` treats a running container as satisfied, and the collector retries exports while a backend finishes starting.

### Expected startup noise

Two log lines look alarming but are harmless in a single-process setup:

- Tempo logs `error calling scheduler ... no jobs found` every minute or so. Its backend worker polls for compaction jobs, and single-process Tempo never registers any. Traces are still ingested, stored, and searchable; only automatic block compaction is skipped, which does not matter at this scale.
- Loki can log `error getting ingester clients ... empty ring` while it starts up and again briefly if it is restarted on its own.

Check `/ready` on Tempo and `/api/health` on Grafana if you want a real signal instead of reading logs.
