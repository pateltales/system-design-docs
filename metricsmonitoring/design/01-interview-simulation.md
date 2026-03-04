# System Design Interview Simulation: Design a Metrics & Monitoring System (Datadog / Prometheus / Grafana)

> **Interviewer:** Principal Engineer (L8), Infrastructure Observability Team
> **Candidate Level:** SDE-3 (L6 — Senior Software Development Engineer)
> **Duration:** ~60 minutes
> **Date:** February 20, 2026

---

## PHASE 1: Opening & Problem Statement (~2 min)

**Interviewer:**
Hey, welcome. I lead the observability platform team. For today's system design round, I'd like you to design a **metrics and monitoring system** — think Datadog or Prometheus + Grafana. Not just a metrics database — I'm talking about the full end-to-end platform: metric collection from thousands of hosts, time-series storage, a query engine, alerting that wakes up the right engineer at 3 AM, and dashboards that render in under 2 seconds.

I care about how you think about the write path at scale, compression, query performance, and the meta-problem of monitoring the monitoring system. I'll push on your choices — that's calibration, not criticism.

Take it away.

---

## PHASE 2: Requirements Gathering & Scoping (~5 min)

**Candidate:**
Thanks! A monitoring system touches every service in an organization, so let me scope this carefully before diving in.

**Functional Requirements — what operations do we need?**

> "Let me identify the core operations from the user's perspective:
>
> - **Collect Metrics** — Gather metric data points from applications, hosts, containers, and infrastructure. Support both push-based (agents push to backend) and pull-based (server scrapes /metrics endpoints) collection models.
> - **Store Time-Series Data** — Store billions of data points efficiently in a purpose-built TSDB. Support multiple metric types: counters, gauges, histograms, summaries.
> - **Query Metrics** — Expressive query language (like PromQL) to select, aggregate, and compute across time series. Support functions like rate(), histogram_quantile(), sum/avg/max grouping.
> - **Alert on Metrics** — Define alert rules (metric query + threshold + duration). Evaluate continuously. Notify via PagerDuty, Slack, email when thresholds are breached. Alert state machine: OK → PENDING → FIRING → RESOLVED.
> - **Dashboard Visualization** — Web UI with configurable panels (line charts, heatmaps, gauges, tables). Template variables for switching between environments/services. Auto-refresh.
>
> And on the platform side:
> - **Service Discovery** — Automatically discover new scrape targets in Kubernetes (pod annotations, service discovery).
> - **Downsampling & Retention** — Roll up raw data to coarser resolutions for long-term storage. Auto-delete expired data.
> - **Self-Monitoring** — The monitoring system must monitor itself. This is the meta-problem — who watches the watchmen?"

**Interviewer:** "Good breadth. Before you go further — are we designing a SaaS product like Datadog, or a self-hosted system like Prometheus?"

> "Great question. Let me design a **self-hosted, horizontally scalable system** — think Prometheus + Thanos/Mimir architecture — because it forces us to solve the hard distributed systems problems. But I'll call out at each decision point how Datadog's SaaS approach differs and why.
>
> One clarifying question: **should I cover logs and traces too?** Modern observability is three pillars — metrics, logs, traces."

**Interviewer:** "Focus on metrics. Mention correlation with logs and traces architecturally, but don't deep-dive them. They're fundamentally different workloads."

> "Agreed — metrics are structured, numeric, compact (~16 bytes per point). Logs are unstructured, text-heavy (~100-1000 bytes per line). They need different storage engines and query patterns. I'll stay focused on metrics."

**Non-Functional Requirements:**

> "The non-functional requirements are what make this problem hard:
>
> | Dimension | Requirement | Rationale |
> |---|---|---|
> | **Ingestion throughput** | 1-10M samples/second | 1,000 hosts × 5,000 series/host × 1 sample/10s = 500K-5M samples/sec |
> | **Ingestion latency** | < 30 seconds end-to-end | Alert evaluation needs fresh data. 30s lag is tolerable for most alerts |
> | **Active time series** | 10M-50M | Cardinality = product of all label value combinations. Grows fast with labels |
> | **Query latency** | < 2 seconds for dashboards | Dashboard with 20 panels, each executing a PromQL query in parallel |
> | **Alert evaluation** | Every 15-60 seconds, < 30s notification latency | Late alert = undetected outage = customer impact |
> | **Retention** | 15 days raw (10s), 90 days 1-min rollups, 1 year 1-hour rollups | Balance cost vs query ability for historical data |
> | **Compression** | ~1.37 bytes/point (Gorilla compression, ~12x ratio) | Time-series data is highly compressible |
> | **Availability** | 99.95% for alert evaluation, 99.5% for dashboards | Alert path must be more reliable than dashboard path |
> | **Self-monitoring** | Independent health checks outside the monitoring system | Can't alert about monitoring failure through the monitoring system |

**Interviewer:**
Good scoping. I like that you separated ingestion latency from query latency — those are very different paths. And the self-monitoring requirement is important — most candidates forget it. Let me ask: why did you call out Gorilla compression specifically?

**Candidate:**

> "Because compression is what makes time-series storage feasible at scale. Without Gorilla compression, 10M series at 10s intervals would require ~1.4 TB/day of raw storage (16 bytes per sample × 8,640 samples/day × 10M series). With Gorilla's delta-of-delta encoding for timestamps and XOR encoding for values, we get ~12x compression — down to ~120 GB/day. That's the difference between needing a massive storage cluster and fitting on a few SSDs. Gorilla compression was published by Facebook in 2015 and is used by Prometheus TSDB, InfluxDB, and Datadog internally."

**Interviewer:**
Perfect — that's quantitative reasoning with the right citations. Let's get into API design.

---

### L5/L6/L7 Rubric — Phase 2: Requirements

| Dimension | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Scope** | Lists functional requirements. Mentions scale vaguely ("millions of metrics") | Separates FR/NFR. Quantifies scale with back-of-envelope math. Calls out push vs pull | All of L6 + frames requirements as a product decision ("SaaS vs self-hosted changes everything") |
| **Non-functional** | Mentions latency and availability | Separates ingestion latency from query latency. Quantifies compression. Distinguishes alert path reliability from dashboard reliability | All of L6 + articulates the self-monitoring paradox. Frames retention as a cost/query trade-off |
| **Depth** | Knows metric types exist | Explains counter vs gauge vs histogram vs summary. Knows histograms are aggregatable, summaries aren't | All of L6 + mentions native histograms as future. Discusses cardinality as the #1 scaling risk |

---

## PHASE 3: API Design (~5 min)

**Candidate:**

> "I'll focus on the three most critical APIs — these cover the highest-throughput write path, the most latency-sensitive read path, and the most operationally critical alerting path. Full API reference in [02-api-contracts.md](02-api-contracts.md).

### API 1: Metrics Ingestion (Highest Throughput)

