# Design Trade-offs — Not Just "What" But "Why This and Not That"

## Opinionated Analysis of Every Major Design Choice in a Metrics System

Every architectural decision in a monitoring system is a trade-off. This doc examines the most important ones — explaining not just what each system chose, but WHY, and when you would choose differently.

---

## Trade-off 1: Push vs Pull Collection

### The Core Tension

**Push**: Application/agent sends metrics TO the monitoring system.
**Pull**: Monitoring system scrapes metrics FROM the application.

### Detailed Comparison

| Dimension | Push (Datadog, StatsD, OTLP) | Pull (Prometheus) |
|---|---|---|
| **Who initiates?** | Client → Server | Server → Client |
| **Firewall/NAT** | Works (client pushes outbound) | Blocked (server can't reach client) |
| **Short-lived processes** | Works (push before exit) | Missed (may exit before scrape) |
| **Backpressure** | Hard (server can be overwhelmed) | Natural (server controls rate) |
| **Health detection** | Must add health checks separately | Free (scrape failure = target down) |
| **Service discovery** | Not needed (client knows server) | Required (server must find targets) |
| **Security** | Client authenticates to server | Server authenticates to client (or trusts network) |
| **Operational complexity** | Agent on every host | Service discovery system |

### Why Prometheus Chose Pull

Prometheus was designed for Kubernetes-like environments where:
- All services are reachable from within the cluster (no firewall issues)
- Service discovery is built-in (Kubernetes API)
- The monitoring server should control scrape frequency (prevents overload)
- Scrape failure = target is down (free health signal, no separate health check)

### Why Datadog Chose Push

Datadog is a SaaS product where:
- Customers' servers are behind corporate firewalls (agent must push outbound)
- No access to customers' internal networks (can't initiate connections inward)
- Must support diverse environments (VMs, containers, serverless, on-prem)
- Agent model allows local aggregation, reducing backend load

### When Would You Choose Each?

```
Choose PULL when:
  • Infrastructure is in a single network (cloud VPC, Kubernetes cluster)
  • Service discovery is available (Kubernetes, Consul, DNS)
  • You want the monitoring server to control scrape frequency
  • You value simplicity (no agent deployment/management)

Choose PUSH when:
  • Applications are behind firewalls or NAT
  • You have short-lived processes (Lambda, batch jobs)
  • You're building a SaaS monitoring product (multi-tenant, customer environments)
  • You want rich local aggregation before sending to backend

Choose BOTH when:
  • You have a mix of environments (OpenTelemetry supports both)
  • You want maximum compatibility (Datadog agent does both)
```

---

## Trade-off 2: Single-Node TSDB vs Distributed TSDB

### The Core Tension

**Single-node (Prometheus)**: All data on one machine. Simple, fast, limited.
**Distributed (Cortex/Mimir, Datadog)**: Data sharded across a cluster. Complex, scalable, unlimited.

### Detailed Comparison

| Dimension | Single-Node (Prometheus) | Distributed (Cortex/Mimir) |
|---|---|---|
| **Complexity** | Single binary, minimal config | 5+ components, Kafka, object storage |
| **Scale limit** | ~10M active series per instance | Unlimited (horizontal scaling) |
| **Write latency** | ~1ms (local disk) | ~10-50ms (network + replication) |
| **Query latency** | Low (all data local) | Higher (fan-out + merge) |
| **Durability** | Single disk (RAID for protection) | 3x replication + object storage (11 nines) |
| **HA** | Run 2 instances (HA pair, 2x data) | Built-in replication |
| **Multi-tenancy** | No (single tenant) | Built-in (tenant isolation) |
| **Cost** | Low (one server) | Higher (cluster + storage) |
| **Operational burden** | Low (operate one process) | High (operate a distributed system) |
| **Recovery time** | Replay WAL (~minutes) | Kafka replay + ingester hand-off (~seconds) |

### When Does Single-Node Break?

