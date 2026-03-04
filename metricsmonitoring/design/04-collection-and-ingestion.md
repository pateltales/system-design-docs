# Metrics Collection & Ingestion Pipeline

## How Metrics Travel from Application Code to the Monitoring System

The collection pipeline is the "first mile" of any monitoring system. Its reliability and efficiency directly determine data quality — if collection is lossy, every downstream component (TSDB, alerting, dashboards) suffers.

---

## 1. Push vs Pull Collection

### Push Model (Datadog, StatsD, OpenTelemetry, InfluxDB Telegraf)

```
┌──────────────┐     ┌──────────────┐     ┌──────────────────┐
│ Application  │────>│  Agent       │────>│  Monitoring      │
│ (instrumented│     │ (on same     │     │  Backend         │
│  with SDK)   │     │  host)       │     │  (SaaS or self-  │
│              │     │              │     │   hosted)        │
│ emit metrics │     │ • aggregates │     │                  │
│ via SDK/     │     │ • buffers    │     │ • ingests        │
│ StatsD/OTLP  │     │ • compresses │     │ • stores in TSDB │
└──────────────┘     │ • retries    │     └──────────────────┘
                     └──────────────┘
```

**How it works**: The application (or host-level agent) actively sends metric data points to the monitoring backend. The agent typically runs as a daemon on the same host, collecting system metrics and receiving application metrics, then forwarding batches to the backend at regular intervals (every 10-15 seconds).

**Pros**:
- Works behind firewalls and NAT — the agent initiates outbound connections (critical for SaaS like Datadog)
- Short-lived processes (batch jobs, Lambda functions, cron jobs) can push their metrics before exiting
- No service discovery needed — the agent knows where to send data (configured backend endpoint)
- Application controls emission frequency — can flush metrics on shutdown

**Cons**:
- Backend can be overwhelmed — no inherent backpressure (if 10,000 agents all push simultaneously after a network partition heals, the backend gets a thundering herd)
- Trust boundary — the backend must validate data from untrusted agents (garbage data, cardinality bombs)
- Agent dependency — every host needs an agent installed, configured, and maintained

**Who chose push and why**: Datadog chose push because their SaaS model requires agents to push through customer firewalls to the Datadog cloud. The agent-based model also allows Datadog to collect system metrics, container metrics, and integration data from the host without the customer exposing any endpoints.

### Pull Model (Prometheus, Nagios)

```
┌──────────────────┐       ┌──────────────┐
│ Monitoring       │──────>│ Application  │
│ Server           │scrape │ (exposes     │
│                  │ every │  /metrics    │
│ • service disc.  │ 15s   │  endpoint)   │
│ • scrape targets │       │              │
│ • store in TSDB  │       │ GET /metrics │
│ • evaluate alerts│       │ → text format│
└──────────────────┘       └──────────────┘
```

**How it works**: The monitoring server periodically scrapes (HTTP GET) a `/metrics` endpoint on each target application. The target exposes its current metric values in a text-based exposition format. The server controls the scrape interval (typically 15-60 seconds).

**Pros**:
- Server controls scrape rate — no flood risk (the server decides how fast to collect)
- Free health signal — if a scrape fails, the target is unhealthy (no need for a separate health check)
- No agent on the target — just expose an HTTP endpoint (simpler deployment)
- Simpler security model — monitoring server initiates all connections (one-way trust)

**Cons**:
- Requires service discovery — the server must know what to scrape and where
- Doesn't work behind firewalls/NAT — server must be able to reach every target
- Short-lived processes may exit before being scraped (missed data)
- All targets must expose an HTTP endpoint — not always feasible (embedded systems, legacy apps)

**Who chose pull and why**: Prometheus chose pull because it simplifies the server design (no ingestion rate limiting, no backpressure mechanism), and the scrape failure = target down signal is extremely valuable for reliability monitoring. In Kubernetes environments, pull works naturally — all pods are reachable from within the cluster.

### Industry Convergence

The push vs pull distinction is blurring:

