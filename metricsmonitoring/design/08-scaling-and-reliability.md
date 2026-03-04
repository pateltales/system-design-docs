# Scaling, Sharding & High Availability

## A Metrics System Must Scale to Millions of Time Series and Billions of Data Points Per Day

The monitoring system is one of the few systems that MUST be more reliable than the systems it monitors. If the application goes down and the monitoring system goes down with it, you're flying blind.

---

## 1. Scale Numbers — How Big Is the Problem?

### Industry Reference Points

| System | Scale | Source |
|---|---|---|
| **Datadog** | Trillions of data points per day, monitors millions of infrastructure components for 28,000+ customers | [PARTIALLY VERIFIED — Datadog investor presentations] |
| **Netflix Atlas** | 1+ billion metrics per minute (~1.5 trillion/day) | [VERIFIED — Netflix Tech Blog "Lessons from Building Observability Tools at Netflix"] |
| **Prometheus (single instance)** | ~1-10 million active time series, ~100K-1M samples/sec ingestion | [VERIFIED — Prometheus documentation and community benchmarks] |
| **Uber** | Billions of metrics per minute across thousands of microservices | [PARTIALLY VERIFIED — Uber Engineering Blog] |
| **Cloudflare** | ~6M distinct metrics per second | [PARTIALLY VERIFIED — Cloudflare Blog] |

### Typical Enterprise Scale

```
Company with 1,000 servers running microservices:

Per host:
  System metrics (CPU, memory, disk, network):     ~50 unique metrics
  Container metrics (10 containers/host):           ~20 metrics × 10 = 200
  Application metrics (per container):              ~100 metrics × 10 = 1,000
  With label combinations (method, status, path):   × 5-50 = 5,000-50,000 series

Per cluster (1,000 hosts):
  Active time series:  5M - 50M
  Ingestion rate:      500K - 5M samples/second
  (at 10-second scrape interval: each series produces 1 sample/10s)

Storage per day (at 10s interval):
  5M series × 8,640 samples/day × 1.37 bytes/sample (Gorilla)
  = ~59 GB/day compressed
  = ~1.8 TB/month

  50M series × 8,640 samples/day × 1.37 bytes/sample
  = ~590 GB/day compressed
  = ~18 TB/month

With rollups (15-day raw, 90-day 1m, 1-year 1h):
  Total storage ≈ 15 days × 590 GB + 75 days × 59 GB + 275 days × 12 GB
  = 8.85 TB + 4.4 TB + 3.3 TB = ~16.5 TB total on disk
```

### Latency Requirements

| Operation | Target | Why |
|---|---|---|
| **Ingestion** (metric emitted → stored) | < 30 seconds | Alert evaluation needs recent data |
| **Dashboard query** (simple, last 1 hour) | < 500ms | Interactive feel |
| **Dashboard query** (complex, last 24 hours) | < 2 seconds | Acceptable wait |
| **Dashboard query** (heavy, last 30 days) | < 5 seconds | Uses rollups |
| **Alert evaluation** (rule checked) | Every 15-60 seconds | Timely detection |
| **Alert notification** (firing → page sent) | < 30 seconds | Rapid response |
| **End-to-end** (metric anomaly → on-call paged) | < 2 minutes | Total detection time |

---

## 2. Write Path Scaling

### The Challenge

The write path must handle millions of samples per second with:
- No data loss (every metric matters for alerting)
- Low latency (alert evaluation needs fresh data)
- Consistent throughput (traffic is 24/7, with daily peaks)

### Horizontal Write Scaling Architecture

```
                    ┌──────────────────────────┐
                    │  Agents / Collectors      │
                    │  (push metrics via HTTPS) │
                    └──────────┬───────────────┘
                               │
                               ▼
                    ┌──────────────────────────┐
                    │  Ingestion Gateway        │
                    │  (load balancer, auth,    │
                    │   validation, routing)    │
                    └──────────┬───────────────┘
                               │
                               ▼
                    ┌──────────────────────────┐
                    │  Kafka (ingestion buffer) │
                    │  Partitioned by:          │
                    │  hash(tenant, metric)     │
                    └──────────┬───────────────┘
                               │
              ┌────────────────┼────────────────┐
              ▼                ▼                ▼
    ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
    │  Ingester #1 │  │  Ingester #2 │  │  Ingester #3 │
    │              │  │              │  │              │
    │ In-memory    │  │ In-memory    │  │ In-memory    │
    │ head block   │  │ head block   │  │ head block   │
    │ + WAL        │  │ + WAL        │  │ + WAL        │
    │              │  │              │  │              │
    │ Replication  │──│ Replication  │──│ Replication  │
    │ factor = 3   │  │ factor = 3   │  │ factor = 3   │
    └──────┬───────┘  └──────┬───────┘  └──────┬───────┘
           │                 │                 │
           ▼                 ▼                 ▼
    ┌──────────────────────────────────────────────┐
    │  Object Storage (S3 / GCS)                   │
    │  Long-term storage of immutable TSDB blocks  │
    └──────────────────────────────────────────────┘
```