```
Single Prometheus instance limits:
  ~10M active time series (memory-limited)
  ~1M samples/second ingestion (CPU-limited)
  ~2 weeks local retention (disk-limited)
  ~100 concurrent queries (CPU-limited)

You outgrow single-node when:
  • You have >10M active series (large microservice deployment)
  • You need >2 weeks retention with raw resolution
  • You need multi-region or multi-tenant isolation
  • You can't tolerate any data loss (single node = single point of failure)

MOST TEAMS START WITH SINGLE PROMETHEUS AND ONLY ADD COMPLEXITY WHEN NEEDED.
Don't build a distributed TSDB for 100K time series.
```

### The Middle Ground: Thanos

Thanos sits between single-node and fully distributed:
- Keep familiar Prometheus (single-node TSDB, PromQL)
- Add long-term storage (S3 via Sidecar)
- Add global query (Thanos Query across multiple Prometheus instances)
- Add downsampling (Thanos Compactor)

This gives you most of the benefits of distributed storage without replacing Prometheus entirely. It's the right choice for teams that have outgrown a single Prometheus but don't need full-scale Cortex/Mimir.

---

## Trade-off 3: Histogram vs Summary for Latency

### The Core Tension

Both measure distributions (e.g., request latency). But they aggregate differently.

**Histogram**: Pre-defined buckets, counted client-side. Aggregatable.
**Summary**: Quantiles computed client-side. NOT aggregatable.

### Why Histograms Win for Multi-Instance Services

```
HISTOGRAM (aggregatable):
  Instance A buckets: le=10ms: 100, le=50ms: 400, le=100ms: 480, le=+Inf: 500
  Instance B buckets: le=10ms: 200, le=50ms: 600, le=100ms: 700, le=+Inf: 750

  Global histogram (sum A + B):
    le=10ms: 300, le=50ms: 1000, le=100ms: 1180, le=+Inf: 1250

  histogram_quantile(0.95, global_histogram) → valid P95 across both instances ✓

SUMMARY (NOT aggregatable):
  Instance A P95: 85ms
  Instance B P95: 42ms

  "Global P95" = ???
  avg(85, 42) = 63.5ms? WRONG. That's not how percentiles work.
  The actual global P95 could be anything from 42ms to 85ms (or higher).
  You CANNOT merge percentiles mathematically.

This is why Prometheus documentation recommends histograms over summaries
for any metric that needs to be aggregated across instances.
```

### When Summaries Are Better

```
Summaries are useful when:
  • You have a SINGLE instance (no aggregation needed)
  • You need EXACT quantiles (histograms interpolate within buckets)
  • You can't predict bucket boundaries in advance

Histograms are better when:
  • You have multiple instances (must aggregate)
  • You can define reasonable bucket boundaries
  • You need to compute quantiles at query time (flexible)
  • You want to change percentiles without re-instrumenting code
```

### The Bucket Boundary Problem

```
Histogram accuracy depends on bucket boundaries:

Good buckets for HTTP latency:
  [5ms, 10ms, 25ms, 50ms, 100ms, 250ms, 500ms, 1s, 2.5s, 5s, 10s]
  → Fine-grained where most requests fall, covers the tail

Bad buckets:
  [100ms, 1s, 10s]
  → P99 somewhere between 1s and 10s... not very useful
  → Too few buckets = inaccurate quantile estimation

Trade-off: more buckets = more accuracy, but each bucket is a separate
time series. 10 buckets × 5 labels = 50 series per histogram metric.
Too many buckets = cardinality explosion.

Emerging solution: Native histograms (Prometheus experimental feature)
use exponentially-spaced buckets with no cardinality explosion.
Still experimental as of Prometheus 2.x [VERIFIED — Prometheus documentation].
```

---

## Trade-off 4: Gorilla (In-Memory) vs LSM/TSM (Disk-Based) Storage

### The Core Tension