| System | Primary Model | But Also Supports |
|---|---|---|
| Prometheus | Pull (scrape) | Push via Pushgateway (for batch jobs) |
| Datadog Agent | Push (to backend) | Pull (scrapes Prometheus /metrics endpoints locally) |
| OpenTelemetry | Push (OTLP) | Pull (Prometheus receiver in Collector) |
| InfluxDB | Push (Telegraf agent) | Both push and pull via plugins |

**OpenTelemetry** is the industry standard that supports both models. Instrument once with the OTel SDK → push to an OTel Collector → the Collector can either push to a backend or expose a `/metrics` endpoint for scraping.

---

## 2. Agent Architecture — Datadog Agent as Reference

The Datadog Agent [VERIFIED — Datadog documentation] is the canonical example of a push-based metrics collection agent.

### Agent Components

```
┌─────────────────────────────────────────────────────────────────┐
│  Datadog Agent (runs on every host)                             │
│                                                                 │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  Core Agent                                              │   │
│  │  • Collects system metrics (CPU, memory, disk, network)  │   │
│  │  • Runs integration checks (MySQL, Redis, NGINX, etc.)   │   │
│  │  • Receives custom metrics via DogStatsD (UDP :8125)     │   │
│  │  • Receives traces via APM (TCP :8126)                   │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                 │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  Local Aggregation                                       │   │
│  │  • Pre-aggregates metrics before sending                 │   │
│  │  • StatsD counters: sum all increments in 10s window     │   │
│  │  • StatsD histograms: compute local percentiles          │   │
│  │  • Reduces network traffic by 10-100x                    │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                 │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  Forwarder                                               │   │
│  │  • Batches metrics (default: every 10 seconds)           │   │
│  │  • Compresses with gzip before sending                   │   │
│  │  • HTTPS POST to intake.datadoghq.com                    │   │
│  │  • Retry with exponential backoff on failure             │   │
│  │  • Local buffer for brief outages (~4 hours)             │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                 │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  Process Agent / Trace Agent / Log Agent                 │   │
│  │  • Separate processes for different signal types         │   │
│  │  • Process agent: per-process CPU/memory/connections     │   │
│  │  • Trace agent: receives APM traces, samples, forwards  │   │
│  │  • Log agent: tails log files, parses, forwards          │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

### Local Aggregation — Why It Matters

Without local aggregation, a service handling 10,000 requests/second would generate 10,000 StatsD metric emissions per second. With local aggregation:

```
WITHOUT aggregation (naive):
  10,000 req/s × 5 metrics/req = 50,000 data points/second to backend

WITH local aggregation (10-second window):
  50,000 data points → aggregated into 5 summary values per 10s:
    request.count = 100,000 (sum)
    request.latency.p50 = 12ms
    request.latency.p95 = 45ms
    request.latency.p99 = 120ms
    request.error_rate = 0.3%

  Network traffic reduction: ~10,000x
