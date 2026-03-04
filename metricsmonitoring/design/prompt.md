Design a Metrics & Monitoring System (like Datadog / Prometheus / Grafana) as a system design interview simulation.

## Template
Follow the EXACT same format as the Netflix interview simulation at:
src/hld/netflix/design/prompt.md

Same structure:
- Interviewer/Candidate dialogue format
- PHASE 1: Opening & Problem Statement
- PHASE 2: Requirements Gathering & Scoping (functional + non-functional + scale numbers)
- PHASE 3: API Design
- PHASE 4: High-Level Architecture (iterative build-up: Attempt 0 → 1 → 2 → ..., starting from single server, finding problems, evolving)
- PHASE 5+: Deep dives on each component
- L5/L6/L7 rubric table after EACH deep dive phase — showing exactly how an L5, L6, and L7 candidate would answer differently. This is critical for understanding what "good" looks like at each level.
- Architecture evolution table after each phase
- Wrap-up phase with "what keeps you up at night"
- Links to supporting deep-dive docs

## Candidate Level
**L6 SDE-3 (Senior SDE)** candidate being interviewed by a Principal Engineer (L8).

The candidate's answers should demonstrate L6-level depth:
- Not just "what" but "why" and "why not the alternative"
- Quantitative reasoning (back-of-envelope math, concrete numbers)
- Awareness of operational concerns (monitoring a monitoring system, failure modes, blast radius)
- Proactive identification of tradeoffs before the interviewer asks

The L5/L6/L7 rubric tables after each phase should make it crystal clear how an L5 answer differs from L6 differs from L7.

## Output location
Create all files under: src/hld/metrics/design/

## Files to create

### 1. 01-interview-simulation.md — the main backbone
The full interview dialogue covering all phases and attempts.

### 2. 02-api-contracts.md — Metrics Platform APIs

This doc should list all the major API surfaces of a metrics/monitoring platform. The interview simulation (Phase 3) will only cover a subset — this doc is the comprehensive reference.

**Structure**: For each API group, list every endpoint with HTTP method, path, request/response shape, and a brief description. Mark endpoints covered in the interview with a star or highlight.

**API groups to cover**:

- **Metrics Ingestion APIs**: The highest-throughput path. `POST /v1/metrics` (batch write of metric data points — each point is {metric_name, value, timestamp, tags}). Support both push-based (agents push metrics) and pull-based (server scrapes endpoints). Include the Prometheus exposition format (`GET /metrics` endpoint on instrumented services, returning metric families in text format). Discuss the OpenTelemetry OTLP protocol (gRPC and HTTP/protobuf for metrics, traces, logs). Batch ingestion for efficiency — don't send one data point per HTTP request.

- **Query APIs**: `GET /v1/query?metric=system.cpu.usage&tags=host:web-01&start=...&end=...&aggregation=avg&interval=60s` (time-series query — return a series of (timestamp, value) pairs). Support aggregation functions (avg, sum, min, max, count, percentiles P50/P95/P99). Support grouping by tag (`group_by=host`). Support arithmetic between series (`metric_a / metric_b`). Query language design: Datadog uses a proprietary query syntax, Prometheus uses PromQL, InfluxDB uses Flux/InfluxQL. PromQL is the most widely adopted open-source query language for metrics.

- **Alerting APIs**: `POST /v1/alerts/rules` (create an alert rule: metric query + threshold + duration + notification channels). `GET /v1/alerts/active` (list currently firing alerts). `POST /v1/alerts/{alertId}/acknowledge` (silence an alert). `POST /v1/alerts/{alertId}/resolve` (manually resolve). Alert rule definition: "If avg(system.cpu.usage){host:web-01} > 90 for 5 minutes → page on-call via PagerDuty." Alert states: OK → PENDING → FIRING → RESOLVED.

- **Dashboard APIs**: `POST /v1/dashboards` (create a dashboard with a layout of panels/widgets). `GET /v1/dashboards/{dashboardId}` (retrieve dashboard definition + render data). `PUT /v1/dashboards/{dashboardId}` (update layout, add/remove panels). Each panel contains a metric query, visualization type (line chart, bar chart, heatmap, gauge, table), and display options. Dashboard-as-code: dashboards defined in JSON/YAML, version-controlled.

- **Service Discovery / Target Management APIs** (for pull-based systems): `GET /v1/targets` (list all scrape targets and their health status). `POST /v1/targets` (register a scrape target: endpoint URL, scrape interval, labels). Integration with Kubernetes service discovery (watch pod/service changes, auto-register scrape targets). Integration with cloud provider APIs (EC2 instance discovery, ECS task discovery).

- **Metadata APIs**: `GET /v1/metrics/metadata` (list all known metric names with descriptions and types — counter, gauge, histogram, summary). `GET /v1/tags?metric=system.cpu.usage` (list all tag keys and values for a metric — used for dashboard autocomplete and query building). Cardinality management: track the number of unique time series per metric (cardinality = product of all tag value combinations).

- **Admin / Configuration APIs**: `POST /v1/retention` (set retention policies — e.g., raw data for 15 days, 1-minute rollups for 90 days, 1-hour rollups for 1 year). `POST /v1/downsampling` (configure rollup rules — aggregate raw data into coarser intervals). `GET /v1/health` (system health endpoint — the monitoring system must monitor itself).

**Contrast with Prometheus API**: Prometheus has a well-defined HTTP API (`/api/v1/query`, `/api/v1/query_range`, `/api/v1/series`, `/api/v1/labels`, `/api/v1/targets`) and the PromQL query language. Prometheus is pull-based (it scrapes targets) rather than push-based (agents push data). This design choice affects the API surface: Prometheus needs target management APIs (what to scrape, how often), while push-based systems like Datadog need ingestion APIs (how to receive data from agents). OpenTelemetry's OTLP protocol unifies metrics, traces, and logs into a single protocol — contrast with Prometheus (metrics only), Datadog (separate agents for metrics/traces/logs), and ELK (logs only).

**Interview subset**: In the interview (Phase 3), focus on: metrics ingestion (the highest-throughput path — millions of data points per second), time-series query (the most latency-sensitive read path), and alerting (the most operationally critical path — a missed alert = undetected outage). The full API list lives in this doc.

### 3. 03-time-series-database.md — Time-Series Storage Engine

The time-series database (TSDB) is the core of any metrics system. General-purpose databases (MySQL, PostgreSQL, MongoDB) are terrible for time-series data. This doc explains why and how purpose-built TSDBs solve the problem.