**In-memory (Gorilla)**: All data in RAM. Fastest reads, highest cost, limited retention.
**Disk-based (Prometheus TSDB, InfluxDB TSM)**: Recent data in memory, historical on disk. Balanced cost/performance.

### Comparison

| Dimension | In-Memory (Gorilla/Facebook) | Disk-Based (Prometheus TSDB) |
|---|---|---|
| **Read latency** | <1ms (all in RAM) | <1ms (recent), 5-50ms (disk blocks) |
| **Write throughput** | Very high (no disk I/O on write path) | High (WAL is sequential, fast) |
| **Cost** | $$$$ (RAM is expensive) | $$ (disk + some RAM) |
| **Retention** | Limited (26 hours at Facebook) | Days-weeks (local), unlimited (object storage) |
| **Compression** | Gorilla: ~1.37 bytes/point | Gorilla + block compaction: ~1-2 bytes/point |
| **Recovery** | Stream from peers | WAL replay |
| **Durability** | Replicated across nodes (no disk) | WAL on disk + replication |

### What Production Systems Actually Do

```
The answer is BOTH — tiered storage:

  HOT TIER (last 2-4 hours): In-memory
    → Used by: alert evaluation, real-time dashboards
    → Gorilla compression, sub-millisecond access
    → Where speed matters most

  WARM TIER (2 hours - 15 days): On-disk blocks
    → Used by: recent dashboard queries, investigations
    → Immutable blocks, indexed, compressed
    → SSD storage for fast I/O

  COLD TIER (15 days - 1 year+): Object storage (S3/GCS)
    → Used by: historical queries, capacity planning
    → Downsampled data (1-min, 1-hour rollups)
    → Cheapest storage (~$0.023/GB/month)

This tiered approach gives:
  • Fast access to recent data (in-memory)
  • Cost-effective storage of historical data (S3)
  • Unlimited retention (object storage has no practical limit)
```

---

## Trade-off 5: Tags/Labels as First-Class Dimensions vs Flat Metric Names

### The Core Tension

**Label-based (Prometheus model)**: `http_requests_total{method="GET", status="200", service="api"}`
**Flat names (StatsD model)**: `api.http.requests.get.200.total`

### Comparison

| Dimension | Label-Based | Flat Names |
|---|---|---|
| **Aggregation** | Flexible: `sum by (service)`, `sum by (method)` | Rigid: pre-determined by name structure |
| **Cardinality risk** | HIGH — easy to add a label with 1M values | Low — name explosion is visible |
| **Query expressiveness** | `{method=~"GET|POST", status=~"5.."}` — regex on any dimension | Must know the exact name pattern |
| **Storage efficiency** | Inverted index needed for label lookups | Simple trie/prefix tree sufficient |
| **Discovery** | `label_values(method)` — discover dimensions | Must know naming convention |
| **Tooling ecosystem** | PromQL, Grafana, OpenTelemetry — all label-based | StatsD, Graphite — mature but aging |

### Why the Industry Converged on Labels

```
The aggregation power of labels is overwhelming:

With labels:
  http_requests_total{method="GET", status="200", service="api", host="web-01"}

  You can query:
  • Total requests:         sum(http_requests_total)
  • By service:             sum(http_requests_total) by (service)
  • By status code:         sum(http_requests_total) by (status)
  • By host:                sum(http_requests_total) by (host)
  • Error rate per service: sum(rate(http_requests_total{status=~"5.."}[5m])) by (service)
                            / sum(rate(http_requests_total[5m])) by (service)

  ONE metric definition → unlimited aggregation dimensions at query time.

With flat names:
  api.http.requests.get.200.web-01

  To get "total requests by service," you need:
  sumSeries(*.http.requests.*.*.*)
  And you'd better hope nobody added a new dimension to the name...

Labels won because the query flexibility is worth the cardinality risk,
and cardinality can be managed (limits, education, relabeling).
```

---