```

This is why Datadog's agent is not just a forwarder — it's a local data processing engine. Without aggregation, the backend would need to handle orders of magnitude more data.

### Agent Resource Footprint

The agent itself consumes host resources. A well-designed agent:
- CPU: <1% of a single core under normal load
- Memory: 50-150 MB (varies with number of integrations enabled)
- Network: ~100 KB per 10-second flush (compressed)
- Disk: minimal (only for buffering during outages)

Trade-off: a heavier agent that does more local aggregation sends less data to the backend (saves backend cost and network bandwidth), but consumes more CPU/memory on every host.

---

## 3. OpenTelemetry Collector Architecture

The OpenTelemetry Collector [VERIFIED — OpenTelemetry documentation] is the vendor-neutral, open-source alternative to proprietary agents.

### Pipeline Architecture: Receivers → Processors → Exporters

```
┌────────────────────────────────────────────────────────────────────┐
│  OpenTelemetry Collector                                           │
│                                                                    │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐ │
│  │   RECEIVERS       │  │   PROCESSORS      │  │   EXPORTERS      │ │
│  │                  │  │                  │  │                  │ │
│  │  • OTLP (gRPC/  │  │  • Batch         │  │  • Datadog       │ │
│  │    HTTP)         │─>│    (group for    │─>│  • Prometheus    │ │
│  │  • Prometheus    │  │    efficiency)   │  │    Remote Write  │ │
│  │    (scrape)      │  │  • Filter        │  │  • OTLP (to      │ │
│  │  • StatsD (UDP)  │  │    (drop unwanted│  │    another       │ │
│  │  • Jaeger        │  │    metrics)      │  │    collector)    │ │
│  │  • Zipkin        │  │  • Transform     │  │  • Jaeger        │ │
│  │  • Host Metrics  │  │    (rename,      │  │  • Zipkin        │ │
│  │  • Kafka         │  │    relabel)      │  │  • Kafka         │ │
│  │  • File          │  │  • Memory        │  │  • File          │ │
│  │                  │  │    Limiter       │  │  • Logging       │ │
│  │                  │  │  • Sampling      │  │                  │ │
│  └──────────────────┘  └──────────────────┘  └──────────────────┘ │
└────────────────────────────────────────────────────────────────────┘
```

### Key Processors (the power of the pipeline)

**Batch processor**: Accumulates data and sends in batches. Configurable by size (e.g., send every 200 items) or time (e.g., send every 5 seconds, whichever comes first). Critical for efficiency — without batching, each metric emission would be a separate RPC call.

**Filter processor**: Drop metrics you don't want before they reach the backend. Example: drop internal library metrics that generate high cardinality but provide no value. This is where cardinality management starts — at the collection edge, not at the storage layer.

```yaml
processors:
  filter:
    metrics:
      exclude:
        match_type: regexp
        metric_names:
          - "go_.*"          # Drop Go runtime internals
          - "process_.*"     # Drop process-level details
        resource_attributes:
          - key: "environment"
            value: "dev"     # Drop all dev environment metrics
```

**Transform processor**: Rename metrics, add/remove/modify labels. Useful for normalizing metrics from different sources into a consistent naming scheme.

**Memory limiter processor**: Prevents the Collector from using too much memory (OOM). If memory usage exceeds a threshold, the Collector starts dropping data and reports backpressure to receivers. This is the Collector's self-protection mechanism.

### The Key Value Proposition

Instrument once with the OpenTelemetry SDK → send to any backend by changing the Collector's exporter configuration:

```yaml
# Switch from Datadog to Prometheus by changing one config block:
exporters:
  # datadog:                    # ← comment out
  #   api_key: "xxx"
  prometheusremotewrite:         # ← add this
    endpoint: "http://prometheus:9090/api/v1/write"
```

This vendor-neutrality is why OpenTelemetry has become the industry standard for instrumentation. Adopting OTel today means you're not locked into any specific monitoring backend.

### Deployment Patterns: Agent vs Gateway

```
AGENT MODE (per-host):                    GATEWAY MODE (centralized):

┌────────┐  ┌────────┐  ┌────────┐       ┌────────┐  ┌────────┐  ┌────────┐
│  App   │  │  App   │  │  App   │       │  App   │  │  App   │  │  App   │
│  +     │  │  +     │  │  +     │       │        │  │        │  │        │
│Collector│  │Collector│  │Collector│      └───┬────┘  └───┬────┘  └───┬────┘
└───┬────┘  └───┬────┘  └───┬────┘            │          │          │
    │           │           │                 └──────────┼──────────┘
    ▼           ▼           ▼                            ▼
┌──────────────────────────────┐          ┌──────────────────────────┐
│      Monitoring Backend      │          │   Gateway Collector      │
└──────────────────────────────┘          │   (centralized pool)     │
                                          └───────────┬──────────────┘
                                                      ▼
                                          ┌──────────────────────────┐
                                          │   Monitoring Backend     │
                                          └──────────────────────────┘
