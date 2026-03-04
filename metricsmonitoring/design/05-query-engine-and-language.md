# Query Engine, PromQL & Aggregation

## How Users Query Metrics — The Query Language, Execution Engine, and Aggregation Pipeline

The query engine is where the monitoring system's value is realized. All the effort of collection, ingestion, and storage is wasted if users can't quickly and expressively ask questions about their data.

---

## 1. PromQL — The De Facto Standard

PromQL (Prometheus Query Language) [VERIFIED — Prometheus documentation] is the most widely adopted open-source query language for metrics. Understanding PromQL deeply is essential because even non-Prometheus systems (Thanos, Cortex/Mimir, Grafana, Datadog) support it or a PromQL-compatible dialect.

### Core Concepts: Vectors

PromQL operates on two fundamental data types:

**Instant vector**: A set of time series, each with a single (most recent) sample value.

```
http_requests_total{method="GET", status="200"}
→ Returns: {method="GET", status="200", instance="web-01"} 10542
            {method="GET", status="200", instance="web-02"} 8931
```

**Range vector**: A set of time series, each with a range of samples over a time window.

```
http_requests_total{method="GET"}[5m]
→ Returns: {method="GET", instance="web-01"} [(t1,v1), (t2,v2), ..., (tn,vn)]
           {method="GET", instance="web-02"} [(t1,v1), (t2,v2), ..., (tn,vn)]

The [5m] suffix means: "give me all samples from the last 5 minutes"
```

**Why this distinction matters**: Range vectors are required for functions like `rate()` — you need multiple samples to compute a rate. You can't take the rate of a single data point.

### Essential PromQL Functions

#### rate() — The Most Important Function

```
rate(http_requests_total[5m])

What it does:
  1. Takes the range vector (last 5 minutes of counter samples)
  2. Computes: (last_value - first_value) / (last_timestamp - first_timestamp)
  3. Returns: per-second rate of increase

Why it exists:
  Raw counter values are useless for display. A counter value of 1,542,387
  means nothing by itself. rate() converts it to "247 requests/second" —
  which is actionable.

Counter reset handling:
  If the counter resets (value decreases), rate() detects this and adjusts.
  Example: values [100, 150, 200, 50, 100] — the drop from 200→50 is a
  restart. rate() accounts for the reset, not treating it as a decrease.
```

#### irate() — Instantaneous Rate

```
irate(http_requests_total[5m])

Difference from rate():
  rate()  = average rate over the entire window (smooth, stable)
  irate() = rate between the last two data points only (spiky, responsive)

When to use which:
  rate()  → dashboards (smooth line charts, less noise)
  irate() → alerting (detect sudden spikes that rate() would average out)
```

#### histogram_quantile() — Percentiles from Histograms

```
histogram_quantile(0.95, rate(http_request_duration_seconds_bucket[5m]))

What it does:
  1. Takes the histogram buckets (each bucket is a counter)
  2. Computes rate() on each bucket counter
  3. Interpolates to find the value at the 95th percentile

This is how you compute P95 latency from Prometheus histograms.
The result is an approximation — accuracy depends on bucket boundaries.
Too few buckets = inaccurate. Too many buckets = high cardinality.
```

### Aggregation Operators

```
sum(rate(http_requests_total[5m])) by (service)
→ Total request rate per service (aggregate across all instances)

avg(node_cpu_usage) by (instance)
→ Average CPU per instance

max(container_memory_usage_bytes) by (namespace)
→ Peak memory per Kubernetes namespace

count(up == 1) by (job)
→ Number of healthy instances per job

topk(5, rate(http_requests_total[5m]))
→ Top 5 time series by request rate

quantile(0.99, rate(http_request_duration_seconds_sum[5m])
  / rate(http_request_duration_seconds_count[5m]))
→ P99 of average request latency across all instances
```

**Aggregation is what makes labels powerful**: Without labels and aggregation, you'd need a separate metric for each `(service, host, status)` combination. With labels, you define one metric and aggregate across any dimension at query time.

### Binary Operators (Arithmetic Between Series)