### Partitioning Strategy

**Consistent hashing** determines which ingester owns which series:

```
Series: http_requests_total{service="api", host="web-01"}
  → hash(series_labels) = 0xA3F2...
  → Consistent hash ring maps to Ingester #2
  → ALL samples for this series go to Ingester #2

Why consistent hashing:
  • Adding/removing ingesters only moves ~1/N of the series (minimal reshuffling)
  • Each series is always on the same ingester → no coordination for writes
  • Queries for a specific series only hit one ingester (efficient)

Replication:
  Replication factor = 3: each sample is written to 3 ingesters
  Ensures durability even if an ingester crashes before flushing to object storage
  Uses quorum writes: write succeeds if 2 of 3 replicas acknowledge
```

### Ingester Lifecycle

```
Phase 1: JOINING
  New ingester joins the hash ring
  Takes ownership of a portion of the series
  Previous owners stop accepting writes for transferred series
  (No data loss — Kafka retains data for replay)

Phase 2: ACTIVE
  Receives samples from Kafka
  Stores in in-memory head block (Gorilla compression)
  Writes to WAL for durability
  Periodically flushes head block → immutable block → uploads to object storage
  Flush interval: every 2 hours (same as Prometheus)

Phase 3: LEAVING (graceful shutdown)
  Stop accepting new writes
  Flush all in-memory data to object storage
  Transfer hash ring ownership to remaining ingesters
  Shut down

Phase 4: CRASH (ungraceful)
  Ingester dies unexpectedly
  Other ingesters detect via heartbeat timeout
  Hash ring updated — other ingesters take over the dead ingester's series
  Data since last flush: recovered from WAL (if disk survives) or from Kafka replay
  Replicas on other ingesters cover the gap
```

---

## 3. Read Path Scaling

### The Challenge

Read (query) workloads are fundamentally different from writes:
- Writes are uniform and predictable (constant stream of samples)
- Reads are bursty and variable (dashboard refreshes, ad-hoc queries, alert evaluations)
- A single expensive query can consume more resources than 1 million writes

### Distributed Query Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  Query Path                                                  │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  Query Frontend                                       │  │
│  │  • Rate limiting (per tenant)                         │  │
│  │  • Query splitting (time-based, shard-based)          │  │
│  │  • Result caching (Memcached)                         │  │
│  │  • Queue management (fair scheduling across tenants)  │  │
│  └──────────────┬─────────────────────────────────────────┘  │
│                 │                                            │
│                 ▼                                            │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  Querier Pool (stateless, horizontally scalable)      │  │
│  │  • Receives sub-query from frontend                   │  │
│  │  • Queries ingesters (recent data, in-memory)         │  │
│  │  • Queries store gateway (historical, object storage) │  │
│  │  • Merges + deduplicates results                      │  │
│  │  • Evaluates PromQL on merged data                    │  │
│  └──────────────┬───────────────┬─────────────────────────┘  │
│                 │               │                            │
│        ┌────────┘               └────────┐                   │
│        ▼                                 ▼                   │
│  ┌─────────────┐                ┌──────────────────┐        │
│  │  Ingesters  │                │  Store Gateway   │        │
│  │  (recent    │                │  (historical data│        │
│  │   data,     │                │   from S3/GCS)   │        │
│  │   in-memory)│                │                  │        │
│  └─────────────┘                │  • Block index   │        │
│                                 │    cache          │        │
│                                 │  • Chunk cache   │        │
│                                 │  • Lazy loading  │        │
│                                 └──────────────────┘        │
└──────────────────────────────────────────────────────────────┘
```

### Query Splitting Strategies

**Time-based splitting**: Split a 30-day query into 30 one-day sub-queries. Execute in parallel. 29 of 30 hit cache (historical data is immutable). Only 1 queries live data.

**Shard-based splitting**: Split a query across N data shards. Each shard handles a subset of the series. Results are merged at the querier level. This is what Grafana Mimir calls "query sharding."

```
Query: sum(rate(http_requests_total[5m])) — touches 1M series