```

**Agent mode**: One Collector per host. Lower latency, data stays local. Higher resource usage across the fleet (N hosts × agent overhead).

**Gateway mode**: Centralized Collector pool. Applications send data directly (or via a lightweight forwarder). Lower total resource usage but introduces a network hop and potential bottleneck.

**Production recommendation**: Use both — an agent-mode Collector on each host for local collection and initial processing, forwarding to a gateway Collector pool that handles cross-cutting concerns (sampling, routing to multiple backends). This is the "Collector layering" pattern recommended by the OpenTelemetry project.

---

## 4. Kubernetes-Native Collection

Kubernetes is the dominant deployment platform for modern applications. Metrics collection in Kubernetes has its own patterns and challenges.

### DaemonSet Deployment

```
┌───────────────────────────────────────────────────────────┐
│  Kubernetes Node                                          │
│                                                           │
│  ┌─────────────────────────────────────────────────────┐  │
│  │  Agent DaemonSet Pod                                │  │
│  │  (one per node — guaranteed by DaemonSet controller)│  │
│  │                                                     │  │
│  │  Collects:                                          │  │
│  │  • Node-level metrics (CPU, memory, disk, network)  │  │
│  │  • Pod-level metrics via kubelet API                 │  │
│  │  • Container metrics via cAdvisor                   │  │
│  │  • Application metrics by scraping pod /metrics      │  │
│  └─────────────────────────────────────────────────────┘  │
│                                                           │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐               │
│  │  App Pod  │  │  App Pod  │  │  App Pod  │              │
│  │ /metrics  │  │ /metrics  │  │ /metrics  │              │
│  └──────────┘  └──────────┘  └──────────┘               │
└───────────────────────────────────────────────────────────┘
```

**Why DaemonSet**: Kubernetes guarantees exactly one pod per node. The agent automatically appears on new nodes and disappears from removed nodes. No manual installation or configuration needed per host.

### Sidecar Deployment (Alternative)

```
┌──────────────────────────────────────┐
│  Application Pod                     │
│                                      │
│  ┌──────────────┐  ┌──────────────┐  │
│  │  App         │  │  Collector   │  │
│  │  Container   │──│  Sidecar     │  │
│  │              │  │              │  │
│  │ emit metrics │  │ receive,     │  │
│  │ to localhost │  │ process,     │  │
│  │              │  │ forward      │  │
│  └──────────────┘  └──────────────┘  │
└──────────────────────────────────────┘
```

**When to use sidecar**: When you need per-pod isolation (multi-tenant clusters), or when the application emits metrics over localhost only (no network exposure). Trade-off: higher resource overhead (one collector per pod instead of per node), but stronger isolation and simpler networking.

### Kubernetes Service Discovery

The agent automatically discovers scrape targets by watching the Kubernetes API:

```yaml
# Prometheus-style Kubernetes service discovery
# Pods opt in to scraping via annotations:
apiVersion: v1
kind: Pod
metadata:
  annotations:
    prometheus.io/scrape: "true"      # "yes, scrape me"
    prometheus.io/port: "8080"        # on this port
    prometheus.io/path: "/metrics"    # at this path
```

**How it works**:
1. Agent watches Kubernetes API for pod create/delete events
2. When a pod with `prometheus.io/scrape: "true"` appears, agent adds it to the scrape target list
3. When the pod is deleted, agent removes it from the target list
4. Labels from Kubernetes metadata (namespace, deployment name, pod name, node) are automatically added as metric tags

This is why pull-based collection works so well in Kubernetes — service discovery is built into the platform.

### Key Kubernetes Metric Sources

**kubelet metrics API**: Per-node metrics about pods running on that node. CPU, memory, filesystem, network per container. Available at `https://<node>:10250/metrics/resource`.

**cAdvisor** [VERIFIED — cAdvisor is built into kubelet]: Container-level resource usage metrics. CPU usage per container, memory usage per container, filesystem reads/writes, network I/O. Exposed as Prometheus-format metrics by kubelet. No separate installation needed — it's part of kubelet.

**kube-state-metrics** [VERIFIED — Kubernetes SIG project]: A dedicated exporter that converts Kubernetes object state into Prometheus metrics:
- Deployment: desired replicas, available replicas, unavailable replicas
- Pod: status (Pending, Running, Failed), restart count, container states
- Node: conditions (Ready, MemoryPressure, DiskPressure), allocatable resources
- Job/CronJob: completion status, active count, last schedule time
- PersistentVolumeClaim: bound status, capacity

