# Time-Series Database — The Storage Engine

> General-purpose databases are terrible for metrics. This doc explains why
> and how purpose-built TSDBs solve the problem with Gorilla compression,
> write-ahead logs, immutable blocks, and inverted indexes.

---

## Table of Contents

1. [Why General-Purpose DBs Fail](#1-why-general-purpose-dbs-fail)
2. [Time-Series Data Model](#2-time-series-data-model)
3. [Metric Types](#3-metric-types)
4. [Gorilla Compression](#4-gorilla-compression)
5. [Prometheus TSDB Internals](#5-prometheus-tsdb-internals)
6. [InfluxDB TSM Engine](#6-influxdb-tsm-engine)
7. [Downsampling & Rollups](#7-downsampling--rollups)
8. [Cardinality — The #1 Scaling Challenge](#8-cardinality--the-1-scaling-challenge)
9. [Contrasts](#9-contrasts)

---

## 1. Why General-Purpose DBs Fail

```
MySQL / PostgreSQL for metrics? Here's why it breaks:

WRITE PATTERN:
  Metrics are append-only. Millions of inserts per second.
  No updates, no deletes (except bulk retention-based drops).

  MySQL: each INSERT acquires a row lock, updates the B-tree index,
  writes to the WAL, and updates data pages. At 1M inserts/sec,
  the index becomes the bottleneck — B-tree rebalancing can't keep up.

  TSDB: append-only writes to a WAL + in-memory buffer. No index
  updates on write (index is built during compaction). Writes are
  essentially sequential I/O — the fastest thing a disk can do.

READ PATTERN:
  "Give me CPU usage for all 500 web servers for the last 6 hours."
  This is a time-range scan across 500 series.

  MySQL: scan 500 × 2,160 = 1,080,000 rows (at 10-second intervals).
  Each row is in a B-tree page, potentially scattered on disk.
  Random I/O. Slow.

  TSDB: data for each series is stored contiguously in compressed chunks.
  Read 500 chunks sequentially. Each chunk is compressed (Gorilla) —
  2 hours of data in a few KB. Sequential I/O. Fast.

COMPRESSION:
  Metric values are HIGHLY compressible:
    Timestamps: monotonically increasing with near-constant delta.
    Values: change slowly (CPU 72.5%, 72.6%, 72.4%, 72.8%).

  MySQL: stores each float64 as 8 bytes, each timestamp as 8 bytes.
  No domain-specific compression. 16 bytes per data point.

  TSDB (Gorilla): ~1.37 bytes per data point. 12x compression.
  At 10M series × 1 sample/10s × 86,400s/day:
    MySQL: ~86.4 billion points × 16 bytes = ~1.3 TB/day
    TSDB:  ~86.4 billion points × 1.37 bytes = ~112 GB/day

RETENTION:
  Old metrics must be deleted automatically.

  MySQL: DELETE FROM metrics WHERE timestamp < '2024-01-01'
  → Scans and deletes billions of rows. Lock contention. Table fragmentation.
  → Hours of background work. Degrades read performance.

  TSDB: drop the file containing data before 2024-01-01.
  → Atomic file deletion. Instant. No fragmentation.
```

---

## 2. Time-Series Data Model

```
A time series is uniquely identified by:
  metric_name + sorted set of label/tag key-value pairs

Example:
  http_requests_total{method="GET", status="200", service="api", host="web-01"}

This combination is ONE unique time series.
Each time series is a sequence of (timestamp, value) pairs:
  [(t1, 154382), (t2, 154419), (t3, 154461), ...]

The IDENTITY of a series is its label set.
  Change any label value → a DIFFERENT time series:

  http_requests_total{method="GET", status="200"} ← series A
  http_requests_total{method="GET", status="404"} ← series B (different)
  http_requests_total{method="POST", status="200"} ← series C (different)

CARDINALITY = number of unique time series for a given metric.

  http_requests_total with labels: method, status, service, host

  Cardinality = |methods| × |statuses| × |services| × |hosts|
              = 5 × 10 × 20 × 500
              = 500,000 time series

  Add a label: endpoint (with 100 unique values)
  → 500,000 × 100 = 50,000,000 time series

  This is the CARDINALITY EXPLOSION problem.
  Each series consumes memory, storage, and query resources.
```

---

## 3. Metric Types

```
COUNTER
  Monotonically increasing value. Resets to 0 on process restart.
  Example: http_requests_total, bytes_sent_total

  Raw value is useless (it just goes up).
  Useful as: rate(http_requests_total[5m]) → requests per second.

  Why counter, not gauge for request counts?
    If the agent misses a scrape (network blip), a gauge would lose data.
    A counter remembers the total — you just compute the rate between
    the scrapes you DO have. Counters are resilient to missed scrapes.

GAUGE
  Value that goes up and down. Snapshot of current state.
  Example: cpu_usage_percent, memory_used_bytes, queue_depth

  Unlike counters, gauges can be aggregated directly:
    avg(cpu_usage_percent) across hosts makes sense.
    avg(http_requests_total) across hosts does NOT (it's a running sum).

HISTOGRAM
  Distribution of observed values. Pre-defined bucket boundaries.
  Example: http_request_duration_seconds with buckets [0.01, 0.05, 0.1, 0.5, 1, 5]

  Stored as multiple counters (one per bucket):
    http_request_duration_seconds_bucket{le="0.01"} = 45231  (requests ≤ 10ms)
    http_request_duration_seconds_bucket{le="0.05"} = 98234  (requests ≤ 50ms)
    http_request_duration_seconds_bucket{le="0.1"}  = 112345 (requests ≤ 100ms)
    http_request_duration_seconds_bucket{le="+Inf"} = 122001 (all requests)
    http_request_duration_seconds_sum = 4523.89
    http_request_duration_seconds_count = 122001

  Quantiles computed at query time:
    histogram_quantile(0.95, rate(http_request_duration_seconds_bucket[5m]))
    → P95 latency

  KEY ADVANTAGE: histograms are AGGREGATABLE across instances.
    You CAN merge histogram buckets from 100 instances and compute
    the global P95. This is mathematically valid.

SUMMARY
  Similar to histogram but computes quantiles CLIENT-SIDE.
  Example: http_request_duration_seconds{quantile="0.95"} = 0.234

  KEY DISADVANTAGE: summaries are NOT aggregatable.
    You CANNOT merge P95 from 100 instances to get the global P95.
    avg(P95_per_instance) ≠ P95_global. Mathematically invalid.

  Prometheus documentation recommends histograms over summaries
  for multi-instance services.
```

---

## 4. Gorilla Compression

**VERIFIED — Facebook research paper "Gorilla: A Fast, Scalable, In-Memory Time Series Database" (2015)**

```
The key insight: consecutive data points in a time series are
highly similar. Exploit this similarity for compression.

TIMESTAMP COMPRESSION: Delta-of-Delta encoding

  Raw timestamps (10-second intervals):
    1710512340, 1710512350, 1710512360, 1710512370, 1710512380

  Deltas (difference between consecutive timestamps):
    10, 10, 10, 10

  Delta-of-deltas (difference between consecutive deltas):
    0, 0, 0

  Encoding:
    Delta-of-delta = 0 → encode as a single bit: '0'
    Small delta-of-delta (±63) → encode with a header + 7 bits
    Larger values → encode with header + 9, 12, or 32 bits

  Result: regular time series timestamps compress to ~1-2 BITS per point.
  (vs 64 bits for a full timestamp)

VALUE COMPRESSION: XOR encoding

  Consecutive float64 values (e.g., CPU usage):
    72.5, 72.6, 72.4, 72.8

  In binary (IEEE 754 float64), consecutive values share many
  leading and trailing bits. XOR reveals only the DIFFERING bits:

  XOR(72.5, 72.6) = 0000...0001...0000
                     ^many leading zeros  ^many trailing zeros

  Encoding:
    XOR = 0 (same value as previous) → single bit: '0'
    XOR has same leading/trailing zeros as previous XOR → encode control bit + XOR meaningful bits
    Otherwise → encode leading zeros count + trailing zeros count + meaningful bits

  Result: values compress to ~1-2 BYTES per point.
  (vs 8 bytes for a raw float64)

COMBINED COMPRESSION:
  Timestamp: ~1-2 bits per point
  Value: ~1-2 bytes per point
  Total: ~1.37 bytes per data point [VERIFIED — Gorilla paper]

  Uncompressed: 8 (timestamp) + 8 (value) = 16 bytes per point
  Compression ratio: 16 / 1.37 ≈ 11.7x

  At 10M active series, 1 sample per 10 seconds:
    Uncompressed: 10M × 16 bytes × 6/min × 60 min = ~576 GB/hour
    Gorilla: 10M × 1.37 bytes × 6/min × 60 min = ~49 GB/hour
    Savings: ~527 GB/hour

  Prometheus TSDB, InfluxDB TSM, and Datadog all use variants
  of Gorilla compression.
```

---

## 5. Prometheus TSDB Internals

**VERIFIED — Prometheus documentation and Fabian Reinartz's TSDB design doc**

```
WRITE PATH:

  Sample arrives: (series_labels, timestamp, value)
       │
       ▼
  ┌─────────────────────────┐
  │  Write-Ahead Log (WAL)  │  ← Sequential append for durability
  │  (append-only file)     │     If process crashes, replay WAL
  └──────────┬──────────────┘
             │
             ▼
  ┌─────────────────────────┐
  │  Head Block (in-memory) │  ← Last ~2 hours of data
  │                         │
  │  Series index:          │
  │    labels_hash → series │
  │                         │
  │  Each series has:       │
  │    • Chunk: compressed  │
  │      samples (Gorilla)  │
  │    • ~120 samples/chunk │
  │      (2 hours at 1-min) │
  └──────────┬──────────────┘
             │ Every ~2 hours
             ▼
  ┌─────────────────────────┐
  │  Persistent Block       │  ← Immutable on disk
  │  (on-disk files)        │
  │                         │
  │  Files per block:       │
  │    chunks/              │  ← Compressed time-series data
  │      000001             │
  │    index                │  ← Series → chunk offsets
  │                         │     Label inverted index
  │    meta.json            │  ← Time range, stats
  │    tombstones           │  ← Deleted series markers
  └─────────────────────────┘

COMPACTION:

  Over time, many 2-hour blocks accumulate.
  The compactor merges them into larger blocks:

  [2h block][2h block][2h block] → [6h block]
  [6h block][6h block][6h block][6h block] → [24h block]

  Benefits of larger blocks:
    • Better compression (more data to exploit patterns)
    • Fewer files to open during queries
    • Tombstones applied (deleted data removed physically)

INVERTED INDEX:

  Maps label key-value pairs to series IDs:

  Label: service="api"    → {series_42, series_87, series_153}
  Label: host="web-01"    → {series_42, series_201, series_305}
  Label: method="GET"     → {series_42, series_87, series_201, series_305}

  Query: http_requests_total{service="api", host="web-01"}
    1. Find series with service="api": {42, 87, 153}
    2. Find series with host="web-01": {42, 201, 305}
    3. Intersect: {42}
    4. Fetch chunks for series 42

  This is essentially a search engine inverted index,
  specialized for time-series label queries.

QUERY PATH:

  Query: avg(rate(http_requests_total{service="api"}[5m]))
    1. Use inverted index → find matching series IDs
    2. For each series: read chunk from head block (in-memory)
       or persistent block (disk)
    3. Decompress samples in the [now-5m, now] time range
    4. Compute rate() per series
    5. Compute avg() across all series
    6. Return result

  Recent data (last 2 hours): served from memory → fast (<10ms)
  Older data: served from disk blocks → slower (~50-200ms)
```

---

## 6. InfluxDB TSM Engine

```
TSM (Time-Structured Merge tree):
  Similar to an LSM tree but optimized for time-series workloads.

WRITE PATH:
  Sample arrives
       │
       ▼
  ┌──────────┐     ┌──────────────┐
  │   WAL    │ AND │  In-Memory   │
  │ (disk,   │     │  Cache       │
  │  durabil.)│     │  (sorted by  │
  └──────────┘     │  series+time)│
                   └──────┬───────┘
                          │ periodic flush
                          ▼
                   ┌──────────────┐
                   │  TSM File    │
                   │  (on disk)   │
                   │              │
                   │  Sorted by:  │
                   │  measurement │
                   │  + tag set   │
                   │  + time      │
                   │              │
                   │  Compressed  │
                   │  (Gorilla    │
                   │   variant)   │
                   └──────────────┘

COMPACTION:
  Multiple small TSM files → merge into larger TSM files.
  Similar to LSM tree compaction levels.
  Each level: fewer, larger, better-compressed files.

RETENTION POLICIES:
  Data is partitioned into "shards" by time range.
  Each shard covers a fixed time window (e.g., 1 day, 7 days).
  Retention policy: "delete data older than 30 days."
  Implementation: drop the entire shard file. Instant.

CONTINUOUS QUERIES:
  Automatic downsampling:
    CREATE CONTINUOUS QUERY "cq_5m" ON "mydb"
    BEGIN
      SELECT mean("value") INTO "downsampled"."cpu_5m"
      FROM "cpu" GROUP BY time(5m), *
    END

  This runs every 5 minutes, computing the average CPU
  and storing it in a separate retention policy.

Key difference from Prometheus TSDB:
  InfluxDB supports writes from multiple clients (distributed write path).
  Prometheus TSDB is single-writer only.
  InfluxDB has built-in retention policies and continuous queries.
  Prometheus relies on external tools (Thanos compactor) for these.
```

---

## 7. Downsampling & Rollups

```
Raw data is expensive to store and slow to query over long time ranges.
Downsampling trades resolution for efficiency.

Strategy:
  ┌────────────────────────────────────────────────────────────┐
  │ Data Age        │ Resolution │ Points/Day/Series │ Storage │
  ├────────────────────────────────────────────────────────────┤
  │ 0-15 days       │ Raw (10s)  │ 8,640            │ ~12 KB  │
  │ 15-90 days      │ 1 minute   │ 1,440            │ ~2 KB   │
  │ 90-365 days     │ 5 minutes  │ 288              │ ~400 B  │
  │ 365+ days       │ 1 hour     │ 24               │ ~33 B   │
  └────────────────────────────────────────────────────────────┘

  Storage per series per year:
    Raw only: 8,640 × 365 × 1.37 bytes = ~4.3 MB
    With rollups: ~15 days raw + rollups ≈ 250 KB
    Savings: ~17x

For each rollup interval, store FIVE aggregates:
  min, max, avg, sum, count

  Why all five?
    • "What was the peak CPU last month?" → max
    • "What was the average CPU last month?" → avg = sum / count
    • "What was the minimum CPU last month?" → min
    • "How many data points were aggregated?" → count
    • "What was the total request count?" → sum (for counters)

  Storing all five is cheap and enables any query on historical data
  without going back to raw data.

Auto-resolution:
  The query engine automatically selects resolution based on:
    • Query time range
    • Dashboard panel pixel width

  Example: 500px-wide chart showing 1 year of data
    Ideal: ~500 data points (1 per pixel)
    1 year = 365 days = 8,760 hours
    → 1-hour rollups give 8,760 points (close enough)
    → No need for 10-second raw data (3.15M points!)

  Query for "last 1 hour": use raw data (360 points at 10s)
  Query for "last 24 hours": use 1-minute rollups (1,440 points)
  Query for "last 30 days": use 5-minute rollups (8,640 points)
  Query for "last 1 year": use 1-hour rollups (8,760 points)
```

---

## 8. Cardinality — The #1 Scaling Challenge

```
Cardinality = number of unique time series.

Each unique combination of metric_name + label values = one series.

Why high cardinality is deadly:

  1. MEMORY: each active series has an in-memory entry
     (~1-2 KB per series for metadata + recent chunk).
     10M series × 2 KB = 20 GB of RAM just for series metadata.
     100M series = 200 GB → OOM.

  2. STORAGE: each series has a chunk file.
     100M series = 100M small files (or entries in a large file).
     File system pressure, compaction overhead.

  3. QUERY: a query like sum(metric) by (user_id)
     with 10M unique user_ids materializes 10M result series.
     The query engine allocates memory for each → OOM.

Common mistakes:

  BAD: http_requests{user_id="abc123"}
    If you have 10M users → 10M series PER metric.
    10 metrics × 10M users = 100M series. System dies.

  BAD: http_requests{request_id="req_xyz789"}
    Request IDs are unique per request → unbounded cardinality.
    System dies within minutes.

  GOOD: http_requests{method="GET", status="200", service="api"}
    5 methods × 10 statuses × 20 services = 1,000 series. Manageable.

  Rule of thumb: labels should have LOW cardinality.
    Good labels: method (5 values), status (10), env (3), region (5)
    Bad labels: user_id (millions), request_id (unbounded), email, IP address

  High-cardinality data belongs in TRACES or LOGS, not metrics.
    Metrics: "how many 500 errors per service?" (low cardinality)
    Traces: "which user_id hit the 500 error?" (high cardinality)

Mitigations:
  1. Cardinality limits: reject metrics with >100K series at ingestion.
  2. Metric relabeling: drop high-cardinality labels at ingestion
     (e.g., drop the "pod_name" label if it changes on every restart).
  3. Education: teach developers which labels are appropriate.
  4. Cardinality dashboards: show teams their cardinality usage.
  5. Alerts on cardinality: warn when a metric's cardinality grows
     unexpectedly (e.g., doubles in a day).
```

---

## 9. Contrasts

### Prometheus TSDB vs InfluxDB TSM vs Facebook Gorilla

| Aspect | Prometheus TSDB | InfluxDB TSM | Facebook Gorilla |
|---|---|---|---|
| **Storage model** | In-memory head + disk blocks | WAL + in-memory cache + TSM files | In-memory only |
| **Compression** | Gorilla variant | Gorilla variant | Original Gorilla |
| **Retention** | Block deletion (whole blocks) | Shard deletion (time-partitioned) | 26 hours (memory limit) |
| **Horizontal scaling** | No (single-node) | Yes (InfluxDB Clustered) | No (in-memory, single-node) |
| **Write model** | Single-writer (one Prometheus) | Multi-writer | Multi-writer |
| **Compaction** | Background (merge blocks) | Background (merge TSM files) | N/A (in-memory only) |
| **Best for** | Self-hosted monitoring at moderate scale | IoT, high-write time-series at scale | Ultra-low-latency recent data |

### TSDB vs General-Purpose DB for Metrics

| Aspect | Purpose-Built TSDB | MySQL / PostgreSQL |
|---|---|---|
| **Write throughput** | Millions of points/sec | Thousands of inserts/sec |
| **Compression** | ~12x (Gorilla) | ~1x (no domain-specific compression) |
| **Time-range queries** | Optimized (contiguous chunks) | Slow (B-tree random I/O) |
| **Retention deletion** | Drop file (instant) | DELETE rows (hours, lock contention) |
| **Index** | Inverted index on labels | B-tree on primary key |
| **Query language** | PromQL, Flux (metric-specific) | SQL (general-purpose) |
| **When to use SQL** | Never for high-volume metrics | When metrics volume is tiny (<1000 series) or you need JOINs with other relational data |