Without sharding:
  1 querier processes 1M series → 10 seconds, high memory

With 16 shards:
  16 queriers each process ~62,500 series → ~0.6 seconds each
  Frontend merges 16 partial sums → total time ~1 second

Query sharding provides near-linear speedup:
  16 shards → ~10-16x faster queries
```

---

## 4. Thanos Architecture — Scaling Prometheus

Thanos [VERIFIED — Thanos project documentation] extends Prometheus with long-term storage, global querying, and high availability without replacing Prometheus.

```
┌──────────────────────────────────────────────────────────────────┐
│  THANOS ARCHITECTURE                                             │
│                                                                  │
│  ┌─────────────────┐  ┌─────────────────┐                       │
│  │ Prometheus A     │  │ Prometheus B     │  (HA pair — both     │
│  │ (US-East, team1) │  │ (US-East, team1) │   scrape same targets)│
│  │                  │  │                  │                       │
│  │ ┌──────────────┐ │  │ ┌──────────────┐ │                      │
│  │ │Thanos Sidecar│ │  │ │Thanos Sidecar│ │  Sidecars:           │
│  │ │              │ │  │ │              │ │  • Upload blocks to S3│
│  │ │ • Upload     │ │  │ │ • Upload     │ │  • Serve StoreAPI    │
│  │ │   blocks     │ │  │ │   blocks     │ │    for recent data   │
│  │ │ • StoreAPI   │ │  │ │ • StoreAPI   │ │                      │
│  │ └──────────────┘ │  │ └──────────────┘ │                      │
│  └────────┬─────────┘  └────────┬─────────┘                      │
│           │                     │                                │
│           ▼                     ▼                                │
│  ┌──────────────────────────────────────┐                        │
│  │  Object Storage (S3 / GCS)          │                         │
│  │  • Prometheus TSDB blocks           │                         │
│  │  • Immutable, compressed, indexed   │                         │
│  │  • Unlimited retention at low cost  │                         │
│  │  • ~$0.023/GB/month (S3 Standard)   │                         │
│  └──────────────────┬───────────────────┘                        │
│                     │                                            │
│           ┌─────────┼─────────┐                                  │
│           ▼                   ▼                                  │
│  ┌──────────────────┐  ┌──────────────────┐                      │
│  │ Thanos Store     │  │ Thanos Store     │  Store Gateways:     │
│  │ Gateway #1       │  │ Gateway #2       │  • Serve historical  │
│  │                  │  │                  │    data from S3      │
│  │ • Index cache    │  │ • Index cache    │  • Index + chunk     │
│  │ • Chunk cache    │  │ • Chunk cache    │    caching in memory │
│  └────────┬─────────┘  └────────┬─────────┘                      │
│           │                     │                                │
│           ▼                     ▼                                │
│  ┌──────────────────────────────────────┐                        │
│  │  Thanos Querier                     │  Global query layer:    │
│  │                                      │  • Fans out to         │
│  │  • PromQL-compatible API             │    Sidecars (recent)   │
│  │  • Fans out to all StoreAPIs         │    + Store Gateways    │
│  │  • Deduplicates HA pair data         │    (historical)        │
│  │  • Merges results                    │  • Single query        │
│  │  • Partial response on timeout       │    endpoint            │
│  └──────────────────────────────────────┘                        │
│                                                                  │
│  ┌──────────────────────────────────────┐                        │
│  │  Thanos Compactor                   │  Background job:        │
│  │                                      │  • Merges small blocks │
│  │  • Runs against object storage       │    into larger blocks  │
│  │  • Compacts blocks (merge + dedupe)  │  • Downsamples         │
│  │  • Downsamples (5m, 1h rollups)      │    (5m → 1h)          │
│  │  • Deletes expired data              │  • Applies retention   │
│  └──────────────────────────────────────┘                        │
└──────────────────────────────────────────────────────────────────┘
```

### What Thanos Solves

| Problem | Without Thanos | With Thanos |
|---|---|---|
| **Long-term retention** | Prometheus keeps data on local disk (limited) | Unlimited retention on S3/GCS (~$0.023/GB/month) |
| **Global view** | Each Prometheus instance only sees its own targets | Thanos Query fans out to all instances |
| **High availability** | Single Prometheus = single point of failure | HA pairs + deduplication at query time |
| **Downsampling** | Prometheus doesn't downsample | Thanos Compactor creates 5m and 1h rollups |
| **Multi-region** | Prometheus is single-region | Thanos Query aggregates across regions |

### Thanos vs Cortex/Mimir

| Aspect | Thanos | Cortex / Grafana Mimir |
|---|---|---|
| **Architecture** | Sidecar model (extends existing Prometheus) | Replaces Prometheus write/read path |
| **Write path** | Prometheus writes locally → sidecar uploads | Agents remote-write → ingesters |
| **Scaling model** | Scale by adding more Prometheus instances | Scale by adding more ingesters/queriers |
| **Operational complexity** | Lower (keep familiar Prometheus) | Higher (new components to operate) |
| **Multi-tenancy** | Limited (no native tenant isolation) | Built-in (tenant ID in every request) |
| **Best for** | Teams already running Prometheus | New deployments, SaaS/multi-tenant platforms |
| **Used by** | Many open-source adopters | Grafana Cloud (Mimir as managed backend) |

---

## 5. High Availability Strategy

### Why HA Matters More for Monitoring

```
If the payment service goes down:
  → You can't process payments, but you KNOW about it
  → You can investigate, mitigate, communicate to customers