kube-state-metrics doesn't measure resource usage (that's cAdvisor's job) — it measures Kubernetes object state. The two are complementary.

**metrics-server** [VERIFIED — Kubernetes SIG project]: Lightweight, in-cluster component that collects resource metrics (CPU, memory) from kubelets. Used by Horizontal Pod Autoscaler (HPA) and `kubectl top`. Not a monitoring system — just provides the data that HPA needs to make scaling decisions.

### The Complete Kubernetes Metrics Stack

```
                    ┌──────────────────────────────┐
                    │  Monitoring Backend           │
                    │  (Prometheus / Datadog /      │
                    │   Grafana Cloud)              │
                    └──────────┬───────────────────┘
                               ▲
                               │
              ┌────────────────┼─────────────────┐
              │                │                 │
   ┌──────────┴───┐  ┌────────┴──────┐  ┌───────┴────────┐
   │ DaemonSet    │  │ kube-state-   │  │ Custom         │
   │ Agent        │  │ metrics       │  │ Exporters      │
   │              │  │               │  │                │
   │ • cAdvisor   │  │ • Object      │  │ • MySQL        │
   │   (container │  │   state       │  │ • Redis        │
   │   resources) │  │   (deploy,    │  │ • Kafka        │
   │ • kubelet    │  │   pod, node   │  │ • Custom app   │
   │   (node      │  │   status)     │  │   metrics      │
   │   resources) │  │               │  │                │
   │ • Pod scrape │  │               │  │                │
   └──────────────┘  └───────────────┘  └────────────────┘
```

---

## 5. Ingestion Pipeline at Scale

Once metrics arrive at the monitoring backend, the ingestion pipeline must handle millions of data points per second without data loss.

### Architecture: Kafka as the Ingestion Buffer

```
┌──────────────┐     ┌──────────────────────────────────────────────────┐
│  Agents /    │     │  Monitoring Backend                              │
│  Collectors  │     │                                                  │
│              │     │  ┌──────────┐     ┌──────────┐   ┌──────────┐   │
│  Push data   │────>│  │ Ingestion│────>│  Kafka   │──>│ Storage  │   │
│  via HTTPS/  │     │  │ Gateway  │     │  (buffer)│   │ Writers  │   │
│  gRPC        │     │  │          │     │          │   │          │   │
│              │     │  │ • Auth   │     │ • Absorbs│   │ • Batch  │   │
│              │     │  │ • Validate│    │   bursts │   │   writes │   │
│              │     │  │ • Route  │     │ • Replay │   │ • Write  │   │
│              │     │  │ • Rate   │     │   on     │   │   to TSDB│   │
│              │     │  │   limit  │     │   failure│   │ • WAL    │   │
│              │     │  └──────────┘     └──────────┘   └──────────┘   │
└──────────────┘     └──────────────────────────────────────────────────┘
```

### Ingestion Gateway Responsibilities

1. **Authentication**: Validate API key/token (who is sending this data?)
2. **Validation**: Check metric format (valid name, valid tags, reasonable timestamp)
3. **Rate limiting**: Per-tenant rate limits to prevent one customer from overwhelming the system
4. **Cardinality checks**: Reject metrics that would create too many new time series (cardinality bomb protection)
5. **Routing**: Determine which Kafka partition (and downstream TSDB shard) should receive this data

### Why Kafka in Front of the TSDB

| Concern | Without Kafka | With Kafka |
|---|---|---|
| **Burst absorption** | TSDB must handle peak load directly | Kafka absorbs bursts; TSDB consumes at steady rate |
| **Data loss on TSDB failure** | Data in flight is lost | Data safe in Kafka; replay after TSDB recovers |
| **Multiple consumers** | Can't fan-out to multiple sinks | Kafka topic consumed by TSDB writer, analytics pipeline, real-time alerting |
| **Backpressure** | Must reject writes or risk OOM | Kafka buffers; consumers lag but don't lose data |
| **Replayability** | No replay | Replay from any offset (re-index, reprocess) |