**Push-based ingestion:**

```
POST /v1/metrics
Content-Type: application/json
Authorization: Bearer <api_key>

{
  "series": [
    {
      "metric": "http_requests_total",
      "type": "counter",
      "points": [[1706000000, 1542387]],
      "tags": ["method:GET", "status:200", "service:api"]
    },
    {
      "metric": "system.cpu.usage",
      "type": "gauge",
      "points": [[1706000000, 72.5]],
      "tags": ["host:web-01", "region:us-east"]
    }
  ]
}

Response: 202 Accepted
  (Async — data will be processed, not immediately queryable.
   202, not 200, because ingestion is async through Kafka.)
```

**Pull-based collection — Prometheus exposition format:**

```
Application exposes:
  GET /metrics → text/plain

  # HELP http_requests_total Total HTTP requests
  # TYPE http_requests_total counter
  http_requests_total{method="GET",status="200"} 1542387
  http_requests_total{method="POST",status="200"} 456123

The monitoring server scrapes this endpoint every 10-15 seconds.
No API key needed (server initiates connection within trusted network).
```

> "Note the design difference: push returns 202 Accepted because the data goes through Kafka asynchronously. Pull doesn't have a response code — the monitoring server is the HTTP client. Prometheus chose pull because it gives the server control over scrape frequency and provides a free health signal — if the scrape fails, the target is down."

### API 2: Time-Series Query (Most Latency-Sensitive)

```
GET /api/v1/query_range
  ?query=sum(rate(http_requests_total{service="api"}[5m])) by (host)
  &start=2024-01-23T00:00:00Z
  &end=2024-01-23T01:00:00Z
  &step=60s

Response: 200 OK
{
  "status": "success",
  "data": {
    "resultType": "matrix",
    "result": [
      {
        "metric": {"host": "web-01"},
        "values": [[1706000000, "12.5"], [1706000060, "13.2"], ...]
      },
      {
        "metric": {"host": "web-02"},
        "values": [[1706000000, "8.7"], [1706000060, "9.1"], ...]
      }
    ]
  }
}
```

> "This is the Prometheus-compatible query API. The `query` parameter takes a PromQL expression. The `step` parameter controls evaluation granularity — Grafana computes this automatically based on panel pixel width (auto-resolution). A 500px panel showing 1 hour of data needs at most 500 data points, so step = 7 seconds."

### API 3: Alert Rule Management (Most Operationally Critical)

```
POST /api/v1/alerts/rules
{
  "name": "High Error Rate on API",
  "query": "sum(rate(http_requests_total{status=~\"5..\",service=\"api\"}[5m])) / sum(rate(http_requests_total{service=\"api\"}[5m])) * 100",
  "threshold": "> 5",
  "for": "3m",
  "severity": "critical",
  "labels": {"team": "platform", "service": "api"},
  "annotations": {
    "summary": "API error rate is {{ $value }}%",
    "runbook": "https://wiki.internal/runbooks/api-errors"
  },
  "notify": ["pagerduty:platform-oncall", "slack:#platform-alerts"]
}

Response: 201 Created
{
  "id": "alert-rule-123",
  "status": "active"
}
```

> "Every alert rule must link to a runbook — an alert without a runbook is useless at 3 AM. The `for` duration prevents transient spikes from triggering pages. The alert state machine is: OK → PENDING (threshold exceeded, timer started) → FIRING (exceeded for full `for` duration) → RESOLVED (back below threshold)."

**Interviewer:**
Good — you covered the three critical paths and explained the design rationale. I noticed you used PromQL in the query API. Why not SQL?

**Candidate:**

> "PromQL is 5-10x more concise for time-series queries. Computing a per-second rate in SQL requires LAG window functions and manual timestamp arithmetic. In PromQL, it's just `rate(counter[5m])`. PromQL also has first-class support for label matching, histogram quantiles, and range vectors — concepts that don't map cleanly to SQL. PromQL has become the de facto standard for the open-source metrics ecosystem — Thanos, Cortex/Mimir, Grafana, even Victoria Metrics all support it."

---

### L5/L6/L7 Rubric — Phase 3: API Design

| Dimension | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Ingestion** | Defines POST endpoint for metrics | Distinguishes push vs pull. Uses 202 for async ingestion. Shows Prometheus exposition format | All of L6 + discusses OTLP (OpenTelemetry) as the vendor-neutral protocol. Discusses batch vs single-point ingestion trade-offs |
| **Query** | Defines GET endpoint with metric name and time range | Uses PromQL. Explains auto-resolution (step based on panel width). Shows structured response format | All of L6 + discusses query cost estimation, fan-out queries, and how recording rules pre-compute expensive queries |
| **Alerting** | Defines threshold-based alert rule | Explains the full alert state machine (OK→PENDING→FIRING→RESOLVED). Requires runbook link. Explains "for" duration rationale | All of L6 + discusses alert-as-code (Terraform), inhibition (suppress symptoms when cause alerts), and anomaly detection as an alternative to static thresholds |

---

## PHASE 4: High-Level Architecture Evolution (~20 min)

### Attempt 0: Cron + Log Files + MySQL (The Naive Approach)

**Candidate:**

> "Let me start from the simplest possible monitoring setup and iteratively find problems to solve.
>
> **Attempt 0** — Applications write metrics to log files. A cron job runs every minute, parses the logs, and stores results in MySQL. A simple web page queries MySQL and shows the latest values. For alerting, the cron job checks thresholds and sends email."

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│ Application │────>│ Log File    │────>│ Cron Job    │
│ (writes to  │     │ (on disk)   │     │ (every 1m)  │
│  log file)  │     │             │     │ Parse logs  │
└─────────────┘     └─────────────┘     │ Store MySQL │
                                        │ Check thresh│
                                        │ Send email  │
                                        └──────┬──────┘
                                               │
                                               ▼
                                        ┌─────────────┐     ┌──────────┐
                                        │ MySQL       │────>│ Web Page │
                                        │ (metrics    │     │ (table   │
                                        │  table)     │     │  of nums)│
                                        └─────────────┘     └──────────┘