- **Why general-purpose DBs fail for metrics**:
  - Write pattern: millions of data points per second, always appending (no updates, no deletes). Row-based DBs struggle with this write volume — each insert acquires locks, updates indexes, writes WAL + data pages.
  - Read pattern: time-range scans across thousands of series ("give me CPU usage for all 500 web servers for the last 6 hours"). Row-based DBs scan rows one at a time; columnar storage is needed.
  - Compression: metric values are highly compressible (timestamps are monotonically increasing, values change slowly). General-purpose DBs don't exploit this — they store each value independently. Purpose-built TSDBs achieve 10-20x compression.
  - Retention: old data must be automatically deleted. In MySQL, deleting billions of rows causes lock contention and table fragmentation. TSDBs delete entire time-range blocks atomically (drop a file, not delete rows).

- **Time-series data model**:
  ```
  A time series is uniquely identified by:
    metric_name + sorted set of label/tag key-value pairs

  Example:
    http_requests_total{method="GET", status="200", service="api"}

  This is ONE time series.
  Change any label value → a DIFFERENT time series:
    http_requests_total{method="GET", status="404", service="api"}  ← different series
    http_requests_total{method="POST", status="200", service="api"} ← different series

  Cardinality = number of unique time series
  For this metric: cardinality = |methods| × |statuses| × |services|
  If you have 5 methods × 10 statuses × 100 services = 5,000 time series
  Add a "host" label with 1,000 hosts → 5,000,000 time series

  HIGH CARDINALITY is the #1 scaling challenge in metrics systems.
  ```

- **Metric types**:
  - **Counter**: Monotonically increasing value. Reset to 0 on process restart. Example: total HTTP requests. Rate is computed at query time: rate(http_requests_total[5m]).
  - **Gauge**: Value that goes up and down. Example: current CPU usage, memory usage, queue depth.
  - **Histogram**: Distribution of values. Pre-defined buckets. Example: request latency histogram with buckets at 10ms, 50ms, 100ms, 500ms, 1s. Client-side aggregation — each bucket is a counter.
  - **Summary**: Similar to histogram but computes quantiles (P50, P95, P99) client-side. Not aggregatable across instances (unlike histograms). Prometheus recommends histograms over summaries for this reason.

- **Gorilla compression (Facebook, 2015)** [VERIFIED — Facebook research paper "Gorilla: A Fast, Scalable, In-Memory Time Series Database"]:
  - **Timestamps**: Delta-of-delta encoding. Consecutive timestamps in a regular time series have nearly constant deltas (e.g., 60, 60, 60, 60 seconds). The delta-of-delta is usually 0. Encode the delta-of-delta with variable-length encoding: 0 = 1 bit, small delta = few bits. Result: timestamps compressed to ~1-2 bits per point.
  - **Values**: XOR encoding. Consecutive float64 values in a metric (e.g., CPU 45.2%, 45.3%, 45.1%) have many identical leading and trailing bits when XORed. Encode only the meaningful bits. Result: values compressed to ~1-2 bytes per point (vs 8 bytes uncompressed).
  - **Combined**: ~1.37 bytes per data point (vs 16 bytes uncompressed = timestamp(8) + value(8)). ~12x compression ratio.
  - Prometheus TSDB, InfluxDB TSM engine, and Datadog's internal TSDB all use variants of Gorilla compression.