```
Error rate as percentage:
  (rate(http_requests_total{status=~"5.."}[5m])
   / rate(http_requests_total[5m])) * 100
→ "What percentage of requests are 5xx errors?"

Saturation:
  container_memory_usage_bytes / container_spec_memory_limit_bytes * 100
→ "What percentage of memory limit is being used?"

Label matching:
  When dividing two series, PromQL matches them by label set.
  The numerator and denominator must have the same labels
  (or use `on()` / `ignoring()` to specify matching rules).
```

### Subquery (PromQL Advanced Feature)

```
max_over_time(rate(http_requests_total[5m])[1h:1m])

What this does:
  1. Compute rate() at 1-minute intervals over the last hour
  2. Take the max of those rates

Use case: "What was the peak request rate in the last hour?"
Without subquery, you'd need to pre-compute this.
```

---

## 2. Query Execution Pipeline

### Step-by-Step Execution

```
User query:
  avg(rate(http_requests_total{service="api"}[5m])) by (host)

Step 1: PARSE
  ┌─── avg() by (host) ───┐
  │  ┌─── rate() ───────┐  │
  │  │  ┌─── selector ┐ │  │
  │  │  │ metric_name= │ │  │
  │  │  │ "http_req.." │ │  │
  │  │  │ service="api"│ │  │
  │  │  │ [5m]         │ │  │
  │  │  └──────────────┘ │  │
  │  └───────────────────┘  │
  └─────────────────────────┘

  Parse the query string into an Abstract Syntax Tree (AST).
  The AST represents the nested structure of operations.

Step 2: SERIES SELECTION (using inverted index)
  Lookup in the inverted index:
    __name__ = "http_requests_total" → {series 1, 5, 12, 42, 87, ...}
    service = "api"                 → {series 5, 12, 42, 99, 153, ...}
    INTERSECTION                    → {series 5, 12, 42}

  Result: 3 matching series (e.g., one per host)

Step 3: FETCH SAMPLES (time range scan)
  For each matching series, fetch samples from [now - 5m, now]:
    series_5:  [(t1, 1000), (t2, 1015), (t3, 1032), ...]
    series_12: [(t1, 5420), (t2, 5455), (t3, 5490), ...]
    series_42: [(t1, 820),  (t2, 838),  (t3, 855), ...]

  Data source priority:
    1. In-memory head block (most recent ~2 hours) — fastest
    2. On-disk persistent blocks (older data) — needs I/O
    3. Object storage (historical data) — slowest, needs network

Step 4: APPLY FUNCTION — rate()
  For each series, compute per-second rate:
    series_5:  rate = (1032 - 1000) / (t3 - t1) = ~1.07/s
    series_12: rate = (5490 - 5420) / (t3 - t1) = ~2.33/s
    series_42: rate = (855 - 820)  / (t3 - t1) = ~1.17/s

Step 5: APPLY AGGREGATION — avg() by (host)
  Group by the "host" label and average:
    host="web-01" (series_5):  avg = 1.07/s
    host="web-02" (series_12): avg = 2.33/s
    host="web-03" (series_42): avg = 1.17/s

Step 6: RETURN
  Return as a set of time series (one per host) with the computed values.
  For range queries (/api/v1/query_range), this is repeated at each
  evaluation step across the requested time range.
```

### Range Queries vs Instant Queries

**Instant query** (`/api/v1/query`): Evaluate at a single point in time → returns one value per series.

**Range query** (`/api/v1/query_range`): Evaluate at multiple points across a time range.

```
GET /api/v1/query_range?
  query=rate(http_requests_total[5m])
  &start=2024-01-01T00:00:00Z
  &end=2024-01-01T01:00:00Z
  &step=60s

This evaluates the query at 61 points (one per minute for 1 hour).
Each evaluation repeats the full pipeline (select series, fetch, compute).

Optimization: the query engine shares series selection and sample fetching
across evaluation steps — it fetches all needed samples once, then evaluates
the function at each step.
```

---

## 3. Query Performance Challenges

### Challenge 1: High Cardinality Queries

```
DANGEROUS:
  sum(http_requests_total) by (user_id)

Why dangerous:
  If you have 10 million unique user_ids, this query materializes
  10 million time series in memory. Even if each series is small,
  the metadata overhead (label sets, hash maps) can cause OOM.

Protection mechanisms:
  1. Max series per query limit (e.g., reject if >100,000 series)
  2. Query cost estimation BEFORE execution
     → Estimate series count from the inverted index
     → If too high, reject with "query would touch N series, limit is M"
  3. Query timeout (e.g., 60 seconds max)
  4. Per-tenant query concurrency limits
```