## Trade-off 6: All-in-One (Datadog) vs Best-of-Breed (Prometheus + Grafana + Loki + Tempo)

### The Core Tension

**All-in-one**: One vendor/platform for metrics, traces, logs, APM, security.
**Best-of-breed**: Specialized open-source tools for each signal, integrated yourself.

### Comparison

| Dimension | All-in-One (Datadog, New Relic) | Best-of-Breed (OSS stack) |
|---|---|---|
| **Time to value** | Hours (install agent, get dashboards) | Weeks (deploy, configure, integrate each tool) |
| **Operational burden** | Zero (SaaS) | High (operate 5+ tools, each with its own failure modes) |
| **Cost at small scale** | Moderate ($15-23/host/month) | Free (open-source) + compute cost |
| **Cost at large scale** | Expensive ($500K-$5M+/year for large orgs) | Cheap (pay for compute + storage) |
| **Vendor lock-in** | High (proprietary agents, query language, dashboards) | Low (OpenTelemetry + Prometheus format are portable) |
| **Feature velocity** | High (paid product team) | Varies (community-driven, slower for niche features) |
| **Correlation** | Built-in (click metric → see traces → see logs) | Manual (configure data source linking in Grafana) |
| **Customizability** | Limited (SaaS constraints) | Unlimited (open-source, fork if needed) |
| **Data residency** | Datadog's cloud (compliance concerns) | Your infrastructure (full control) |

### Decision Framework

```
Choose ALL-IN-ONE when:
  • Team is <20 engineers (no dedicated platform team)
  • Speed of setup matters more than cost
  • You want one tool for metrics + traces + logs + APM
  • You don't want to operate infrastructure for observability
  • Budget allows $15-23/host/month

Choose BEST-OF-BREED when:
  • Team is >100 engineers (can afford a platform team)
  • Cost at scale is a concern ($1M+/year on Datadog is common)
  • Data residency requirements (data must stay in your cloud)
  • You want to avoid vendor lock-in
  • You have specific needs that Datadog doesn't cover

The transition path:
  Startup → Datadog (fast setup, all-in-one)
  Growth → Evaluate cost (Datadog bills grow with infrastructure)
  Scale → Migrate to OSS stack (Prometheus + Grafana + Loki + Tempo)
         or negotiate Datadog enterprise pricing

Many large companies run BOTH:
  • Datadog for application-level monitoring (APM, traces, synthetics)
  • Prometheus + Thanos for infrastructure metrics (cheaper at scale)
```

---

## Trade-off 7: Static Thresholds vs ML Anomaly Detection for Alerting

### The Core Tension

**Static**: `if cpu > 90% for 5 min → page`. Deterministic, predictable.
**ML-based**: "This value is anomalous relative to the learned baseline." Adaptive, sometimes surprising.

### Comparison

| Dimension | Static Thresholds | ML Anomaly Detection |
|---|---|---|
| **Predictability** | Deterministic (you know exactly when it fires) | Probabilistic (depends on learned baseline) |
| **Seasonality handling** | None (Monday 2pm and Saturday 3am are treated the same) | Yes (learns daily/weekly patterns) |
| **Setup effort** | Manual (choose threshold per metric) | Automatic (learns from data) |
| **False positives** | High for seasonal metrics | Lower (adapts to patterns) |
| **False negatives** | Low (absolute thresholds always catch) | Possible (novel failures may not look anomalous) |
| **Explainability** | "CPU is above 90%" — clear | "CPU is 2.3 sigma above expected" — requires explanation |
| **Debug-ability** | Easy (check threshold, check value) | Hard (why did the model think this was anomalous?) |
| **Cold start** | None (works immediately) | Needs 2-4 weeks of data to learn baseline |

### Best Practice: Use Both