### Partitioning Strategy in Kafka

How to partition metric data across Kafka partitions determines write distribution and downstream query patterns:

```
Option A: Partition by metric_name hash
  Pro: Evenly distributed writes
  Con: A single query (e.g., all metrics for host:web-01) may span all partitions

Option B: Partition by tenant_id
  Pro: Tenant isolation (one tenant's traffic doesn't affect others)
  Con: Hot tenants create hot partitions

Option C: Partition by metric_name + tenant_id hash  ← most common
  Pro: Balanced load + tenant isolation
  Con: Slightly more complex routing logic

Option D: Partition by time range
  Pro: Natural alignment with time-based TSDB blocks
  Con: "Now" partition is always the hottest
```

Most production systems use **Option C** — hash(tenant_id, metric_name) determines the Kafka partition and the downstream TSDB shard. This ensures that all data for a given metric within a tenant lands on the same shard, enabling efficient queries without fan-out.

### Write Path Through the TSDB

Once a storage writer consumes a batch from Kafka:

```
1. DESERIALIZE batch of metric data points

2. For each data point:
   a. LOOKUP series ID:
      series_key = metric_name + sorted(labels)
      if series_key exists in memory → get series_id
      if new → create new series, assign series_id, update inverted index

   b. APPEND sample (timestamp, value) to the series' in-memory chunk
      Uses Gorilla compression (delta-of-delta timestamps, XOR values)

   c. WRITE to WAL (for durability)
      Sequential append — very fast

3. Periodically (every ~2 hours in Prometheus TSDB):
   FLUSH in-memory chunks to immutable on-disk blocks
   Blocks are indexed, compressed, and optionally uploaded to object storage
```

### Handling Ingestion Failures

**Agent-side retries**: If the backend rejects or times out, the agent retries with exponential backoff + jitter. Local buffering keeps data safe for a few hours.

**Kafka consumer lag**: If TSDB writers can't keep up, Kafka consumer lag increases. This is visible in the monitoring system's own metrics (yes, you monitor Kafka consumer lag with... the monitoring system). If lag exceeds a threshold, alert the platform team.

**Out-of-order data**: Timestamps may arrive out of order (network delays, agent retries). The TSDB must handle out-of-order samples. Prometheus TSDB (since 2.39) supports out-of-order ingestion within a configurable window [VERIFIED — Prometheus documentation]. Before this, out-of-order samples were silently dropped.

---

## 6. Contrast: Metrics Collection vs Log Collection

| Dimension | Metrics | Logs |
|---|---|---|
| **Data format** | Structured: `{name, value, timestamp, tags}` | Semi-structured: text lines with optional parsing |
| **Size per event** | ~16 bytes (timestamp + float64 value) | ~100-1,000 bytes (text line) |
| **Volume** | Millions of data points/second | Millions of log lines/second (but 10-100x more bytes) |
| **Storage** | Time-series database (Prometheus TSDB, InfluxDB) | Search engine (Elasticsearch, Loki, Splunk) |
| **Compression** | Gorilla: ~12x (exploits time-series patterns) | General-purpose: ~5-10x (gzip, zstd, Snappy) |
| **Query pattern** | Aggregate over time: "avg CPU last 1 hour" | Search by keyword: "find ERROR lines with trace_id=abc" |
| **Aggregation** | Pre-aggregated (counter already counts) | Post-ingestion (count lines matching pattern) |
| **Retention cost** | Cheap (compressed numeric data) | Expensive (verbose text, full-text indexes) |
| **Collection** | Agent aggregates locally, sends summaries | Agent tails files, sends every line |

**Key insight**: Metrics are for "what happened" (quantitative). Logs are for "why it happened" (qualitative). Both are essential, but they need fundamentally different storage and query engines. Don't try to store metrics in Elasticsearch or logs in a TSDB — use the right tool for each workload.

---

## 7. Prometheus Exposition Format