### Challenge 2: Long Time Range Queries

```
"Show me CPU for the last 6 months"

At 10-second granularity:
  6 months × 30 days × 24 hours × 3600 seconds / 10 = ~15.5 million samples per series
  × 100 hosts = 1.55 billion samples to scan

Solution: AUTOMATIC RESOLUTION SELECTION
  The query engine selects the appropriate data resolution based on:
  • Query time range
  • Display resolution (panel pixel width)

  6-month chart on a 500-pixel-wide panel:
    500 pixels → need at most 500 data points
    Use 1-hour rollups: 6 × 30 × 24 = 4,320 points → downsample to 500
    Instead of scanning 1.55 billion raw samples, scan 4,320 rollup values

  This is called "auto-resolution" or "step alignment."
  Grafana does this automatically — it computes the step size based on
  the panel width and sends it in the query_range request.
```

### Challenge 3: Fan-Out in Distributed Systems

```
If metric data is sharded across 50 storage nodes:

  ┌────────────────┐
  │  Query Frontend │
  │                │
  │  1. Receive    │
  │     query      │
  │  2. Determine  │
  │     shards     │
  │  3. Fan out    │
  └───┬──┬──┬──┬───┘
      │  │  │  │
      ▼  ▼  ▼  ▼
  ┌──┐┌──┐┌──┐┌──┐   ... (50 shards)
  │S1││S2││S3││S4│
  └──┘└──┘└──┘└──┘

  4. Wait for all responses (with timeout)
  5. Merge and aggregate results
  6. Return to user

Challenges:
  • Tail latency: P99 of 50 parallel requests is worse than P99 of 1
    If each shard has P99 = 50ms, the merged P99 ≈ 200ms
  • Partial failure: if shard S7 is down, do we return partial results
    or an error? → Return partial results with a warning
  • Double-counting: if data is replicated, the merge step must deduplicate
    (Thanos handles this with deduplication by replica label)
```

### Challenge 4: Recording Rules (Pre-Computation)

For frequently-used expensive queries, pre-compute the result:

```yaml
# Prometheus recording rule
groups:
  - name: api_rules
    interval: 30s
    rules:
      - record: job:http_requests:rate5m
        expr: sum(rate(http_requests_total[5m])) by (job)

      - record: job:http_errors:ratio5m
        expr: |
          sum(rate(http_requests_total{status=~"5.."}[5m])) by (job)
          / sum(rate(http_requests_total[5m])) by (job)
```

**What recording rules do**: Evaluate the expression every 30 seconds and store the result as a new time series (`job:http_requests:rate5m`). Dashboards and alerts query the pre-computed series instead of re-evaluating the expensive expression each time.

**When to use**: High-cardinality aggregations that are queried frequently (e.g., on the main ops dashboard that 50 engineers refresh every minute). Without recording rules, each dashboard refresh re-executes the query against raw data.

**Convention**: Pre-computed metric names use colons as separators (`level:metric:operation`) to distinguish them from raw metrics (which use underscores).

---

## 4. Query Language Comparison

### PromQL vs Datadog Query Language vs SQL

| Aspect | PromQL | Datadog Query | SQL (for metrics) |
|---|---|---|---|
| **Paradigm** | Functional (nested functions) | Method chaining | Declarative (SELECT/FROM/WHERE) |
| **Example: request rate** | `rate(http_requests_total[5m])` | `sum:http.requests{*}.as_rate()` | `SELECT rate(value) FROM http_requests WHERE time > now()-5m` |
| **Example: error percentage** | `sum(rate(errors[5m])) / sum(rate(total[5m])) * 100` | `(sum:http.errors{*} / sum:http.requests{*}) * 100` | `SELECT (SUM(errors)/SUM(total))*100 FROM metrics GROUP BY time(5m)` |
| **Type safety** | Strong (instant vs range vectors) | Loose | Standard SQL types |
| **Rate computation** | `rate()`, `irate()` — first-class | `.as_rate()` — method call | Manual (window functions, LAG) |
| **Label matching** | Built-in (`on()`, `ignoring()`) | Automatic by tag | JOIN syntax |
| **Joins** | Limited (label matching only) | Limited | Full JOIN support |
| **Subqueries** | Supported (`[1h:1m]` syntax) | Not supported | Full subquery support |
| **Learning curve** | Moderate (unique syntax, strict typing) | Low (intuitive chaining) | Low (widely known) |
| **Ecosystem** | De facto standard (open-source) | Proprietary (Datadog users) | Universal but verbose |