```

**Interviewer:**
This is a reasonable starting point. What breaks?

**Candidate:**

> "Almost everything:
>
> 1. **Parsing logs is fragile** — any log format change breaks the parser. No schema enforcement.
> 2. **MySQL is terrible for time-series writes** — millions of inserts/second need row locks, B-tree index updates, WAL + data page writes. MySQL was designed for transactional workloads, not append-only metric streams.
> 3. **1-minute granularity from cron** — can't detect a 30-second spike. No real-time visibility.
> 4. **No tagging/dimensions** — flat metric names (`cpu.web01`) can't be aggregated across hosts. Can't ask 'average CPU across all web servers.'
> 5. **No dashboarding** — just a table of numbers. No time-series charts.
> 6. **No query language** — can't aggregate, filter, or compute rates.
> 7. **Email alerting** — no severity levels, no escalation, no grouping (100 hosts with high CPU = 100 separate emails)."

---

### Attempt 1: Structured Metrics + Pull-Based Collection + Purpose-Built TSDB

**Candidate:**

> "Let me fix the foundational problems.
>
> **Key change**: Define a structured metric format `{name, value, timestamp, tags}`. Applications expose a `/metrics` HTTP endpoint in Prometheus exposition format. A collector scrapes these endpoints every 10-15 seconds. Store in a purpose-built TSDB — in-memory head block (recent 2 hours, Gorilla compressed) + WAL for durability + immutable on-disk blocks for historical data. Basic query API: select by metric name and tags, aggregate over time."

```
┌──────────────┐     ┌──────────────┐     ┌──────────────────────────┐
│ Application  │     │ Collector    │     │ Time-Series Database     │
│ (exposes     │<───>│ (scrapes     │────>│                          │
│  /metrics    │     │  every 10s)  │     │ ┌──────────────────────┐ │
│  endpoint)   │     │              │     │ │ Head Block (in-mem)  │ │
│              │     │ Service      │     │ │ • Gorilla compressed │ │
│ Prometheus   │     │ discovery    │     │ │ • Last ~2 hours      │ │
│ exposition   │     │ (k8s API)    │     │ └──────────┬───────────┘ │
│ format       │     └──────────────┘     │            │ flush       │
└──────────────┘                          │            ▼             │
                                          │ ┌──────────────────────┐ │
                                          │ │ On-disk blocks       │ │
                                          │ │ • Immutable          │ │
                                          │ │ • Indexed            │ │
                                          │ │ • Compacted          │ │
                                          │ └──────────────────────┘ │
                                          │                          │
                                          │ WAL (Write-Ahead Log)    │
                                          └──────────────────────────┘
```

> "**Why pull-based**: The collector controls scrape frequency (no flood risk), and scrape failure = target is down (free health signal). This is exactly the Prometheus model.
>
> **Why a purpose-built TSDB**: Gorilla compression gives us ~1.37 bytes per data point vs 16 bytes uncompressed — 12x savings. The WAL provides durability for in-memory data. Immutable on-disk blocks allow efficient deletion (drop a file, not delete rows from a B-tree).
>
> **Contrast with Attempt 0**: Structured metrics → no fragile parsing. TSDB → 100x better write throughput than MySQL. 10s scrape → real-time visibility. Tags → dimensional aggregation."

**Interviewer:**
Good evolution. You mentioned Gorilla compression — can you briefly explain the mechanism?

**Candidate:**

> "Sure. Two key techniques from Facebook's 2015 Gorilla paper:
>
> **Timestamps — delta-of-delta encoding**: Consecutive timestamps in a regular scrape have nearly constant deltas (e.g., 60, 60, 60 seconds). The delta-of-delta is usually 0. Encode 0 with a single bit. Small deltas (jitter) need a few bits. Result: timestamps compressed to ~1-2 bits per point.
>
> **Values — XOR encoding**: Consecutive float64 values (e.g., CPU 45.2%, 45.3%, 45.1%) have many identical bits when XORed. We only store the meaningful (changed) bits. Result: values compressed to ~1-2 bytes per point vs 8 bytes uncompressed.
>
> **Combined**: ~1.37 bytes per data point. Prometheus TSDB, InfluxDB TSM engine, and Datadog all use variants of this."

**Interviewer:**
What's still broken?

**Candidate:**

> "Several critical gaps:
> 1. **Single TSDB server** — limited to one machine's capacity (~10M active series). No horizontal scaling.
> 2. **No long-term retention** — disk fills up. No downsampling for historical queries.
> 3. **No HA** — if the TSDB server dies, monitoring is blind. Single point of failure.
> 4. **No alerting pipeline** — just basic threshold checks with no grouping, deduplication, or escalation.
> 5. **No dashboarding** — raw query API only. No visual exploration."

---

### Architecture Evolution Table — Attempts 0-1

| Dimension | Attempt 0 (Cron + MySQL) | Attempt 1 (Pull + TSDB) |
|---|---|---|
| Collection | Parse log files (fragile) | Structured /metrics endpoint (standardized) |
| Storage | MySQL (slow for time-series) | Purpose-built TSDB (Gorilla compression) |
| Granularity | 1 minute (cron interval) | 10-15 seconds (scrape interval) |
| Query | Raw SQL | Basic metric query API |
| Dimensions | Flat names | Label/tag-based (dimensional) |
| Alerting | Cron + email | None yet |
| Dashboarding | Table of numbers | None yet |

---

### Attempt 2: Alerting Pipeline + Dashboarding + Retention Policies

**Candidate:**

> "Now I need to add the user-facing features that make a monitoring system useful in production.
>
> **Alerting pipeline**: A separate alert evaluation service runs queries against the TSDB on a loop (every 15-60 seconds), compares results against thresholds, manages alert states (OK → PENDING → FIRING → RESOLVED), and sends notifications to configurable channels. Alert grouping — 500 hosts with the same problem → 1 notification, not 500. Deduplication — same alert from multiple evaluators → 1 page.
>
> **Dashboarding**: Web UI (think Grafana) with configurable panels. Each panel runs a PromQL query and renders a visualization. Template variables for switching between environments. Auto-resolution: select data granularity based on time range + panel width.
>
> **Retention and downsampling**: Raw (10s) → 15 days. 1-minute rollups → 90 days. 1-hour rollups → 1 year. Downsampling runs as a background process. Old data deleted by dropping entire time-range blocks (not row-by-row deletes)."

```
                          ┌──────────────────────────────────────┐
                          │         Dashboard (Grafana-like)     │
                          │  ┌────────┐ ┌────────┐ ┌────────┐   │
                          │  │ Panel  │ │ Panel  │ │ Panel  │   │
                          │  │ (line) │ │ (gauge)│ │ (table)│   │
                          │  └───┬────┘ └───┬────┘ └───┬────┘   │
                          │      │          │          │         │
                          └──────┼──────────┼──────────┼─────────┘
                                 │          │          │
                                 ▼          ▼          ▼
                          ┌──────────────────────────────────────┐
┌──────────────┐          │             Query Engine             │
│ Alert        │──query──>│  (PromQL evaluation)                 │
│ Evaluator    │          │                                      │
│              │          └──────────────┬───────────────────────┘
│ Rules:       │                         │
│ • query      │                         ▼
│ • threshold  │          ┌──────────────────────────────────────┐
│ • for dur.   │          │      Time-Series Database            │
│ • notify     │          │                                      │
└──────┬───────┘          │  Head Block ──> Disk Blocks          │
       │                  │  WAL           Compaction            │
       ▼                  │                Downsampling          │