```
STATIC THRESHOLDS for absolute limits:
  "CPU > 95% is NEVER acceptable, regardless of time of day"
  "Disk > 90% means we're about to run out of space"
  "Error rate > 10% is always a problem"

ML ANOMALY DETECTION for relative deviations:
  "This service's latency is 3x higher than usual for this time of day"
  "Request traffic dropped 50% compared to same time last week"
  "This metric is behaving differently from all other similar services"

Static thresholds catch known failure modes.
Anomaly detection catches unknown failure modes.
Together, they cover more ground than either alone.
```

---

## Trade-off 8: PromQL vs SQL for Metrics

### The Core Tension

**PromQL**: Purpose-built for time-series. Compact, expressive for metrics. Unique syntax.
**SQL**: Universal, familiar, verbose for time-series workloads.

### Comparison

| Query | PromQL | SQL |
|---|---|---|
| Request rate | `rate(http_requests_total[5m])` | `SELECT (value - LAG(value) OVER (...)) / (ts - LAG(ts) OVER (...)) FROM metrics WHERE name='http_requests_total' AND ts > NOW()-'5m'` |
| Error percentage | `sum(rate(errors[5m])) / sum(rate(total[5m])) * 100` | Multiple subqueries with window functions |
| P95 latency | `histogram_quantile(0.95, rate(buckets[5m]))` | No standard syntax for histogram quantiles |
| Top 5 services | `topk(5, sum(rate(requests[5m])) by (service))` | `SELECT service, rate FROM (...) ORDER BY rate DESC LIMIT 5` |

### Why PromQL Won for Metrics

```
PromQL is 5-10x more concise for time-series queries.
The concepts that PromQL has as first-class (rate, histogram_quantile,
aggregation by label, range vectors) require verbose workarounds in SQL.

But SQL has advantages:
  • Every engineer already knows SQL
  • Full JOIN support (PromQL has limited label matching)
  • Richer type system
  • Subqueries and CTEs for complex analysis

Emerging trend: SQL-based metrics query languages are gaining traction:
  • InfluxDB Flux (pipe-forward SQL-like)
  • CloudWatch Metrics Insights (SQL-like)
  • VictoriaMetrics MetricsQL (PromQL superset with SQL-like extensions)

The industry may converge on a PromQL + SQL hybrid, but PromQL remains
the de facto standard for the open-source ecosystem.
```

---

## Trade-off 9: Cardinality — Richness vs Cost

### The Core Tension

More labels = richer queries but more time series = higher cost.

### The Cardinality Math

```
metric: http_requests_total
labels:
  method:  [GET, POST, PUT, DELETE, PATCH]     = 5 values
  status:  [200, 201, 301, 400, 401, 403, 404, 500, 502, 503] = 10 values
  service: [api, auth, payments, search, ...] = 20 values
  host:    [web-01, web-02, ..., web-100]     = 100 values

Cardinality = 5 × 10 × 20 × 100 = 100,000 time series
Storage: 100K × 8,640 samples/day × 1.37 bytes = ~1.2 GB/day
Manageable ✓

Now add: endpoint: [/users, /users/{id}, /orders, ...]  = 200 values
Cardinality = 5 × 10 × 20 × 100 × 200 = 20,000,000 time series
Storage: 20M × 8,640 × 1.37 = ~237 GB/day
Expensive but workable ⚠

Now add: user_id: [uuid, uuid, ...]  = 10,000,000 values
Cardinality = 5 × 10 × 20 × 100 × 200 × 10M = 2 × 10^13 time series
Storage: LOL, your monitoring system is now more expensive than your application
NEVER DO THIS ✗
```

### Good Labels vs Bad Labels

```
GOOD labels (bounded, meaningful):
  • method: GET, POST, PUT, DELETE (bounded: ~5 values)
  • status: 200, 400, 500 (bounded: ~10 values)
  • service: api, auth, payments (bounded: ~20-100 values)
  • region: us-east, eu-west (bounded: ~3-5 values)
  • environment: prod, staging, dev (bounded: ~3 values)

BAD labels (unbounded, high-cardinality):
  • user_id: 10M unique values → USE TRACES, not metrics
  • request_id: unique per request → USE TRACES
  • email: unique per user → USE LOGS
  • ip_address: millions of unique values → aggregate in logs
  • full_url_path: /users/abc123 → PARAMETERIZE to /users/{id}

Rule of thumb:
  If a label has >1000 unique values → it probably shouldn't be a metric label.
  Use traces or logs for high-cardinality dimensions instead.
```