If the monitoring system goes down:
  → You don't know if ANYTHING is down
  → You're flying blind
  → Customer reports are your only signal
  → MTTR increases dramatically

The monitoring system must be MORE reliable than any individual service it monitors.
```

### HA at Every Layer

| Component | HA Strategy | Details |
|---|---|---|
| **Agents** | DaemonSet (auto-restart) | Kubernetes restarts crashed agents; data loss limited to buffer period |
| **Ingestion gateway** | Stateless, multiple replicas + load balancer | Any gateway can handle any request; horizontal scaling |
| **Kafka** | Replication factor 3, min ISR 2 | Data survives 1 broker failure; ISR ensures consistency |
| **Ingesters** | Replication factor 3, consistent hashing | Each sample on 3 ingesters; quorum writes |
| **Object storage (S3/GCS)** | 11 nines durability (built-in) | Cloud provider guarantees; cross-AZ replication |
| **Queriers** | Stateless, multiple replicas | Any querier can handle any query; horizontal scaling |
| **Store gateways** | Multiple replicas with shared cache | Sharded block ownership; cache in Memcached |
| **Query frontend** | Stateless, multiple replicas | Consistent hashing for cache affinity |
| **Alert evaluator** | Active-active or active-standby | Deduplication of notifications (Alertmanager cluster) |
| **Grafana** | Multiple replicas, shared DB | Dashboard definitions in Postgres (replicated) |

### Prometheus HA Pattern

```
Standard HA: Run 2 Prometheus instances scraping the SAME targets

  Prometheus A ──scrape──> App Pod
  Prometheus B ──scrape──> App Pod  (both scrape the same targets)

Both instances collect the same data independently.
If Prometheus A dies, Prometheus B still has the data.

Deduplication: Thanos Query or Mimir querier deduplicates the overlapping data
at query time (using the "replica" external label).

Trade-off:
  • 2x storage cost (two copies of everything)
  • 2x scrape load on targets (two scrapers)
  • But: simple, no coordination needed, zero data loss on single failure
```

---

## 6. Multi-Region Architecture

### Why Multi-Region

```
Single region:
  If us-east-1 has an outage → monitoring for ALL services goes down
  → Worst time to lose monitoring is during a region failure

Multi-region:
  Each region has its own monitoring stack
  Global query layer aggregates across regions
  If us-east-1 fails → eu-west-1 monitoring still works
  → Can still see us-east-1's last-known-good data from replicated storage