┌──────────────┐          └──────────────────────────────────────┘
│ Alertmanager │
│              │
│ • Group      │
│ • Dedupe     │
│ • Route      │
│ • Silence    │
│ • Notify     │
└──────┬───────┘
       │
  ┌────┼────┐
  ▼    ▼    ▼
PD  Slack  Email
```

> "**Contrast with Prometheus ecosystem**: I've essentially recreated the Prometheus + Alertmanager + Grafana stack. This is where most teams start. But it's still single-server — one machine for all of ingestion, storage, querying, and alerting."

**Interviewer:**
Good. You mentioned auto-resolution for dashboards — explain that briefly.

**Candidate:**

> "A 500-pixel-wide panel showing 1 year of data doesn't need 10-second granularity — that's 31.5 million points per series. Instead, the query engine automatically selects 1-hour rollups and returns ~500 data points. The step size = max(scrape_interval, (end - start) / panel_width). Grafana computes this and sends it in the query_range request. This is critical — without auto-resolution, long-range dashboard queries would kill the TSDB."

**Interviewer:**
What's still broken?

**Candidate:**

> "The fundamental problem: this is still a **single-server TSDB**.
> 1. **Can't handle >10M active series** — limited by one machine's memory.
> 2. **No push-based collection** — can't monitor serverless, ephemeral containers, or anything behind a firewall.
> 3. **No distributed query** — queries only run on one TSDB node.
> 4. **No multi-tenancy** — can't serve multiple teams with resource isolation.
> 5. **Single point of failure** — one crash = monitoring is blind."

---

### L5/L6/L7 Rubric — Phase 4 (Attempts 0-2)

| Dimension | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Evolution** | Jumps to "use Prometheus" without building up | Starts from cron+MySQL, identifies concrete problems, evolves step by step. Shows the Prometheus architecture as a natural evolution | All of L6 + explains why each evolution happened historically (Facebook→Gorilla, Google→Borgmon→Prometheus) |
| **TSDB understanding** | "Use a time-series database" | Explains Gorilla compression, WAL, head block vs disk blocks, inverted index, compaction | All of L6 + discusses block layout (chunks, index, tombstones), compaction strategies, and out-of-order ingestion support |
| **Alerting** | "Add threshold alerts" | Designs alert state machine, explains "for" duration, requires runbooks, discusses grouping/dedup | All of L6 + discusses inhibition (suppress symptoms), alert-as-code, and the difference between symptom-based vs cause-based alerting |

---

### Attempt 3: Horizontal Scaling + Push Ingestion + Distributed Query

**Candidate:**

> "Now I need to break past the single-server limit. This is where the architecture becomes a distributed system.
>
> **Horizontal write scaling**: Shard the TSDB across multiple ingester nodes. Partition by consistent hash of (tenant_id, metric_name). Each sample written to 3 ingesters (replication factor 3, quorum writes). Add Kafka as an ingestion buffer — agents push to Kafka, consumers write to sharded ingesters.
>
> **Push-based ingestion**: Accept metrics via HTTP/gRPC from agents and OpenTelemetry Collectors. Kafka absorbs burst traffic and provides replayability. Agents do local aggregation (10-second windows) to reduce backend load by 10-100x.
>
> **Distributed query engine**: Query frontend receives the user's PromQL query → determines relevant shards → fans out sub-queries in parallel → merges results. Query splitting: divide a 30-day query into 30 one-day sub-queries, execute in parallel, 29 hit cache (historical data is immutable), 1 queries live data.
>
> **Multi-tenancy**: Tenant ID in every request. Per-tenant rate limits. Per-tenant cardinality limits. Data isolation."

```
┌──────────────┐     ┌──────────────┐     ┌──────────────────────────────┐
│  Agents /    │     │  Ingestion   │     │        Kafka                 │
│  OTel        │────>│  Gateway     │────>│  (ingestion buffer)          │
│  Collectors  │     │  (auth,      │     │  Partitioned by              │
│              │     │   validate,  │     │  hash(tenant, metric)        │
│  Push metrics│     │   rate limit)│     └──────────┬───────────────────┘
└──────────────┘     └──────────────┘                │
                                        ┌────────────┼────────────┐
                                        ▼            ▼            ▼
                                  ┌──────────┐ ┌──────────┐ ┌──────────┐
┌──────────────┐                  │Ingester 1│ │Ingester 2│ │Ingester 3│
│ Collector    │                  │(in-mem   │ │(in-mem   │ │(in-mem   │
│ (scrapes     │───directly──────>│ +WAL)    │ │ +WAL)    │ │ +WAL)    │
│  /metrics)   │  (pull model)    └────┬─────┘ └────┬─────┘ └────┬─────┘
└──────────────┘                       │            │            │
                                       ▼            ▼            ▼
                                  ┌────────────────────────────────────┐
                                  │      Object Storage (S3/GCS)      │
                                  │  Immutable TSDB blocks             │
                                  └────────────────────────────────────┘
                                                    ▲
                                                    │