The de facto standard for exposing application metrics [VERIFIED — Prometheus documentation]:

```
# HELP http_requests_total The total number of HTTP requests.
# TYPE http_requests_total counter
http_requests_total{method="GET",status="200"} 1027
http_requests_total{method="GET",status="404"} 3
http_requests_total{method="POST",status="200"} 456

# HELP http_request_duration_seconds Request latency histogram.
# TYPE http_request_duration_seconds histogram
http_request_duration_seconds_bucket{le="0.01"} 500
http_request_duration_seconds_bucket{le="0.05"} 800
http_request_duration_seconds_bucket{le="0.1"} 900
http_request_duration_seconds_bucket{le="0.5"} 980
http_request_duration_seconds_bucket{le="1"} 995
http_request_duration_seconds_bucket{le="+Inf"} 1000
http_request_duration_seconds_sum 120.5
http_request_duration_seconds_count 1000

# HELP node_memory_usage_bytes Current memory usage in bytes.
# TYPE node_memory_usage_bytes gauge
node_memory_usage_bytes 4294967296
```

**Format properties**:
- Human-readable text (easy to debug with `curl`)
- Self-describing (`# TYPE` declares counter/gauge/histogram/summary)
- Compact enough for HTTP responses (a typical app exposes 100-500 metrics in ~10-50 KB)
- Supported by virtually every modern monitoring system

**OpenTelemetry OTLP** is the successor — binary protobuf format (more efficient, not human-readable) that supports metrics + traces + logs in one protocol. But Prometheus exposition format remains the most widely adopted for metrics specifically.

---

## 8. Collection Pipeline Reliability

### Data Loss Prevention

The collection pipeline has multiple failure points. Each requires a mitigation:

| Failure Point | Risk | Mitigation |
|---|---|---|
| Application crash before emit | Lose in-flight metrics | Flush metrics on SIGTERM; accept some loss for crashes |
| Agent crash | Lose buffered, unsent data | WAL on agent; restart and replay |
| Network partition (agent ↔ backend) | Agent can't send data | Local buffer (4-24 hours); retry with backoff |
| Ingestion gateway overload | Reject incoming data | Rate limiting + Kafka buffering + horizontal scaling |
| Kafka broker failure | Potential data loss | Kafka replication factor ≥ 3 across availability zones |
| TSDB writer crash | Lose in-flight batch | Kafka retains data; new consumer picks up from last offset |
| TSDB disk full | Reject new writes | Monitoring + alerting on disk usage; auto-retention policies |

### End-to-End Latency Budget

```
App emits metric                    t = 0s
Agent receives (localhost UDP/TCP)  t = ~1ms
Agent aggregates (10s window)       t = ~10s
Agent flushes to backend            t = ~10s
Ingestion gateway processes         t = ~50ms
Kafka produces                      t = ~10ms
Kafka consumer reads                t = ~100ms-1s (depends on consumer lag)
TSDB writes to memory + WAL         t = ~10ms
                                    ─────────
Total: metric emitted → queryable   ~15-25 seconds typical

This means: if CPU spikes at t=0, the dashboard shows it at t≈20s.
This is acceptable for most monitoring use cases.
For real-time alerting: alert evaluator queries TSDB every 15-60s,
so worst-case alert latency = ingestion lag + evaluation interval ≈ 45-85s.
```

---

## Summary

| Component | Purpose | Key Design Choice |
|---|---|---|
| Push vs Pull | How metrics leave the application | Push for SaaS/ephemeral; Pull for static/k8s |
| Agent (Datadog) | Local collection + aggregation | Heavy agent reduces backend load 10-100x |
| OTel Collector | Vendor-neutral pipeline | Receivers → Processors → Exporters |
| Kubernetes | Cloud-native collection | DaemonSet + service discovery + cAdvisor + kube-state-metrics |
| Kafka buffer | Decouple ingestion from storage | Absorb bursts, enable replay, fan-out to multiple consumers |
| Write path | Get data into the TSDB | Batch writes, Gorilla compression, WAL for durability |