- **Prometheus TSDB internals** [VERIFIED — Prometheus documentation and Fabian Reinartz's design doc]:
  - **Write-Ahead Log (WAL)**: All incoming samples are first written to a WAL for durability. WAL is a sequential append-only log. If the process crashes, replay the WAL to recover in-memory state.
  - **Head block (in-memory)**: Recent data (last ~2 hours) lives in memory. Organized as: series → chunks of compressed samples. Each chunk holds ~120 samples (2 hours at 1-minute intervals). Uses Gorilla compression.
  - **Persistent blocks**: Every 2 hours, the head block is compacted into an immutable block on disk. Each block contains: a chunk file (compressed time-series data), an index file (series → chunk offsets, label index for fast lookups), a metadata file (time range, stats). Blocks are immutable — no in-place updates.
  - **Compaction**: Over time, many small blocks accumulate. The compactor merges them into larger blocks (e.g., 2-hour blocks → 6-hour block → 24-hour block). Larger blocks have better compression and faster query performance (fewer files to scan). During compaction, tombstones (deleted series) are applied.
  - **Inverted index**: Maps label key-value pairs to the set of series IDs that have that label. Example: {service="api"} → {series_1, series_5, series_42}. Used for fast series selection in PromQL queries. Similar to an inverted index in a search engine.

- **InfluxDB TSM (Time-Structured Merge tree)** engine:
  - Similar to an LSM tree but optimized for time-series. Write path: WAL → in-memory cache → periodic flush to TSM files on disk. TSM files are sorted by (measurement, tag set, time). Compaction merges TSM files for better compression and query performance.
  - Retention policies: automatically delete data older than a configured duration. Implemented by dropping entire shards (time-partitioned) rather than deleting individual rows.
  - Continuous queries: automatically downsample data at configured intervals (e.g., compute 5-minute averages from 10-second raw data). Reduces storage for historical data while preserving query ability.

- **Downsampling / Rollups**:
  ```
  Raw data: every 10 seconds → 8,640 points per day per series
  After 15 days: roll up to 1-minute averages → 1,440 points per day
  After 90 days: roll up to 5-minute averages → 288 points per day
  After 1 year: roll up to 1-hour averages → 24 points per day

  For each rollup interval, store: min, max, avg, sum, count
  This allows queries like "what was the peak CPU in the last 6 months?"
  to be answered from rollup data without scanning raw data.

  Storage savings: 15 days of raw (10s) + 75 days of 1m + 275 days of 5m + years of 1h
  vs keeping raw data forever. Easily 10-50x storage reduction.
  ```

- **Contrast with Prometheus storage limitations**: Prometheus is designed as a single-node TSDB. It does NOT support clustering, replication, or long-term retention natively. For production at scale, you need Thanos (adds a global query layer + object storage backend + downsampling) or Cortex/Mimir (adds horizontal write scaling + long-term storage). Datadog handles all of this internally — users don't manage storage.

### 4. 04-collection-and-ingestion.md — Metrics Collection Pipeline

How metrics get from application code to the monitoring system.

- **Push vs Pull collection**:
  ```
  PUSH (Datadog, StatsD, OpenTelemetry, InfluxDB):
    Application/agent sends data TO the monitoring system.

    App → Agent (on same host) → Monitoring Backend
    or
    App → directly → Monitoring Backend

    Pros:
      • Works behind firewalls/NAT (agent pushes out)
      • Short-lived processes can push before exiting
        (batch jobs, Lambda functions, cron jobs)
      • No need for service discovery (agent knows where to send)
      • Application controls the send frequency

    Cons:
      • Monitoring system can be overwhelmed (no backpressure from server)
      • Need to trust the agent (what if it sends garbage data?)
      • Each application must be instrumented with an agent/SDK

  PULL (Prometheus, Nagios):
    Monitoring system scrapes data FROM the application.

    Monitoring Server → scrapes → App's /metrics endpoint

    Pros:
      • Monitoring system controls scrape rate (no flood risk)
      • Can detect if a target is DOWN (scrape fails = target unhealthy)
      • No agent needed on the target (just expose an HTTP endpoint)
      • Simpler security model (monitoring server initiates connections)

    Cons:
      • Requires service discovery (server must know what to scrape)
      • Doesn't work behind firewalls/NAT (server can't reach target)
      • Short-lived processes may exit before being scraped
      • All targets must expose an HTTP endpoint (not always feasible)

  Industry trend: OpenTelemetry supports BOTH push and pull.
  Prometheus added push gateway for short-lived jobs.
  Datadog agent pulls /metrics endpoints AND receives pushed StatsD data.
  The distinction is blurring — modern systems support both models.
  ```

- **Agent architecture (Datadog Agent as reference)**:
  - Runs on every host (VM, container, bare-metal).
  - Collects: system metrics (CPU, memory, disk, network), process metrics, container metrics (Docker/Kubernetes), application metrics (via integrations or custom checks).
  - Local aggregation: the agent pre-aggregates metrics locally before sending to the backend. Example: instead of sending every individual request latency, compute a local histogram and send the histogram to the backend every 10-15 seconds. This reduces network traffic by 10-100x.
  - Buffering: if the backend is temporarily unreachable, the agent buffers data locally and retries. Prevents data loss during brief network outages.
  - Configuration: YAML-based. Define which integrations to enable, which custom metrics to collect, which tags to apply.

- **OpenTelemetry Collector architecture** [VERIFIED — OpenTelemetry documentation]:
  ```
  ┌──────────────┐     ┌──────────────────────────────────────────┐
  │ Application  │────>│  OpenTelemetry Collector                 │
  │ (instrumented│     │                                          │
  │  with OTel   │     │  Receivers:                              │
  │  SDK)        │     │    • OTLP (gRPC/HTTP) — native OTel      │
  │              │     │    • Prometheus (scrape /metrics)         │
  │              │     │    • StatsD (receive StatsD UDP packets)  │
  │              │     │    • Jaeger (receive Jaeger traces)       │
  │              │     │                                          │
  │              │     │  Processors:                              │
  │              │     │    • Batch (group data for efficiency)    │
  │              │     │    • Filter (drop unwanted metrics)       │
  │              │     │    • Transform (rename, relabel, enrich)  │
  │              │     │    • Tail sampling (for traces)           │
  │              │     │                                          │
  │              │     │  Exporters:                               │
  │              │     │    • Datadog                              │
  │              │     │    • Prometheus Remote Write              │
  │              │     │    • OTLP (to another collector or backend)│
  │              │     │    • Jaeger, Zipkin, etc.                 │
  └──────────────┘     └──────────────────────────────────────────┘

  The Collector is vendor-neutral: instrument once with OTel SDK,
  send to any backend by changing the exporter configuration.
  This is the key value proposition of OpenTelemetry.
  ```

- **Kubernetes-native collection**:
  - **DaemonSet**: Deploy agent as a DaemonSet (one pod per node). Agent collects node-level and pod-level metrics.
  - **Sidecar**: Deploy collector as a sidecar container alongside the application pod. Higher isolation but more resource overhead.
  - **Service discovery**: Agent watches Kubernetes API for pod/service changes. Auto-discovers new scrape targets when pods are created/destroyed. Labels from Kubernetes metadata (namespace, deployment, pod name) are automatically added as metric tags.
  - **kube-state-metrics**: A dedicated exporter that exposes Kubernetes object state as Prometheus metrics (deployment replicas, pod status, node conditions).
  - **cAdvisor**: Container-level resource usage metrics (CPU, memory, filesystem, network per container). Built into kubelet.

- **Contrast with log collection**: Metrics are structured, numeric, and compact (~16 bytes per data point). Logs are unstructured, text-heavy, and verbose (~100-1000 bytes per log line). Metrics need a TSDB; logs need a search engine (Elasticsearch, Loki). Metrics have fixed dimensions (tags); logs have arbitrary fields. Metrics are pre-aggregated (the counter already counts); logs need post-ingestion aggregation ("count log lines matching pattern X"). The monitoring system should support both, but the storage and query engines are fundamentally different.

### 5. 05-query-engine-and-language.md — Query Engine, PromQL & Aggregation

How users query metrics — the query language, execution engine, and aggregation pipeline.

- **PromQL (Prometheus Query Language)** [VERIFIED — Prometheus documentation]:
  ```
  Instant vector selectors:
    http_requests_total{method="GET", status="200"}
    → Returns the latest value of matching series

  Range vector selectors:
    http_requests_total{method="GET"}[5m]
    → Returns the last 5 minutes of samples for matching series

  Functions:
    rate(http_requests_total[5m])
    → Per-second rate of increase over 5 minutes
    → Essential for counters (raw counter values are useless — rate is what matters)

    histogram_quantile(0.95, rate(http_request_duration_seconds_bucket[5m]))
    → P95 latency from a histogram

  Aggregation:
    sum(rate(http_requests_total[5m])) by (service)
    → Total request rate per service

    avg(node_cpu_usage) by (instance)
    → Average CPU per instance

  Binary operators:
    (rate(http_requests_total{status=~"5.."}[5m])
     / rate(http_requests_total[5m])) * 100
    → Error rate as a percentage

  PromQL is the de facto standard for metrics querying in the
  open-source ecosystem. Datadog, Grafana, Thanos, Cortex all
  support PromQL or PromQL-compatible query languages.
  ```

- **Query execution pipeline**:
  ```
  User query: avg(rate(http_requests_total{service="api"}[5m])) by (host)

  Step 1: SERIES SELECTION
    Use the inverted index to find all series matching:
      metric_name = "http_requests_total" AND service = "api"
    Result: series_ids = [42, 87, 153, 201, ...]

  Step 2: FETCH SAMPLES
    For each series_id, fetch samples from [now-5m, now]:
      Scan in-memory head block (recent data)
      If needed, scan on-disk blocks (older data)
    Result: per-series sample arrays [(t1, v1), (t2, v2), ...]

  Step 3: APPLY FUNCTION — rate()
    For each series, compute the per-second rate from the samples.
    rate = (last_value - first_value) / (last_timestamp - first_timestamp)
    Handle counter resets (value decreases = process restart).

  Step 4: APPLY AGGREGATION — avg() by (host)
    Group series by the "host" label.
    For each group, compute the average of the rate values.

  Step 5: RETURN RESULT
    Return the aggregated time series:
      {host="web-01"} → [(t1, avg_rate_1), (t2, avg_rate_2), ...]
      {host="web-02"} → [(t1, avg_rate_1), (t2, avg_rate_2), ...]
  ```

- **Query performance challenges**:
  - **High cardinality queries**: `sum(metric) by (user_id)` with millions of unique user IDs → materializes millions of series in memory. Can OOM the query engine. Solution: cardinality limits (reject queries that would touch >100K series), query cost estimation before execution.
  - **Long time ranges**: "Show me CPU for the last 6 months" → scans billions of data points. Solution: use rollup/downsampled data for long ranges. Automatically select the appropriate resolution based on the query time range and the display resolution (no need for 10-second granularity on a 6-month chart with 500 pixels).
  - **Fan-out queries in distributed systems**: If data is sharded across 100 storage nodes, a single query may need to fan out to all 100 nodes, wait for all responses, and merge. Solution: parallel fan-out with timeout, partial results on failure (show what we have, indicate which shards timed out).

- **Contrast: PromQL vs Datadog Query Language vs SQL**:
  | Aspect | PromQL | Datadog Query | SQL (for metrics) |
  |---|---|---|---|
  | Type safety | Strongly typed (instant vs range vectors) | Loosely typed | Standard SQL types |
  | Aggregation | Built-in (sum, avg, min, max, count, quantile) | Built-in (similar) | GROUP BY + aggregate functions |
  | Rate computation | rate(), irate() — first-class | .as_rate() — method chaining | Manual (lag functions, window) |
  | Joins | Limited (label matching only) | Limited | Full JOIN support |
  | Learning curve | Moderate (unique syntax) | Low (intuitive chaining) | Low (widely known) |
  | Adoption | De facto standard (open-source) | Proprietary (Datadog users) | Universal but verbose for metrics |

### 6. 06-alerting-pipeline.md — Alerting, Anomaly Detection & On-Call

The alerting pipeline is the most operationally critical part of the monitoring system — a missed alert = an undetected outage.

- **Alert rule evaluation**:
  ```
  Alert rule:
    name: "High CPU on API servers"
    query: avg(system.cpu.usage){service:api} > 90
    for: 5 minutes
    severity: critical
    notify: pagerduty-oncall, slack-#ops

  Evaluation loop (runs every 15-60 seconds):
    1. Execute the metric query: avg(system.cpu.usage){service:api}
    2. Compare result against threshold: > 90?
    3. If threshold exceeded:
       a. First time? → State: OK → PENDING. Start the "for" timer.
       b. Still exceeding after 5 minutes? → State: PENDING → FIRING.
          Send notification to configured channels.
       c. Still firing? → Continue sending (with configurable repeat interval).
    4. If threshold no longer exceeded:
       a. State: FIRING → RESOLVED. Send resolution notification.

  Alert states:
    OK → PENDING → FIRING → RESOLVED
                ↓
           (threshold no longer exceeded
            before "for" duration → back to OK)
  ```

- **Alert fatigue**: The #1 operational problem with monitoring. Too many alerts → on-call engineer ignores them → real alerts are missed.
  - Causes: too many noisy/non-actionable alerts, redundant alerts (CPU high AND memory high AND disk I/O high — all caused by one thing), alerts on symptoms not causes, thresholds too sensitive.
  - Mitigations: alert severity levels (critical = page, warning = Slack, info = log), alert grouping (group related alerts into one notification), alert deduplication (same alert from multiple sources → one notification), alert snooze/mute (suppress during known maintenance), runbooks (each alert has a linked runbook explaining what to do).

- **Anomaly detection (ML-based alerting)**:
  - Static thresholds are brittle: CPU > 90% is fine for a constant workload, but what about a workload with daily/weekly seasonality? Thursday at 2 PM CPU is always 85% — that's normal. Saturday at 3 AM CPU is usually 20% — if it hits 50%, that's anomalous.
  - ML anomaly detection: learn the normal pattern (seasonality, trend) for each metric. Alert when the actual value deviates significantly from the predicted value. Algorithms: STL decomposition (seasonal-trend decomposition), Prophet (Facebook's forecasting model), DBSCAN (density-based clustering for outlier detection), z-score on residuals.
  - Datadog Watchdog [VERIFIED — Datadog product documentation]: automatically detects anomalies across all metrics without user-defined rules. Uses ML to learn baselines and flag deviations.
  - Trade-off: anomaly detection reduces false positives (no alerting on expected patterns) but can miss novel failure modes that don't look "anomalous" to the model. Static thresholds are dumb but predictable; ML alerting is smart but can be surprising.

- **On-call integration**:
  - PagerDuty / OpsGenie / VictorOps integration: monitoring system sends alerts to on-call platform → on-call platform routes to the right person based on schedule, escalation policies, and severity.
  - Escalation: if on-call doesn't acknowledge within 5 minutes → escalate to backup → escalate to manager.
  - Incident management lifecycle: Alert fires → Page on-call → Acknowledge → Investigate → Mitigate → Resolve → Post-mortem.

- **Contrast: Prometheus Alertmanager vs Datadog Monitors vs CloudWatch Alarms**:
  | Aspect | Prometheus Alertmanager | Datadog Monitors | CloudWatch Alarms |
  |---|---|---|---|
  | Alert evaluation | Prometheus evaluates rules, sends to Alertmanager | Datadog backend evaluates | CloudWatch evaluates |
  | Grouping | Sophisticated (group by labels, wait, group_interval) | Basic (multi-alert grouping) | None (each alarm independent) |
  | Routing | Label-based routing to different receivers | Tag-based routing | SNS topics |
  | Silencing | Time-based, matcher-based | Downtime scheduling | — |
  | Deduplication | Built-in (by alert fingerprint) | Built-in | — |
  | Anomaly detection | Not built-in (external tools) | Watchdog (ML-based, built-in) | Anomaly detection alarms |
  | Self-hosted? | Yes (open-source) | No (SaaS only) | No (AWS managed) |

### 7. 07-dashboarding-and-visualization.md — Dashboards, Panels & User Experience

Dashboards are how engineers interact with the monitoring system daily. They must render complex queries over millions of data points in under 2 seconds.

- **Dashboard architecture**:
  - Dashboard = layout of panels. Each panel = a metric query + visualization type + display options.
  - Panel types: time-series line chart (most common), bar chart, heatmap (for distributions), gauge (for current values), single stat (big number), table, topology map, log stream.
  - Dashboard rendering: on page load, all panels execute their queries in parallel. Results are rendered as soon as they arrive (progressive rendering). Queries share a common time range (the dashboard's global time picker).

- **Query optimization for dashboards**:
  - Dashboards are the heaviest read workload — opening one dashboard may trigger 20-30 concurrent queries.
  - Auto-resolution: the query engine automatically selects the appropriate data resolution based on the time range and the panel's pixel width. A 500px-wide chart showing 1 year of data doesn't need 10-second granularity — 1-hour rollups are sufficient. This reduces the number of data points fetched by 360x.
  - Caching: dashboard query results are cached with a short TTL (30-60 seconds). Refreshing the dashboard within the TTL serves from cache. Cache key = query hash + time range.
  - Pre-computation: for "golden signal" dashboards (the most viewed), pre-compute the query results on a schedule rather than on demand.

- **Grafana** [VERIFIED — Grafana documentation and open-source project]:
  - The most widely used open-source dashboarding tool.
  - Data source plugins: connects to Prometheus, Datadog, InfluxDB, Elasticsearch, CloudWatch, MySQL, PostgreSQL, and 100+ other data sources.
  - Unified alerting: Grafana 8+ includes its own alerting engine (previously relied on data source alerting like Prometheus Alertmanager).
  - Grafana-as-code: dashboards defined in JSON, provisioned via YAML or Terraform.
  - Grafana Cloud: managed SaaS offering with Prometheus (Mimir), Loki (logs), Tempo (traces) backends.

- **Template variables**: Dashboard variables that allow users to switch context without editing queries. Example: a dropdown for "environment" (prod, staging, dev) that updates all panels. Variables are resolved at query time — the same dashboard works for any environment.

- **Contrast: Grafana vs Datadog Dashboards vs CloudWatch Dashboards**:
  | Aspect | Grafana | Datadog | CloudWatch |
  |---|---|---|---|
  | Data sources | 100+ plugins (any TSDB) | Datadog backend only | AWS services only |
  | Customizability | Highly customizable (open-source, plugins) | Opinionated but polished | Limited |
  | Learning curve | Moderate (many options) | Low (guided setup) | Low (AWS-native) |
  | Cost | Free (self-hosted) or Grafana Cloud | Per-host + per-custom-metric pricing | Per-dashboard + per-metric pricing |
  | Dashboard-as-code | JSON + YAML provisioning | Terraform provider | CloudFormation |
  | Collaboration | Annotations, shared dashboards | Notebooks, shared dashboards | — |

### 8. 08-scaling-and-reliability.md — Scaling, Sharding & High Availability

A metrics system must scale to millions of time series and billions of data points per day while maintaining query latency <2 seconds.

- **Scale numbers**:
  - **Datadog**: processes trillions of data points per day, monitors millions of infrastructure components for 28,000+ customers [PARTIALLY VERIFIED — Datadog investor presentations and product marketing].
  - **Prometheus single instance**: handles ~1-10 million active time series and ~100K-1M samples ingested per second [VERIFIED — Prometheus documentation and community benchmarks]. Beyond this, needs federation or Thanos/Cortex/Mimir.
  - **Typical microservice**: emits 100-500 unique metrics (with different tag combinations, this can be 1,000-50,000 time series per instance).
  - **1,000-server deployment**: at 5,000 time series per host × 1 sample per 10 seconds = 500,000 samples/second, ~50 million unique time series (with churn from container restarts).
  - **Netflix Atlas**: ingests 1+ billion metrics per minute [VERIFIED — Netflix Tech Blog]. One of the largest internal metrics systems.

- **Write path scaling**:
  - The write path must handle millions of samples per second without blocking.
  - Partitioning strategy: shard by metric name hash, or by tenant (multi-tenant systems), or by time range. Datadog likely shards by a combination of customer + metric + time.
  - Write buffering: incoming data is buffered in memory (and WAL for durability), flushed to persistent storage in batches. Batch writes are 10-100x more efficient than individual writes.
  - Kafka as the ingestion buffer: put Kafka in front of the storage layer. Agents push to Kafka → Kafka consumers write to TSDB. Kafka absorbs burst traffic, provides replayability, and decouples ingestion from storage.

- **Read path scaling**:
  - The read path must return query results in <2 seconds for dashboard queries.
  - Query fan-out: if data is sharded across N storage nodes, the query layer fans out to all relevant nodes, collects results, and merges. Use parallel fan-out with timeout (return partial results if a shard is slow).
  - Caching: hot metrics (the ones on popular dashboards) are cached in memory. Cold metrics are served from disk. The Pareto principle applies — 80% of queries hit 20% of metrics.

- **Thanos architecture** (scaling Prometheus) [VERIFIED — Thanos project documentation]:
  ```
  ┌────────────────┐   ┌────────────────┐
  │ Prometheus #1  │   │ Prometheus #2  │   (multiple Prometheus instances,
  │ (US-East)      │   │ (EU-West)      │    each scraping a subset of targets)
  │                │   │                │
  │ Thanos Sidecar │   │ Thanos Sidecar │   ← Uploads blocks to object storage
  └───────┬────────┘   └───────┬────────┘
          │                    │
          ▼                    ▼
  ┌──────────────────────────────────────┐
  │   Object Storage (S3 / GCS)          │   ← Long-term, durable, cheap storage
  │   Stores Prometheus TSDB blocks      │
  └──────────────────────────────────────┘
          ↑                    ↑
  ┌───────┴────────┐   ┌──────┴─────────┐
  │ Thanos Store   │   │ Thanos Store   │   ← Serves queries from object storage
  │ Gateway        │   │ Gateway        │
  └───────┬────────┘   └───────┬────────┘
          │                    │
          ▼                    ▼
  ┌──────────────────────────────────────┐
  │   Thanos Query                       │   ← Global query layer
  │   (fans out to Sidecars + Stores)    │      Deduplicates overlapping data
  │   (PromQL-compatible API)            │      Provides a single query endpoint
  └──────────────────────────────────────┘
          ↓
  ┌──────────────────────────────────────┐
  │   Thanos Compactor                   │   ← Compacts + downsamples blocks
  │   (runs against object storage)      │      in object storage
  └──────────────────────────────────────┘
  ```
  Thanos solves: long-term storage (unlimited retention via object storage), global query (query across multiple Prometheus instances), high availability (deduplicate data from HA pairs), downsampling (reduce resolution for old data).

- **Cortex / Grafana Mimir** (alternative to Thanos):
  - Horizontally scalable, multi-tenant Prometheus backend.
  - Ingestion path: write replicas receive samples via Prometheus remote write → store in chunks in object storage (S3/GCS).
  - Query path: query frontend splits large queries into smaller sub-queries → parallel execution → merge results.
  - Multi-tenancy: each tenant's data is isolated. Tenant ID in every request.
  - Used by Grafana Cloud as the metrics backend.

- **High availability**:
  - Monitoring systems must monitor themselves. If the monitoring system goes down, you lose visibility into all other systems.
  - HA strategy: run multiple replicas of every component. Prometheus: run 2 instances scraping the same targets (HA pair, deduplicate at query time). Alertmanager: cluster of 3+ instances with gossip protocol for deduplication. Storage: replicated across availability zones.
  - The meta-problem: "Who watches the watchmen?" If your monitoring system is down, how do you know? Solution: external synthetic checks (a simple cron job that pings the monitoring API and alerts via a separate, independent channel — e.g., SMS via Twilio, not through the monitoring system).

- **Contrast: Datadog (SaaS) vs Prometheus + Thanos (self-hosted) vs CloudWatch (managed)**:
  | Aspect | Datadog | Prometheus + Thanos | CloudWatch |
  |---|---|---|---|
  | Operational burden | None (SaaS) | High (operate Prometheus, Thanos, object storage) | Low (AWS managed) |
  | Cost at scale | Expensive ($15-23/host/month + custom metrics) | Cheap (open-source, pay for compute + storage) | Moderate (per-metric + per-alarm pricing) |
  | Multi-cloud | Yes | Yes | AWS only |
  | Retention | 15 months (standard) | Unlimited (object storage) | 15 months (standard), longer with Metric Streams |
  | Query language | Datadog query syntax | PromQL | Metrics Insights (SQL-like) |
  | Features | All-in-one (metrics, traces, logs, APM, security) | Metrics only (add Loki for logs, Tempo for traces) | Integrated with AWS services |

### 9. 09-design-trade-offs.md — Design Philosophy & Trade-off Analysis

Opinionated analysis of design choices — not just "what" but "why this and not that."

- **Push vs Pull collection**: Push is better for ephemeral workloads (Lambda, batch jobs, containers). Pull is better for static infrastructure (VMs, bare-metal) where the monitoring server controls scrape frequency. At scale, push requires flow control (agents can overwhelm the backend); pull requires robust service discovery. Prometheus chose pull because it simplifies the server (no ingestion rate limiting needed) and provides a free health signal (scrape failure = target is down). Datadog chose push because its SaaS model requires agents to push through firewalls/NAT to the Datadog cloud.

- **Single-writer TSDB (Prometheus) vs distributed TSDB (Cortex/Mimir, Datadog)**: Prometheus TSDB is single-writer — one instance owns all data. Simpler, faster (no consensus, no distributed coordination), but limited to one machine's capacity (~10M active series). Distributed TSDBs (Cortex, Mimir, Datadog's internal TSDB) shard data across many nodes — unlimited scale, HA, but adds distributed systems complexity (consistency, replication, query fan-out, compaction coordination). For most teams: start with single Prometheus, move to Thanos/Mimir when you outgrow it.

- **Tags/labels as first-class dimensions vs flat metric names**: Prometheus uses labels ({method="GET", status="200"}) — flexible, enables powerful aggregation, but causes cardinality explosion if misused (labeling by user_id = millions of series). StatsD uses flat metric names (http.requests.get.200) — no cardinality risk, but inflexible (can't aggregate across dimensions). The industry has converged on label-based metrics (Prometheus model) because the aggregation power outweighs the cardinality risk (which is mitigated by cardinality limits and education).

- **Histogram vs Summary for latency tracking**: Histograms (pre-defined buckets, counted client-side) are aggregatable — you can merge histograms from multiple instances and compute quantiles on the merged result. Summaries (quantiles computed client-side) are NOT aggregatable — you can't merge P99 values from multiple instances to get the global P99 (that's mathematically invalid). Prometheus documentation recommends histograms for multi-instance services. Trade-off: histograms require choosing bucket boundaries upfront; summaries give exact quantiles per instance.

- **Gorilla (in-memory) vs LSM/TSM (disk-based) storage**: Gorilla keeps all data in memory — lowest latency, highest cost, limited retention (26 hours at Facebook). LSM/TSM (Prometheus TSDB, InfluxDB) uses a combination of in-memory (recent data) and disk (historical data) — lower cost, longer retention, slightly higher latency for old data. Datadog uses a hybrid: in-memory for recent data + custom disk-based TSDB for historical data. For most systems: in-memory for the last 2-4 hours (hot path), disk for the rest (warm/cold path).

- **All-in-one (Datadog) vs best-of-breed (Prometheus + Grafana + Loki + Tempo + Alertmanager)**: All-in-one platforms (Datadog, New Relic) provide a unified experience — metrics, traces, logs, APM, security in one tool. Easy to set up, hard to leave (vendor lock-in). Best-of-breed (open-source stack) gives flexibility and avoids lock-in but requires operating 5+ tools. For startups and small teams: all-in-one is the right choice (operational simplicity). For large engineering orgs: best-of-breed can be cheaper and more customizable, but requires a dedicated platform team.

- **Metrics vs Logs vs Traces (the three pillars of observability)**: Metrics tell you WHAT is broken (CPU is high, error rate is up). Logs tell you WHY it's broken (stack trace, error message). Traces tell you WHERE in a distributed system it's broken (which service in the call chain is slow). All three are needed. Modern systems (Datadog, Grafana Cloud, New Relic) correlate them: click on an anomalous metric → see correlated logs → see correlated traces. OpenTelemetry unifies the instrumentation SDK for all three signals.

- **Cardinality management — the #1 operational challenge**: High cardinality labels (user_id, request_id, email) create millions of unique time series. Each series consumes memory, storage, and query resources. At 10M series per Prometheus instance, adding a label with 1000 values doubles the series count to potentially 10B — instant OOM. Solutions: cardinality limits (reject metrics above a threshold), metric relabeling (drop high-cardinality labels at ingestion), education (teach developers which labels are appropriate for metrics vs traces).

### 10. 10-metrics-for-monitoring-systems.md — Monitoring the Monitoring System (Meta-Problem)

The most ironic and important problem in system design: how do you monitor your monitoring system?

- **Self-monitoring requirements**: Ingestion rate (data points/second), ingestion lag (time between sample creation and storage), query latency (P50/P95/P99), query error rate, storage utilization, alerting latency (time between metric crossing threshold and notification sent), alert delivery success rate.
- **Independent health checks**: A health check that runs OUTSIDE the monitoring system (simple script, separate cloud function, external service like Pingdom) that verifies the monitoring system is alive and responsive.
- **Multi-layer alerting**: Use the monitoring system itself for most alerts. Use an independent system (SMS via Twilio, PagerDuty direct integration, simple cron + curl) for critical alerts about the monitoring system's own health. Never have a single point of failure where the alerting system can't alert about its own failure.
- **Contrast: Google's Monarch**: Google's internal monitoring system processes billions of time series. Google uses a separate, simpler monitoring system (Borgmon, predecessor to Prometheus) as a backstop for Monarch's health. Even Google doesn't trust a single monitoring system to monitor itself.

## CRITICAL: The design must reference real monitoring systems

Datadog is the reference for the commercial/SaaS approach. Prometheus is the reference for the open-source approach. The design should explain how both work and when to choose which. Where other systems (InfluxDB, CloudWatch, New Relic, Grafana, Thanos, Cortex/Mimir, OpenTelemetry) made different design choices, call those out explicitly as contrasts.

## CRITICAL: Iterative build-up (DO NOT skip this)
The architecture MUST evolve iteratively. Each attempt builds on the previous one by identifying a concrete problem and solving it. The candidate should NOT jump to the final architecture. The progression should feel natural:

### Attempt 0: Single server with cron + log files
- Applications write metrics to log files. A cron job parses log files every minute and stores results in MySQL. A simple web page shows the latest values. Alerts: cron job checks thresholds and sends email.
- **Problems found**: Parsing logs is fragile (format changes break the parser), MySQL is too slow for time-series writes at scale, no real-time updates (1-minute granularity from cron), no tagging/dimensions (flat metric names), no dashboarding (just a table of numbers), no query language (can't aggregate across hosts).

### Attempt 1: Structured metrics + pull-based collection + basic TSDB
- Define a structured metric format: `{name, value, timestamp, tags}`. Applications expose a `/metrics` HTTP endpoint (like Prometheus). A collector scrapes endpoints every 10-15 seconds. Store in a purpose-built TSDB (in-memory + WAL for recent data, append-only files on disk for historical data). Basic query API: select metrics by name and tags, aggregate over time.
- **Contrast with cron-based**: Pull-based collection gives the server control over scrape frequency, provides a free health signal (scrape failure = target down), and works with structured metric format (no log parsing).
- **Problems found**: Single TSDB server — limited to one machine's capacity (~10M series). No horizontal scaling. No long-term retention (disk fills up). No HA (if the server dies, monitoring is blind). No alerting pipeline (just basic threshold checks). No dashboarding (just raw query API).

### Attempt 2: Alerting pipeline + dashboarding + retention policies
- **Alerting pipeline**: Separate alert evaluation service. Runs queries against the TSDB on a loop (every 15-60 seconds). Compares results against thresholds. Manages alert states (OK → PENDING → FIRING → RESOLVED). Sends notifications to configurable channels (email, Slack, PagerDuty). Alert grouping and deduplication (don't send 500 emails for 500 hosts with the same problem).
- **Dashboarding**: Web UI with configurable panels. Each panel runs a metric query and renders a visualization (line chart, bar, gauge). Template variables for switching between hosts/environments. Auto-resolution: select data granularity based on time range + panel pixel width.
- **Retention policies**: Define how long to keep data at each resolution. Raw (10s) → 15 days. 1-minute rollups → 90 days. 1-hour rollups → 1 year. Downsampling runs as a background process. Old data is deleted by dropping entire time-range blocks (not row-by-row deletes).
- **Contrast with Prometheus**: Prometheus Alertmanager handles grouping, deduplication, silencing, and routing — all of which are non-trivial to build. Grafana provides dashboarding. This attempt re-creates the Prometheus + Alertmanager + Grafana stack.
- **Problems found**: Still single-server TSDB — can't handle >10M series. No push-based collection (can't monitor serverless/ephemeral workloads). No distributed query (queries only run on one TSDB node). No multi-tenancy (can't serve multiple teams with isolation).

### Attempt 3: Horizontal scaling + push ingestion + distributed query
- **Horizontal write scaling**: Shard the TSDB across multiple nodes. Partition by: metric name hash (distributes load evenly) or tenant ID (for multi-tenancy). Use consistent hashing to route writes to the correct shard. Replication factor 3 for durability.
- **Push-based ingestion**: Accept pushed metrics via HTTP/gRPC (from agents, OpenTelemetry Collectors, StatsD). Add Kafka as an ingestion buffer — agents push to Kafka → consumers write to sharded TSDB. Kafka absorbs bursts and provides replayability.
- **Distributed query engine**: Query frontend receives the user's query → determines which shards contain relevant data → fans out sub-queries to those shards in parallel → merges results → returns to user. Similar to Thanos Query or Cortex query frontend.
- **Multi-tenancy**: Tenant ID attached to every metric. Data isolation per tenant. Per-tenant rate limits and cardinality limits.
- **Contrast with Thanos**: Thanos achieves horizontal scaling by keeping multiple independent Prometheus instances and adding a global query layer on top. Cortex/Mimir achieves it with a unified distributed write and read path. This attempt follows the Cortex/Mimir model.
- **Problems found**: No long-term storage (sharded TSDB nodes have limited disk). Operational complexity of managing the distributed TSDB cluster. No anomaly detection (only static thresholds). No correlation with logs and traces. No self-monitoring (what if the monitoring system itself fails?).

### Attempt 4: Object storage + anomaly detection + observability correlation
- **Long-term storage on object storage (S3/GCS)**: Recent data (last 2-4 hours) in memory on TSDB nodes. Older data flushed to object storage (S3) as immutable compressed blocks. Query engine transparently queries both in-memory and object storage data. Unlimited retention at low cost.
- **Downsampling at scale**: Background compactor reads raw blocks from object storage → produces downsampled blocks (1-minute, 5-minute, 1-hour aggregates) → writes back to object storage. Query engine automatically selects the appropriate resolution.
- **Anomaly detection**: ML-based baseline learning for each metric (seasonal decomposition, trend analysis). Automatic anomaly alerts without user-defined thresholds. Reduces alert fatigue by learning normal patterns.
- **Observability correlation**: Link metrics → logs → traces. When an alert fires, show correlated log entries and trace spans. Shared context: trace_id, service name, host. This requires a unified data model across metrics, logs, and traces — OpenTelemetry provides this.
- **Contrast with Datadog**: Datadog provides all of this out of the box (SaaS). Building it yourself requires significant engineering investment. Datadog's advantage is the all-in-one experience; the open-source stack's advantage is cost and flexibility.
- **Problems found**: Operational complexity is very high. Need to manage: TSDB cluster, Kafka, object storage, query engine, alert evaluator, anomaly detection service, dashboard service, log storage, trace storage. A dedicated platform engineering team is required.

### Attempt 5: Production hardening + self-monitoring + multi-region
- **Self-monitoring**: The monitoring system monitors itself. Separate, independent health checks (external synthetic monitors) verify the monitoring system is alive. Critical alerts about the monitoring system are sent via a separate channel (not through the monitoring system itself).
- **Multi-region**: Deploy monitoring infrastructure in each region. Regional TSDB clusters store local data. Global query layer can query across regions for a unified view. Cross-region replication for HA (if a region's monitoring fails, another region can serve its data).
- **Cardinality management**: Automated cardinality tracking per metric. Warn/block metrics that exceed cardinality limits. Metric relabeling at ingestion to drop high-cardinality labels.
- **Access control and audit**: Role-based access to dashboards and alert rules. Audit log for all changes (who modified which alert rule, when). Critical for compliance in regulated industries.
- **Cost optimization**: Show teams their monitoring cost breakdown (which metrics are most expensive — high cardinality, high ingestion rate). Enable teams to make informed decisions about what to monitor.
- **Contrast with cloud-native monitoring**: CloudWatch, Azure Monitor, and GCP Cloud Monitoring are tightly integrated with their respective clouds. They provide monitoring with zero operational overhead but are limited to one cloud provider. For multi-cloud or hybrid deployments, a self-hosted or SaaS (Datadog) solution is needed.

## CRITICAL: Accuracy requirements

I DO NOT TRUST AI-GENERATED FACTS ABOUT SPECIFIC SYSTEMS. Every concrete claim about Datadog, Prometheus, or other monitoring system internals must be verifiable against official sources. Specifically:

1. **Use WebSearch and WebFetch tools** to look up Datadog Engineering Blog, Prometheus documentation, Grafana Labs blog, OpenTelemetry documentation, and other official sources BEFORE writing. Search for:
   - "Datadog engineering blog architecture"
   - "Datadog scale metrics per second data points"
   - "Prometheus TSDB design document Fabian Reinartz"
   - "Prometheus TSDB internals chunks compaction"
   - "Gorilla time series compression Facebook paper"
   - "OpenTelemetry Collector architecture"
   - "OpenTelemetry OTLP protocol specification"
   - "Thanos architecture components sidecar store query"
   - "Cortex Mimir architecture distributed Prometheus"
   - "InfluxDB TSM engine internals"
   - "Netflix Atlas metrics scale billion per minute"
   - "Grafana data source plugins architecture"
   - "time series database design interview"
   - "push vs pull metrics collection trade-offs"
   - "Datadog Watchdog anomaly detection"
   - "Prometheus cardinality limits best practices"
   - "metrics monitoring system design at scale"

   **BLANKET PERMISSION**: You have full permission to use WebSearch and WebFetch to read any online documentation, blog posts, or research papers. Do NOT ask the user for permission to read — just read. This applies to prometheus.io, datadoghq.com, grafana.com, opentelemetry.io, and any other reference source. Read as many pages as needed to verify facts.

2. **For every concrete number** (data points per day, series limits, scrape intervals, compression ratios), verify against official documentation or engineering blog posts. If you cannot verify a number, explicitly write "[UNVERIFIED — check official docs]" next to it.

3. **For every claim about system internals** (TSDB structure, compression algorithm, query execution), if it's not from an official source, mark it as "[INFERRED — not officially documented]".

4. **CRITICAL: Do NOT confuse Prometheus with Datadog.** These are different systems with different architectures:
   - Prometheus: pull-based, single-node TSDB, open-source, self-hosted, PromQL, Alertmanager for notifications
   - Datadog: push-based (agent), distributed TSDB (proprietary), SaaS, custom query language, integrated APM/logs/traces
   - When discussing design decisions, ALWAYS explain WHY each system chose its approach and how the alternative reflects a different operational model.

5. **CRITICAL: Do NOT confuse metrics with logs.** Metrics are structured, numeric, compact (~16 bytes/point). Logs are unstructured, text-heavy, verbose (~100-1000 bytes/line). They need different storage engines, different query languages, and different retention strategies. The monitoring system should support both, but acknowledge they are fundamentally different workloads.

## Key monitoring system topics to cover

### Requirements & Scale
- Ingest millions of metric data points per second from thousands of hosts/containers
- Store trillions of data points with configurable retention (15 days raw, 1 year rollups)
- Query latency <2 seconds for dashboard rendering (fan-out across shards, merge results)
- Alert evaluation every 15-60 seconds with <30-second notification latency
- Support 10M+ active time series with Gorilla compression (~1.37 bytes/point)
- Cardinality management (prevent high-cardinality label explosions)
- Self-monitoring (the monitoring system must monitor itself)

### Architecture deep dives (create separate docs as listed in "Files to create" above)

### Design evolution (iterative build-up — the most important part)
- Attempt 0: Cron + log parsing + MySQL + email alerts
- Attempt 1: Structured metrics + pull-based collection + single-node TSDB
- Attempt 2: Alerting pipeline + dashboarding + retention/downsampling
- Attempt 3: Horizontal scaling (sharded TSDB) + push ingestion (Kafka) + distributed query + multi-tenancy
- Attempt 4: Object storage for long-term + anomaly detection + observability correlation (metrics + logs + traces)
- Attempt 5: Self-monitoring + multi-region + cardinality management + cost optimization

Each attempt MUST:
1. Start by identifying concrete problems with the previous attempt
2. Propose a solution
3. Explain WHY this approach (and why not the alternative — mention Prometheus, Datadog, or other systems where relevant)
4. End with "what's still broken?" to motivate the next attempt

### Consistency & Data
- Write-heavy workload: append-only, no updates, no deletes (except retention-based bulk deletes)
- Gorilla compression for timestamps (delta-of-delta) and values (XOR encoding) → ~12x compression
- WAL for durability (write-ahead log before in-memory storage)
- Immutable blocks on disk (compacted, indexed, compressed)
- Eventual consistency acceptable for metrics queries (a few seconds of lag is fine)
- Strong consistency needed for alert state (an alert must fire exactly once, not zero or twice)
- Downsampling for long-term retention (raw → 1-minute → 5-minute → 1-hour rollups)
- Object storage (S3/GCS) for unlimited, cheap, durable long-term storage

## What NOT to do
- Do NOT treat this as "just a database" — it's a full observability platform with collection, storage, querying, alerting, dashboarding, and anomaly detection. Frame it accordingly.
- Do NOT confuse Prometheus with Datadog. Highlight differences at every layer, don't blur them.
- Do NOT confuse metrics with logs. They are fundamentally different workloads requiring different architectures.
- Do NOT jump to the final architecture. Build it step by step (Attempt 0 → 5).
- Do NOT make up internal implementation details — verify against official docs or mark as inferred.
- Do NOT skip the iterative build-up.
- Do NOT write a Wikipedia article. This is an INTERVIEW SIMULATION with dialogue.
- Do NOT describe features without explaining WHY they exist (what problem they solve).
- Do NOT ask the user for permission to read online documentation — blanket permission is granted (see Accuracy requirements section).
- Do NOT ignore the cardinality challenge — high-cardinality labels are the #1 cause of metrics system failures. Treat it as a first-class architectural concern.
- Do NOT ignore the meta-problem — monitoring the monitoring system is essential and should be discussed.