---

## Trade-off 10: Metrics vs Logs vs Traces — The Three Pillars

### When to Use Each

```
METRICS tell you WHAT is broken:
  "Error rate is 5%"
  "P99 latency is 2 seconds"
  "CPU usage is 95%"
  → Numeric, pre-aggregated, cheap to store, fast to query
  → Use for: alerting, dashboards, capacity planning, SLOs

LOGS tell you WHY it's broken:
  "NullPointerException at UserService.java:142"
  "Connection refused: database host db-01:5432"
  "Request failed: timeout after 30 seconds"
  → Text, verbose, expensive to store, powerful to search
  → Use for: debugging, root cause analysis, audit trails

TRACES tell you WHERE in a distributed system it's broken:
  "Request spent 2ms in API → 5ms in Auth → 1500ms in Database → 3ms in Cache"
  → Structured, per-request, moderate cost
  → Use for: distributed debugging, latency breakdown, service dependency mapping
```

### Correlation — The Real Power

```
The three pillars are most powerful when correlated:

1. Alert fires: "API error rate > 5%"                      (METRIC)
2. Click on alert → see correlated traces for failing requests  (TRACE)
3. Trace shows: API → Auth (ok, 5ms) → Database (error, timeout) (TRACE)
4. Click on database span → see correlated logs              (LOG)
5. Log shows: "Connection pool exhausted, max connections = 100" (LOG)
6. Root cause: database connection pool too small for current traffic

Without correlation:
  You'd manually search logs for errors around the same time,
  hope to find the right log line among millions,
  guess which service is the culprit.

With correlation:
  Metric → Trace → Log → Root cause in 2 minutes.
```

### OpenTelemetry's Unification

OpenTelemetry [VERIFIED — OpenTelemetry documentation] provides a single instrumentation SDK and protocol (OTLP) for all three signals. Instrument your application once → get metrics, traces, and logs. This is the industry's convergence point — instrument with OTel, send to whatever backend (Datadog, Grafana Cloud, self-hosted OSS stack).

---

## Summary Table

| Trade-off | Option A | Option B | Verdict |
|---|---|---|---|
| Push vs Pull | Push (Datadog) | Pull (Prometheus) | Push for SaaS/firewalls, Pull for k8s/internal. Both with OTel. |
| Single vs Distributed TSDB | Prometheus | Cortex/Mimir | Start single, scale distributed when needed. Thanos as middle ground. |
| Histogram vs Summary | Histograms (aggregatable) | Summaries (exact per-instance) | Histograms for multi-instance services. Summaries rare. |
| In-Memory vs Disk | Gorilla (RAM-only) | Prometheus TSDB (RAM + disk) | Hybrid: hot in memory, cold on disk, archive on S3. |
| Labels vs Flat Names | Prometheus-style labels | StatsD-style flat names | Labels won. Aggregation power outweighs cardinality risk. |
| All-in-One vs Best-of-Breed | Datadog | Prometheus + Grafana + Loki | Datadog for small teams; OSS for large orgs with platform teams. |
| Static vs ML Alerting | Threshold rules | Anomaly detection | Use both: static for absolutes, ML for patterns. |
| PromQL vs SQL | PromQL | SQL for metrics | PromQL for daily use. SQL for ad-hoc analytics. |
| High vs Low Cardinality | Rich labels | Conservative labels | Keep labels bounded. Use traces for high-cardinality dimensions. |
| Metrics vs Logs vs Traces | — | — | All three needed. Correlate for maximum value. |