### InfluxQL and Flux (InfluxDB)

**InfluxQL**: SQL-like language specifically for time-series data. Feels familiar to SQL users but has time-series-specific extensions (GROUP BY time(), fill policies for missing data).

```sql
-- InfluxQL
SELECT mean("cpu_usage") FROM "system"
WHERE "host" = 'web-01' AND time > now() - 1h
GROUP BY time(5m)
```

**Flux**: InfluxDB's newer functional language (pipe-forward syntax). More expressive than InfluxQL but steeper learning curve.

```
// Flux
from(bucket: "system")
  |> range(start: -1h)
  |> filter(fn: (r) => r.host == "web-01" and r._field == "cpu_usage")
  |> aggregateWindow(every: 5m, fn: mean)
```

**Industry consensus**: PromQL has won the open-source ecosystem. Even InfluxDB users often run Grafana in front, which uses PromQL for dashboards. Datadog's proprietary syntax is used only within Datadog.

---

## 5. Query Engine Architecture for Scale

### Thanos Query Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  Thanos Query                                                │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  Query Frontend                                       │  │
│  │  • Accepts PromQL queries via HTTP API                │  │
│  │  • Splits long time ranges into sub-queries           │  │
│  │  • Caches results (per-query or per-step)             │  │
│  │  • Rate limits concurrent queries per tenant          │  │
│  └──────────────┬─────────────────────────────────────────┘  │
│                 │                                            │
│                 ▼                                            │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  Query Engine (Thanos Querier)                        │  │
│  │  • Receives sub-query                                 │  │
│  │  • Fans out to StoreAPIs (Sidecars + Store Gateways)  │  │
│  │  • Merges results from multiple sources               │  │
│  │  • Deduplicates (for HA Prometheus pairs)             │  │
│  │  • Applies PromQL evaluation on merged data           │  │
│  └──────────────┬─────────────────────────────────────────┘  │
│                 │                                            │
│        ┌────────┼────────┐                                   │
│        ▼        ▼        ▼                                   │
│  ┌──────┐ ┌──────┐ ┌──────────┐                             │
│  │Sidecar│ │Sidecar│ │Store     │                            │
│  │(Prom1)│ │(Prom2)│ │Gateway   │                            │
│  │recent │ │recent │ │(S3 data) │                            │
│  │data   │ │data   │ │historical│                            │
│  └──────┘ └──────┘ └──────────┘                             │
└──────────────────────────────────────────────────────────────┘
```

### Cortex/Mimir Query Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Cortex / Grafana Mimir                                     │
│                                                             │
│  Query Frontend                                             │
│  ├── Splits query by time (24h chunks)                      │
│  ├── Splits query by shard (hash of series labels)          │
│  ├── Results cache (Memcached/Redis)                        │
│  └── Queue per tenant (fair scheduling)                     │
│          │                                                  │
│          ▼                                                  │
│  Querier Pool (stateless, horizontally scalable)            │
│  ├── Receives sub-query from frontend                       │
│  ├── Queries ingesters (recent data, in-memory)             │
│  ├── Queries store-gateway (historical data, object storage)│
│  ├── Merges and deduplicates                                │
│  └── Evaluates PromQL                                       │
│          │              │                                   │
│          ▼              ▼                                   │
│  ┌─────────────┐ ┌──────────────┐                           │
│  │  Ingesters  │ │ Store Gateway│                           │
│  │  (recent    │ │ (S3/GCS      │                           │
│  │   in-memory)│ │  blocks)     │                           │
│  └─────────────┘ └──────────────┘                           │
└─────────────────────────────────────────────────────────────┘
```