```

### Architecture

```
┌──────────────────────────────────────────────────────────┐
│  Region: US-East-1                                       │
│                                                          │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐   │
│  │ Ingesters    │  │ Store GW     │  │ Queriers     │   │
│  │ (write)      │  │ (read S3)    │  │ (query)      │   │
│  └──────┬───────┘  └──────┬───────┘  └──────────────┘   │
│         │                 │                              │
│         ▼                 ▼                              │
│  ┌──────────────────────────────┐                        │
│  │  S3 (us-east-1)             │───── cross-region ─────>│
│  │  Regional metric blocks     │      replication        │
│  └──────────────────────────────┘                        │
└──────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────┐
│  Region: EU-West-1                                       │
│  (identical stack — ingesters, store GW, queriers, S3)   │
└──────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────┐
│  Global Query Layer                                      │
│                                                          │
│  ┌──────────────────────────────────────────────────┐    │
│  │  Global Querier                                  │    │
│  │  • Fans out to US-East queriers + EU-West queriers│   │
│  │  • Merges cross-region results                   │    │
│  │  • Provides a unified view of all regions        │    │
│  └──────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────┘
```

### What Stays Regional vs Global

| Component | Scope | Rationale |
|---|---|---|
| **Ingestion** | Regional | Metrics from us-east apps stay in us-east (low latency) |
| **Storage** | Regional (with cross-region replication) | Primary in-region, replicated for DR |
| **Alert evaluation** | Regional | Alerts for us-east services evaluated in us-east |
| **Dashboard queries** | Regional first, global when needed | Most queries are about one region; cross-region for global dashboards |
| **Grafana** | Global | One Grafana instance with data sources in each region |

---

## 7. Graceful Degradation

### What Happens When Components Fail

| Failure | Impact | Mitigation |
|---|---|---|
| **Agent crash** | Metrics not collected from one host | DaemonSet auto-restarts; gap in data for ~30s |
| **Kafka broker down (1 of 3)** | No impact (replication) | Min ISR = 2 ensures no data loss |
| **Ingester crash** | Lose in-memory data since last flush | WAL recovery + Kafka replay + replication |
| **Object storage latency spike** | Slow historical queries | Return partial results (recent data from ingesters) |
| **Querier overloaded** | Slow dashboard rendering | Auto-scale queriers; drop ad-hoc queries, prioritize alerts |
| **Alert evaluator crash** | Alerts not evaluated | Active-standby failover; Alertmanager cluster gossip |
| **Grafana down** | No dashboards | Multiple replicas; CLI/API access as fallback |
| **Full region outage** | Regional monitoring down | Cross-region replicated data; global query layer |

### Circuit Breaker Pattern

```
Query path circuit breaker:

CLOSED (normal operation):
  Queries flow to store gateway normally
  Track error rate

If error rate > 50% for 30 seconds:
  → OPEN (stop sending queries to failing component)
  Return partial results or cached data
  "Some data may be missing — store gateway is experiencing issues"

After 60 seconds:
  → HALF-OPEN (send a probe query)
  If probe succeeds → CLOSED (resume normal operation)
  If probe fails → OPEN (continue protecting the system)

This prevents a failing store gateway from cascading failures
to the entire query path.
```

---

## 8. Capacity Planning

### Back-of-Envelope Sizing

```
Given:
  10M active time series
  10-second scrape interval
  Gorilla compression: ~1.37 bytes/sample
  Retention: 15 days raw, 90 days 1-min, 1 year 1-hour

Ingestion rate:
  10M series / 10 seconds = 1M samples/second

Storage:
  Raw (15 days):
    1M samples/sec × 86,400 sec/day × 15 days × 1.37 bytes
    = 1.78 TB

  1-min rollups (75 additional days):
    10M series × 1,440 samples/day × 75 days × 5 values × 1.37 bytes
    = 7.4 TB

  1-hour rollups (275 additional days):
    10M series × 24 samples/day × 275 days × 5 values × 1.37 bytes
    = 452 GB

  Total: ~9.6 TB on object storage

Ingesters (in-memory):
  10M series × ~200 bytes/series (metadata + recent chunk)
  = ~2 GB memory per ingester (with 3x replication: 6 GB across cluster)
  + sample data in chunks: ~500 MB per ingester
  → 3-5 ingesters with 4 GB RAM each

Queriers:
  Peak concurrent queries: ~100 (50 dashboard users × 2/min)
  Per query memory: ~100 MB (for series materialization)
  → 5-10 queriers with 4 GB RAM each
  Scale based on peak query load
```

---

## Summary

| Component | Purpose | Key Design Choice |
|---|---|---|
| Write scaling | Handle millions of samples/sec | Consistent hashing to ingesters, Kafka buffer, replication factor 3 |
| Read scaling | Sub-second dashboard queries | Query splitting (time + shard), fan-out, caching, auto-resolution |
| Thanos | Scale Prometheus | Sidecar → S3 → Store Gateway → Global Query |
| Cortex/Mimir | Distributed Prometheus | Replace write/read path, built-in multi-tenancy |
| HA | Survive component failures | Replicate everything, dedup at query, no single point of failure |
| Multi-region | Survive region failures | Regional ingestion/storage, cross-region replication, global query |
| Graceful degradation | Partial service > no service | Circuit breakers, partial results, priority queues |