┌──────────────┐    ┌──────────────┐    ┌───────────┴──────────┐
│  Dashboard   │───>│ Query        │───>│ Querier Pool          │
│  (Grafana)   │    │ Frontend     │    │ (fan-out to ingesters │
│              │    │ (split,cache,│    │  + store gateways,    │
│              │    │  rate limit) │    │  merge, deduplicate)  │
│              │    └──────────────┘    └───────────────────────┘
└──────────────┘
```

> "**This is essentially the Cortex/Grafana Mimir architecture.** Thanos takes a different approach — it keeps individual Prometheus instances and adds a sidecar that uploads blocks to S3, plus a global query layer on top. The Cortex/Mimir model replaces the Prometheus write/read path entirely with a distributed system. Thanos is simpler to adopt (keep existing Prometheus), Mimir is more scalable (unified distributed write path)."

**Interviewer:**
Why Kafka in front of the TSDB? Why not write directly?

**Candidate:**

> "Four reasons:
> 1. **Burst absorption** — if 10,000 agents reconnect after a network partition, Kafka absorbs the thundering herd. Without Kafka, the ingesters get overwhelmed.
> 2. **Data safety** — if an ingester crashes, the data is safe in Kafka. We replay from the last consumed offset. Without Kafka, data in flight is lost.
> 3. **Fan-out** — one Kafka topic can be consumed by the TSDB writer, a real-time alerting evaluator, and an analytics pipeline simultaneously.
> 4. **Replayability** — if we discover a storage bug, we can replay Kafka from an earlier offset and re-index."

**Interviewer:**
What's still broken?

**Candidate:**

> "We've solved scale but introduced new problems:
> 1. **No long-term storage** — ingester disks have limited capacity. Where do we store years of data cheaply?
> 2. **No downsampling at scale** — how do we produce rollups from data spread across many ingesters?
> 3. **No anomaly detection** — only static thresholds. Can't handle seasonal patterns.
> 4. **No correlation** — metrics exist in isolation. Can't click a metric spike and see related traces/logs.
> 5. **No self-monitoring** — the most ironic gap. If this system fails, who detects it?"

---

### Attempt 4: Object Storage + Anomaly Detection + Observability Correlation

**Candidate:**

> "**Long-term storage on object storage (S3/GCS)**: Ingesters hold the last 2-4 hours in memory. Every 2 hours, they flush immutable blocks to S3. Store Gateways serve queries against S3 data with index and chunk caching. This gives unlimited retention at ~$0.023/GB/month — orders of magnitude cheaper than SSD.
>
> **Downsampling at scale**: A background Compactor reads raw blocks from S3, produces downsampled versions (1-minute and 1-hour aggregates), and writes them back. Each rollup stores min, max, avg, sum, count — so queries like 'peak CPU in the last 6 months' can be answered from rollup data without scanning raw data. The query engine automatically selects the appropriate resolution based on the query time range.
>
> **Anomaly detection**: ML-based baseline learning for each metric — seasonal decomposition learns daily and weekly patterns. Instead of 'CPU > 90% → alert,' the system learns that 85% CPU at Monday 2pm is normal but 50% CPU at Saturday 3am is anomalous. This reduces alert fatigue for seasonal workloads. Datadog's Watchdog does this automatically across all metrics.
>
> **Observability correlation**: Link metrics → traces → logs. When an alert fires, show correlated trace spans and log entries. Shared context: trace_id, service name, host. OpenTelemetry provides a unified SDK for all three signals."

**Interviewer:**
How does the query engine know whether to hit ingesters or object storage?

**Candidate:**

> "Time-based routing. The query frontend knows:
> - **Last ~2 hours**: Query ingesters (data is in memory, fastest)
> - **2 hours to 15 days**: Query Store Gateways (data on S3, block-indexed)
> - **15 days+**: Query Store Gateways using downsampled blocks (1-min or 1-hour resolution)
>
> For a query spanning multiple tiers (e.g., last 7 days), the querier fans out to both ingesters AND store gateways in parallel, then merges and deduplicates the results. The deduplication is important because there's an overlap period — the most recent block on S3 overlaps with the oldest data in the ingester."

---

### Attempt 5: Production Hardening — Self-Monitoring + Multi-Region + Cardinality Management

**Candidate:**

> "The final attempt addresses production operations at scale.
>
> **Self-monitoring**: The monitoring system instruments itself with the same metrics (ingestion rate, query latency, alert evaluation time). But this creates a circular dependency — if the system fails, it can't alert about its own failure.
>
> Solution: Independent external health checks running OUTSIDE the monitoring system. A Lambda function writes a canary metric every 60 seconds, reads it back 30 seconds later, and verifies the round-trip. If the canary check fails, it sends an alert via Twilio SMS — not through the monitoring system's own Alertmanager. This is the 'who watches the watchmen' problem, and the answer is: a simpler, independent system watches the complex system.
>
> **Multi-region**: Deploy the full monitoring stack in each region. Regional ingesters store local data. Cross-region replication for DR. A global query layer can aggregate across regions for unified dashboards.
>
> **Cardinality management**: The #1 scaling risk. A single high-cardinality label (e.g., user_id with 10M values) can create billions of time series and OOM the entire system. Mitigations:
> - Cardinality tracking per metric (alert when a metric exceeds N unique series)
> - Rate limiting at ingestion (reject metrics that would create too many new series)
> - Metric relabeling at the collector (drop high-cardinality labels before they hit the backend)
> - Education (teach developers: user_id belongs in traces, not metrics)
>
> **Cost optimization**: Show teams their monitoring cost breakdown — which metrics consume the most storage, which create the most series. Let teams make informed decisions about what to monitor."

---

### Final Architecture

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                           MONITORING SYSTEM — FINAL ARCHITECTURE                │
│                                                                                 │
│  ┌───────────────────────────────────────────────────────────────────────────┐   │
│  │  COLLECTION TIER                                                        │   │
│  │                                                                         │   │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────────┐  ┌──────────────┐        │   │
│  │  │ Datadog  │  │  OTel    │  │ Prometheus   │  │ kube-state-  │        │   │
│  │  │ Agent    │  │ Collector│  │ scraping     │  │ metrics +    │        │   │
│  │  │ (push)   │  │ (push)   │  │ (pull)       │  │ cAdvisor     │        │   │
│  │  └────┬─────┘  └────┬─────┘  └──────┬───────┘  └──────┬───────┘        │   │
│  └───────┼──────────────┼───────────────┼─────────────────┼────────────────┘   │
│          │              │               │                 │                     │
│          ▼              ▼               │                 │                     │
│  ┌───────────────────────────┐          │                 │                     │
│  │  Ingestion Gateway        │          │                 │                     │
│  │  (auth, validate, route,  │          │                 │                     │
│  │   rate limit, cardinality)│          │                 │                     │
│  └───────────┬───────────────┘          │                 │                     │
│              ▼                          │                 │                     │
│  ┌───────────────────────────┐          │                 │                     │
│  │       KAFKA               │<─────────┘                 │                     │
│  │  (ingestion buffer)       │<───────────────────────────┘                     │
│  └───────────┬───────────────┘                                                  │
│              │                                                                  │
│  ┌───────────┼──────────────────────────────────────────────────────────────┐   │
│  │  STORAGE TIER                                                           │   │
│  │           │                                                             │   │
│  │  ┌───────┴────────┐                                                     │   │
│  │  │  Ingesters     │  (in-memory head block + WAL, Gorilla compression)  │   │
│  │  │  (3x replicas) │  Recent ~2 hours. Consistent hash ring.             │   │
│  │  └───────┬────────┘                                                     │   │
│  │          │ flush every ~2h                                              │   │
│  │          ▼                                                              │   │
│  │  ┌────────────────────┐                                                 │   │
│  │  │  Object Storage    │  (S3/GCS — immutable blocks, unlimited retention│   │
│  │  │  (S3 / GCS)        │   ~$0.023/GB/month, 11 nines durability)       │   │
│  │  └────────┬───────────┘                                                 │   │
│  │           │                                                             │   │
│  │  ┌────────┴───────────┐     ┌──────────────────────┐                    │   │
│  │  │  Store Gateways    │     │  Compactor            │                   │   │
│  │  │  (serve historical │     │  (merge blocks,       │                   │   │
│  │  │   data from S3,    │     │   downsample 5m/1h,   │                   │   │
│  │  │   index+chunk cache│     │   delete expired)     │                   │   │
│  │  └────────────────────┘     └──────────────────────┘                    │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
│                                                                                 │
│  ┌─────────────────────────────────────────────────────────────────────────┐    │
│  │  QUERY TIER                                                            │    │
│  │                                                                        │    │
│  │  ┌────────────────┐     ┌──────────────────┐                           │    │
│  │  │ Query Frontend │────>│  Querier Pool     │                          │    │
│  │  │ (split, cache, │     │  (fan-out to      │                          │    │
│  │  │  rate limit,   │     │   ingesters +     │                          │    │
│  │  │  fair schedule)│     │   store gateways, │                          │    │
│  │  └────────────────┘     │   merge, dedupe,  │                          │    │
│  │                         │   PromQL eval)    │                          │    │
│  │                         └──────────────────┘                           │    │
│  └─────────────────────────────────────────────────────────────────────────┘    │
│                                                                                 │
│  ┌─────────────────────────────────────────────────────────────────────────┐    │
│  │  ALERTING & VISUALIZATION TIER                                         │    │
│  │                                                                        │    │
│  │  ┌──────────────┐   ┌───────────────┐   ┌──────────────┐              │    │
│  │  │ Alert        │   │ Alertmanager  │   │ Grafana      │              │    │
│  │  │ Evaluator    │──>│ (group, dedup,│──>│ (dashboards, │              │    │
│  │  │ (rule eval   │   │  route, notify│   │  panels,     │              │    │
│  │  │  every 15-60s│   │  silence,     │   │  variables,  │              │    │
│  │  │  via PromQL) │   │  inhibit)     │   │  annotations)│              │    │
│  │  └──────────────┘   └───────┬───────┘   └──────────────┘              │    │
│  │                             │                                          │    │
│  │                     ┌───────┼────────┐                                 │    │
│  │                     ▼       ▼        ▼                                 │    │
│  │                  PagerDuty Slack   Email                               │    │
│  └─────────────────────────────────────────────────────────────────────────┘    │
│                                                                                 │
│  ┌─────────────────────────────────────────────────────────────────────────┐    │
│  │  SELF-MONITORING (independent, outside main system)                    │    │
│  │                                                                        │    │
│  │  ┌──────────────────┐     Alert via:                                   │    │
│  │  │ Lambda/CronJob   │     • Twilio SMS (not through Alertmanager)     │    │
│  │  │ • Write canary   │     • Direct PagerDuty API call                 │    │
│  │  │ • Read canary    │     • Separate email                            │    │
│  │  │ • Verify round   │                                                  │    │
│  │  │   trip            │     Dead man's switch:                          │    │
│  │  │ • Check alert    │     • Heartbeat to external service             │    │
│  │  │   pipeline       │     • Absence of heartbeat → SMS               │    │
│  │  └──────────────────┘                                                  │    │
│  └─────────────────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### Architecture Evolution Summary

| Dimension | Attempt 0 | Attempt 1 | Attempt 2 | Attempt 3 | Attempt 4 | Attempt 5 |
|---|---|---|---|---|---|---|
| **Collection** | Parse logs | Pull /metrics | Pull | Push + Pull + Kafka | Same | + cardinality mgmt |
| **Storage** | MySQL | Single TSDB | + retention/downsampling | Sharded ingesters | + S3 long-term | + multi-region |
| **Query** | SQL | Basic API | PromQL | Distributed fan-out | + auto-resolution | + cost tracking |
| **Alerting** | Cron + email | None | Alert state machine + Alertmanager | Same | + anomaly detection | Same |
| **Dashboard** | Table | None | Grafana-like | Same | Same | Same |
| **HA** | None | None | None | 3x replication | Same | + self-monitoring |
| **Scale** | ~100 metrics | ~10M series | Same | ~100M+ series | Same | Same |

---

### L5/L6/L7 Rubric — Phase 4 (Attempts 3-5)

| Dimension | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Scaling** | "Shard the database" | Explains consistent hashing to ingesters, Kafka as buffer (burst absorption, replayability), quorum writes. Compares Thanos vs Cortex/Mimir approaches | All of L6 + discusses hash ring management (join/leave), zone-aware replication, and the operational cost of distributed systems vs staying on single-node longer |
| **Storage tiers** | "Use S3 for old data" | Explains hot (in-memory) → warm (disk) → cold (S3) tiering. Store Gateways with caching. Compactor for downsampling | All of L6 + discusses block-level index caching, lazy loading strategies, and cost modeling ($/GB/month across tiers) |
| **Self-monitoring** | Doesn't mention it | Identifies the circular dependency. Proposes canary metric pattern with independent Lambda + Twilio SMS | All of L6 + designs a multi-layer monitoring strategy (self-monitoring + independent checks + dead man's switch). References Google's Monarch/Borgmon pattern |

---

## PHASE 5: Deep Dive — Time-Series Database Internals (~5 min)

**Interviewer:**
Let's go deep on the TSDB. You mentioned Gorilla compression and the head block. Walk me through what happens when a sample arrives at an ingester.

**Candidate:**

> "The write path inside an ingester:
>
> 1. **Deserialize** the incoming sample: {metric_name, labels, timestamp, value}
>
> 2. **Lookup series ID**: Compute series_key = metric_name + sorted(labels). Check the in-memory hash map. If it exists → get series_id. If new → create a new series, assign an ID, update the inverted index (label → series_id mapping).
>
> 3. **Append to chunk**: Each series has an active chunk in memory. Append the sample using Gorilla encoding — delta-of-delta for the timestamp, XOR for the value. A chunk holds ~120 samples (~2 hours at 1-minute intervals).
>
> 4. **Write to WAL**: Sequentially append to the Write-Ahead Log. This is synchronous — we don't acknowledge the write until it's in the WAL. If the process crashes, we replay the WAL to recover in-memory state.
>
> 5. **Periodically (every ~2 hours)**: Flush the head block into an immutable on-disk block containing: chunk files (compressed data), an index file (series → chunk offsets + inverted index), and metadata (time range, stats). Upload to S3."

**Interviewer:**
What about the inverted index? How does it enable fast queries?

**Candidate:**

> "The inverted index maps each label key-value pair to the set of series IDs that have that label:
>
> ```
> service='api'    → {series 5, 12, 42, 87}
> method='GET'     → {series 5, 42, 99, 153}
> status='200'     → {series 5, 42, 87, 201}
> ```
>
> A query like `http_requests_total{service='api', method='GET'}` intersects the posting lists:
> ```
> service='api' ∩ method='GET' = {series 5, 42}
> ```
>
> This is the same concept as a search engine's inverted index — Lucene uses the same structure for full-text search. The difference is that in a TSDB, the 'documents' are time series and the 'terms' are label key-value pairs."

See [03-time-series-database.md](03-time-series-database.md) for full deep dive.

---

## PHASE 6: Deep Dive — Collection & Ingestion Pipeline (~5 min)

**Interviewer:**
Walk me through the collection pipeline — how does a metric get from application code to being queryable in the TSDB?

**Candidate:**

> "End-to-end flow with latency budget:
>
> ```
> App emits metric                     t = 0s
> Agent receives (localhost UDP/TCP)   t = ~1ms
> Agent aggregates (10s window)        t = ~10s
> Agent flushes to ingestion gateway   t = ~10s
> Gateway validates + produces to Kafka t = ~50ms
> Kafka consumer reads                 t = ~100ms-1s
> Ingester writes to memory + WAL      t = ~10ms
>                                      ─────────
> Total: emitted → queryable           ~15-25 seconds
> ```
>
> The agent's local aggregation is crucial. Without it, a service handling 10,000 req/s emitting 5 metrics per request would generate 50,000 data points/second per host. With local aggregation in a 10-second window, that becomes 5 summary values every 10 seconds — a 10,000x reduction in backend traffic.
>
> In Kubernetes, the agent runs as a DaemonSet (one per node). Service discovery watches the Kubernetes API for pods with `prometheus.io/scrape: 'true'` annotations and auto-configures scrape targets."

**Interviewer:**
What happens if the ingestion gateway is overloaded?

**Candidate:**

> "Multiple defenses:
> - **Rate limiting**: Per-tenant rate limits. If tenant A is sending 2x their quota, reject with 429.
> - **Kafka buffering**: Even if the gateway is slow, it produces to Kafka quickly. Kafka absorbs the burst.
> - **Agent-side retry**: Agents retry with exponential backoff + jitter. The jitter prevents synchronized retries (thundering herd).
> - **Cardinality check**: If a batch would create 100K new time series, reject it at the gateway — this is a cardinality bomb, likely a misconfigured label."

See [04-collection-and-ingestion.md](04-collection-and-ingestion.md) for full deep dive.

---

## PHASE 7: Deep Dive — Query Engine & PromQL (~5 min)

**Interviewer:**
You mentioned fan-out queries. How does the distributed query engine work?

**Candidate:**

> "The query frontend is the entry point. It does three things before executing:
>
> 1. **Query splitting**: A 30-day query is split into 30 one-day sub-queries. Each can execute independently and be cached independently.
>
> 2. **Shard splitting**: For high-cardinality queries, the frontend splits by series hash — 16 shards means each querier handles ~1/16 of the series. Near-linear speedup.
>
> 3. **Cache check**: For each sub-query, check the result cache (Memcached). Historical sub-queries almost always hit cache because time-series data is immutable — yesterday's data will never change.
>
> Then the querier fans out to data sources:
> - **Ingesters** for recent data (last ~2 hours, in-memory)
> - **Store Gateways** for historical data (S3 blocks)
>
> The querier merges results, deduplicates (for replicated data), and evaluates the PromQL expression on the merged dataset."

**Interviewer:**
What about dangerous queries? A user writes `sum(metric) by (user_id)` with 10 million unique user IDs.

**Candidate:**

> "This would materialize 10 million time series in memory — instant OOM. Protection mechanisms:
>
> 1. **Max series limit**: Reject queries that would touch >100K series. The frontend estimates this from the inverted index before execution.
> 2. **Query cost estimation**: Estimate cost = series_count × time_range × function_complexity. Reject if above threshold.
> 3. **Query timeout**: Kill after 60 seconds.
> 4. **Per-tenant concurrency limit**: Max N concurrent queries per tenant — one bad query doesn't starve everyone else.
>
> The deeper lesson: user_id doesn't belong in metric labels. Metrics are for bounded dimensions (service, host, method, status). High-cardinality dimensions (user_id, request_id) belong in traces."

See [05-query-engine-and-language.md](05-query-engine-and-language.md) for full deep dive.

---

## PHASE 8: Deep Dive — Alerting Pipeline (~5 min)

**Interviewer:**
Let's talk about alerting. How do you handle alert fatigue?

**Candidate:**

> "Alert fatigue is the #1 operational risk in monitoring. Too many alerts → on-call ignores them → real alerts are missed.
>
> Key mitigations:
>
> **1. Symptom-based alerting, not cause-based**: Alert on 'error rate > 5%' (the symptom users see), not on 'CPU > 90%' (the potential cause). When CPU, memory, and disk I/O are all high simultaneously, cause-based alerting fires 3 alerts. Symptom-based fires 1 — 'error rate is high.'
>
> **2. Alert grouping**: Alertmanager groups 500 identical alerts (one per host) into a single notification: '500 hosts have high CPU.' Not 500 separate pages.
>
> **3. Inhibition**: If 'cluster_down' is firing, suppress all 'pod_unhealthy' alerts for that cluster. They're symptoms of the cluster being down — alerting on both is redundant.
>
> **4. The 'for' duration**: Transient spikes (lasting <3 minutes) stay in PENDING and auto-resolve without paging anyone. Only sustained problems trigger notifications.
>
> **5. The golden rule**: If an on-call engineer is paged and doesn't need to take immediate action, the alert is misconfigured. Every critical page should result in human intervention."

**Interviewer:**
What about anomaly detection vs static thresholds?

**Candidate:**

> "They serve different purposes and should be used together:
>
> **Static thresholds** for absolute limits: 'CPU > 95% is never acceptable, regardless of time of day.' Deterministic, predictable, no cold start.
>
> **ML anomaly detection** for relative deviations: 'Latency is 3x higher than usual for this time of day.' Handles seasonality — 85% CPU at Monday 2pm is normal, 50% CPU at Saturday 3am is anomalous. Datadog's Watchdog does this automatically.
>
> Trade-off: anomaly detection reduces false positives for seasonal patterns but can miss novel failure modes that don't look 'anomalous' to the model. Static thresholds are dumb but predictable; ML is smart but can be surprising."

See [06-alerting-pipeline.md](06-alerting-pipeline.md) for full deep dive.

---

### L5/L6/L7 Rubric — Phases 5-8

| Dimension | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **TSDB internals** | "Data is stored in a database" | Walks through write path: deserialize → lookup series → append chunk (Gorilla) → WAL → flush to blocks → S3. Explains inverted index for series selection | All of L6 + discusses compaction strategies, out-of-order ingestion, chunk encoding alternatives, and the trade-off between head block size and flush frequency |
| **Ingestion** | "Agent sends data to server" | Explains local aggregation (10,000x traffic reduction), agent buffering, Kafka as buffer (burst absorption + replayability), cardinality bomb protection | All of L6 + discusses OpenTelemetry Collector pipeline (Receivers→Processors→Exporters), agent vs gateway mode, and the cost model of local vs centralized aggregation |
| **Query engine** | "Query the database" | Explains fan-out + merge, query splitting (time + shard), caching (immutable historical data = high hit rate), max series protection | All of L6 + discusses query cost estimation before execution, recording rules for pre-computation, and the difference between PromQL's type system (instant vs range vectors) and SQL |
| **Alerting** | "Set thresholds and send alerts" | Explains alert fatigue, symptom vs cause alerting, grouping, inhibition, "for" duration. Compares static vs ML anomaly detection | All of L6 + designs alert-as-code workflow (Terraform), discusses inhibition trees, and proposes composite alerts with boolean logic |

---

## PHASE 9: Deep Dive — Scaling, HA & Self-Monitoring (~5 min)

**Interviewer:**
Let's talk about the meta-problem. How do you monitor the monitoring system?

**Candidate:**

> "This is the most important problem in monitoring system design, and most candidates don't think about it.
>
> **The circular dependency**: The monitoring system can't reliably alert about its own failure using its own alerting pipeline. If Kafka goes down, no new metrics are ingested, the TSDB has stale data, the alert evaluator sees no threshold breach, and no alert fires. Meanwhile, your production database is also down, but the monitoring system doesn't see it.
>
> **Three layers of defense:**
>
> **Layer 1: Self-monitoring** — The monitoring system instruments itself (ingestion rate, query latency, alert evaluation time). Catches individual component degradation. But blind to total system failure.
>
> **Layer 2: Independent health checks** — A Lambda function (separate infrastructure, separate cloud account ideally) writes a canary metric every 60 seconds, reads it back 30 seconds later, verifies the round trip. If the canary check fails → send alert via Twilio SMS, NOT through the monitoring system's Alertmanager. This is end-to-end — a failure at any point (agent → Kafka → ingester → TSDB → query engine) causes the canary to fail.
>
> **Layer 3: Dead man's switch** — The monitoring system sends a heartbeat to an external service every minute. If the external service doesn't receive the heartbeat for 5 minutes → it sends an SMS. This inverts the problem: instead of detecting failure (hard), detect absence of success (easy).
>
> Google uses the same principle — their complex system (Monarch) is monitored by a simpler system. Simple = fewer failure modes = more reliable."

**Interviewer:**
Good. What about multi-region?

**Candidate:**

> "Each region has its own full monitoring stack — ingesters, store gateways, queriers, alert evaluators. This ensures regional independence — if us-east-1 has an outage, eu-west-1's monitoring still works.
>
> Cross-region replication: TSDB blocks on S3 are replicated across regions. If a region's monitoring stack fails, another region can serve its historical data.
>
> A global query layer can aggregate across regions for unified dashboards (e.g., 'total requests across all regions'). But regional alerting stays regional — alerts for us-east services are evaluated in us-east, not cross-region."

See [08-scaling-and-reliability.md](08-scaling-and-reliability.md) and [10-metrics-for-monitoring-systems.md](10-metrics-for-monitoring-systems.md) for full deep dives.

---

## PHASE 10: Wrap-Up (~3 min)

**Interviewer:**
Good design. Last question: you're running this system in production. It's 2 AM. What keeps you up at night?

**Candidate:**

> "Three things scare me:
>
> **1. Cardinality bomb** — A developer pushes code that adds a user_id label to a high-throughput metric. Suddenly we go from 50K series to 50 billion. The ingesters OOM, the inverted index explodes, and the entire monitoring system falls over. Mitigation: per-metric cardinality limits at the ingestion gateway, real-time cardinality tracking dashboards, and education — but it only takes one bad deployment.
>
> **2. Silent alerting failure** — The alert evaluator is running but the Alertmanager can't deliver notifications (PagerDuty webhook misconfigured, Slack token expired). Alerts fire internally but never reach a human. Production goes down, nobody gets paged. Mitigation: notification delivery health metrics, regular alert pipeline testing (fire a test alert, verify delivery end-to-end), and the dead man's switch as the ultimate backstop.
>
> **3. Correlated failure** — The monitoring system shares infrastructure with the services it monitors. A Kubernetes cluster outage takes down both the application AND the monitoring system simultaneously. Exactly when you need monitoring most, it's gone. Mitigation: deploy monitoring on separate infrastructure, or at minimum a separate node pool with dedicated resources and higher scheduling priority."

**Interviewer:**
Those are exactly the right things to worry about. The cardinality bomb is the most common cause of monitoring system outages in my experience. Good awareness of the correlated failure problem — many teams learn this the hard way. Strong design overall. Thanks.

---

### L5/L6/L7 Rubric — Final (Wrap-Up)

| Dimension | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Self-monitoring** | Doesn't mention it | Three-layer defense: self-monitoring + canary + dead man's switch. Uses independent notification channel (Twilio, not Alertmanager) | All of L6 + references Google's Monarch/Borgmon. Proposes monitoring SLOs (alert evaluation 99.95%, dashboards 99.5%). Designs the operational playbook for monitoring system failure |
| **Production readiness** | "It should be reliable" | Identifies cardinality bombs, silent alerting failure, and correlated failures. Proposes specific mitigations for each | All of L6 + discusses cost optimization (show teams their monitoring cost), capacity planning (back-of-envelope sizing), and the organizational challenge of running a platform team |
| **Overall design** | Basic metrics collection and storage. No alerting pipeline. No scaling beyond single node | Complete system: collection (push+pull) → Kafka → sharded ingesters → S3 → distributed query → alerting → dashboards → self-monitoring. Compares Thanos vs Cortex/Mimir. Quantitative reasoning throughout | All of L6 + frames the entire design as a product decision. Discusses build vs buy trade-offs (when to use Datadog vs self-hosted). Proposes a migration path from simple to complex |

---

## Supporting Deep-Dive Documents

| # | Document | Topic |
|---|---|---|
| 1 | [01-interview-simulation.md](01-interview-simulation.md) | This file — main interview backbone |
| 2 | [02-api-contracts.md](02-api-contracts.md) | Full API reference (ingestion, query, alerting, dashboard, service discovery, metadata, admin) |
| 3 | [03-time-series-database.md](03-time-series-database.md) | TSDB internals — Gorilla compression, Prometheus TSDB, InfluxDB TSM, downsampling, cardinality |
| 4 | [04-collection-and-ingestion.md](04-collection-and-ingestion.md) | Push vs Pull, Datadog Agent, OpenTelemetry Collector, Kubernetes collection, Kafka pipeline |
| 5 | [05-query-engine-and-language.md](05-query-engine-and-language.md) | PromQL, query execution pipeline, fan-out queries, caching, query safety |
| 6 | [06-alerting-pipeline.md](06-alerting-pipeline.md) | Alert state machine, alert fatigue, anomaly detection, on-call integration |
| 7 | [07-dashboarding-and-visualization.md](07-dashboarding-and-visualization.md) | Grafana, panel types, template variables, auto-resolution, query caching |
| 8 | [08-scaling-and-reliability.md](08-scaling-and-reliability.md) | Write/read scaling, Thanos vs Cortex/Mimir, HA, multi-region, graceful degradation |
| 9 | [09-design-trade-offs.md](09-design-trade-offs.md) | Push vs Pull, single vs distributed TSDB, histogram vs summary, all-in-one vs best-of-breed |
| 10 | [10-metrics-for-monitoring-systems.md](10-metrics-for-monitoring-systems.md) | Self-monitoring, independent health checks, dead man's switch, monitoring SLOs |