**Key optimization in Mimir**: Query sharding. A single query like `sum(rate(http_requests[5m]))` across 1 million series is split into N sub-queries (e.g., 16 shards), each handling ~62,500 series. The sub-queries execute in parallel across querier instances, and results are merged at the frontend. This provides near-linear query performance scaling with the number of querier pods.

---

## 6. Caching Strategies

### Multi-Level Cache

```
Level 1: QUERY RESULT CACHE (Query Frontend)
  Cache key: hash(query_string + time_range + step)
  TTL: 30-60 seconds for recent data, longer for historical
  Hit rate: ~60-80% for dashboard queries (same dashboards refreshed repeatedly)
  Backend: Memcached or Redis

Level 2: CHUNK CACHE (Store Gateway / Querier)
  Cache key: block_id + series_id + chunk_id
  Caches decompressed chunks from object storage
  Avoids repeated S3/GCS reads for the same data
  Backend: Memcached (large capacity needed)

Level 3: METADATA CACHE (Store Gateway)
  Cache key: block_id + index_type
  Caches block index and postings (inverted index)
  Critical for fast series selection
  Backend: Memcached

Level 4: OS PAGE CACHE (TSDB on disk)
  Frequently accessed TSDB blocks stay in OS page cache
  Free — just needs enough RAM on the storage nodes
```

### Cache Invalidation

Time-series data has a natural invalidation strategy: data is immutable once written. A sample at timestamp T with value V will never change. This means:

- **Historical query results**: Can be cached indefinitely (the past doesn't change)
- **Recent query results**: Short TTL (new data is still being ingested)
- **Dashboard optimization**: For a 6-month dashboard, only the "last hour" portion changes — cache the first 5 months, 29 days, and 23 hours indefinitely

This is why time-series query caching is much more effective than general-purpose query caching — immutable historical data means high cache hit rates.

---

## 7. Query Safety and Governance

### Protecting the System from Dangerous Queries

A single bad query can take down the entire monitoring system:

```
DANGER LEVEL 1 — High cardinality aggregation:
  sum(metric) by (request_id)
  → Millions of unique series → OOM

DANGER LEVEL 2 — Unbounded time range:
  rate(metric[365d])
  → Scans an entire year of data → disk I/O saturation

DANGER LEVEL 3 — Cross-product join:
  metric_a * metric_b  (with no label matching)
  → Cartesian product of series → exponential explosion

DANGER LEVEL 4 — Expensive regex:
  {__name__=~".*"}
  → Matches EVERY metric → scans entire TSDB
```

### Protection Mechanisms

| Mechanism | What It Does |
|---|---|
| **Max series limit** | Reject queries that would touch >100K series |
| **Query timeout** | Kill queries after 60s (configurable) |
| **Concurrency limit** | Max N concurrent queries per tenant |
| **Cost estimation** | Estimate query cost (series × time range × functions) before execution; reject if too expensive |
| **Query audit log** | Log all queries with cost metrics for review |
| **Slow query log** | Flag queries that take >5s for optimization |
| **Rate limiting** | Max N queries per minute per tenant |

### Query Cost Estimation (Pre-Flight Check)

```
Before executing a query:
  1. Parse the query → extract selectors
  2. Use inverted index to estimate matching series count
  3. Estimate data volume: series_count × time_range / scrape_interval
  4. Estimate compute cost: data_volume × function_complexity
  5. If estimated_cost > threshold → reject with explanation

This prevents the query from ever hitting the storage layer.
It's like a SQL EXPLAIN plan but for PromQL.
```

---

## Summary

| Component | Purpose | Key Insight |
|---|---|---|
| PromQL | Standard query language | Functional, type-safe, built for time-series |
| Instant vs Range vectors | Two fundamental data types | Range vectors needed for rate/increase/delta |
| rate() | Convert counters to rates | The most important PromQL function |
| Query pipeline | Parse → Select → Fetch → Compute → Aggregate | Inverted index enables fast series selection |
| Recording rules | Pre-compute expensive queries | Essential for high-cardinality aggregations |
| Auto-resolution | Match data granularity to display | 1-hour rollups for 6-month charts |
| Fan-out queries | Distributed query execution | Parallel sub-queries, merge, deduplicate |
| Query safety | Protect system from bad queries | Max series, timeout, cost estimation |
