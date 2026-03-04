# Deep Dive: View Ingestion & Write Path

> Companion document to [01-interview-simulation.md](01-interview-simulation.md) — Phase 5.
> This is the most architecturally interesting part of the View Counter system.

---

## 1. The Core Problem

YouTube processes approximately **800,000 view events per second** at peak, with an average of ~500K/sec. A single viral video (BTS premiere, World Cup final, breaking news) can receive **millions of views per minute**.

Writing each view directly to a database is impossible at this scale:
- A typical MySQL instance handles ~10K writes/sec
- 800K writes/sec would require 80+ MySQL instances just for counters
- A viral video creates a **hot row** — millions of concurrent `UPDATE` statements on the same row, serialized by row-level locks

This is a **write-heavy, read-light** system — the opposite of most web applications. The read path (return a cached integer) is trivial. The write path is where all the complexity lives.

---

## 2. Event Streaming Architecture

### Why Kafka?

The fundamental design principle: **decouple ingestion from processing.**

The client doesn't need confirmation that the count was updated — it just needs to know the event was received. This makes the entire system asynchronous.

```
┌──────────┐     ┌──────────────┐     ┌─────────────────┐     ┌──────────────┐     ┌─────────────┐
│  Client   │────→│ API Gateway  │────→│  Kafka Cluster  │────→│  Consumers   │────→│  Data Stores│
│ (Player)  │     │ (validate,   │     │  (view-events   │     │ (aggregate,  │     │ (Redis,     │
│           │     │  202 Accept) │     │   topic)        │     │  flush)      │     │  Cassandra) │
└──────────┘     └──────────────┘     └─────────────────┘     └──────────────┘     └─────────────┘
```

**Why Kafka specifically?**

| Benefit | Explanation |
|---------|-------------|
| **Absorbs write spikes** | Kafka can handle millions of events/sec per cluster. A viral video spike doesn't cause backpressure to clients |
| **Decouples producers/consumers** | API servers don't know about downstream processing. Consumers can be added, removed, or restarted independently |
| **Durability** | Events are persisted to disk and replicated across brokers (`acks=all`, RF=3). No data loss |
| **Replay capability** | If a consumer crashes, it replays from its last committed offset. Events are reprocessed automatically |
| **Multiple consumers** | The same event stream feeds both the counter pipeline and the analytics pipeline (separate consumer groups) |
| **Ordering guarantees** | Partitioning by `hash(videoId)` ensures all events for a given video arrive at the same partition, in order |

### Kafka Topic Design

**Topic:** `view-events`

| Setting | Value | Rationale |
|---------|-------|-----------|
| Partitions | 256 | 800K events/sec ÷ 256 = ~3,125/partition. Each consumer handles ~10K/sec easily. Room to grow |
| Replication Factor | 3 | Survives 2 broker failures. Standard for critical data |
| `acks` | `all` | Wait for all ISR replicas before acknowledging. Durability over latency |
| Retention | 7 days | Allows replay for recovery and reconciliation. Archived to S3 after |
| Compression | LZ4 | Best throughput/compression ratio for real-time pipelines |
| `max.message.bytes` | 1 MB | View events are ~200 bytes; this is generous headroom |

**Partition key: `hash(videoId)` vs `hash(region)`**

| Strategy | Pros | Cons |
|----------|------|------|
| **hash(videoId)** | All events for one video on one partition → easy per-video aggregation. Ordering per video | Hot video = hot partition. One consumer handles all events for a viral video |
| **hash(region)** | Even distribution (regions have similar traffic). No hot partitions | Events for one video spread across partitions. Aggregation requires cross-partition coordination |
| **Hybrid** | hash(videoId) for most videos, sub-partition hot videos across multiple partitions | More complex routing logic. Need hot-key detection |

**Recommendation:** Start with `hash(videoId)` for simplicity. Add sub-partitioning for detected hot keys.

### Consumer Groups

```
                                    ┌───────────────────────┐
                                    │  Consumer Group 1:    │
                              ┌────→│  COUNTER CONSUMERS    │────→ Redis (INCRBY)
                              │     │  (aggregate + flush)  │────→ Cassandra (append)
┌─────────────────┐           │     └───────────────────────┘
│  Kafka Topic    │───────────┤
│  view-events    │           │     ┌───────────────────────┐
│  (256 parts)    │           └────→│  Consumer Group 2:    │
└─────────────────┘                 │  ANALYTICS CONSUMERS  │────→ Flink → ClickHouse
                                    │  (windowed aggregation)│
                                    └───────────────────────┘
```

- **Counter consumers:** Simple logic — aggregate counts per videoId in memory, flush to Redis every 5 seconds.
- **Analytics consumers:** Feed events into Apache Flink for time-windowed aggregation (per-minute, per-hour rollups).
- **Why two groups?** Different processing needs, different scaling profiles, independent failure domains.

---

## 3. Counting Strategies

### Strategy 1: Naive — Increment Counter Per Event

```sql
-- For every view event:
UPDATE videos SET view_count = view_count + 1 WHERE video_id = ?;
```

**Why it fails:**

| Problem | Impact |
|---------|--------|
| 800K writes/sec to DB | Exceeds capacity of any single database |
| Row-level lock contention | Viral video = millions of concurrent UPDATEs on same row. All serialize |
| No buffering | If DB is slow/down, writes fail immediately |
| Write amplification | Each 200-byte event triggers a full row write + WAL entry + index update |

**When it's acceptable:** Low-traffic systems (< 1K writes/sec), prototypes, non-viral content.

### Strategy 2: In-Memory Aggregation (Micro-Batching)

The key insight: **batch writes by aggregating in consumer memory before flushing.**

```
Consumer memory (5-second window):
┌──────────────────────────────────┐
│  video_abc  → +3,247 views      │
│  video_def  → +12 views         │
│  video_ghi  → +89,401 views     │  ← viral video
│  video_jkl  → +2 views          │
│  ...        → ...               │
└──────────────────────────────────┘
          │
          │ Every 5 seconds: flush all deltas
          ▼
┌──────────────────────────────────┐
│  Redis:                          │
│  INCRBY video:abc:count 3247    │
│  INCRBY video:def:count 12     │
│  INCRBY video:ghi:count 89401  │
│  INCRBY video:jkl:count 2      │
└──────────────────────────────────┘
```

**Write reduction math:**

Without batching: 800K events/sec = 800K Redis ops/sec.

With 5-second batching:
- In any 5s window, events are distributed across ~200K distinct videos (power law — most of 800M videos get 0 views in any 5s).
- That's 200K flushes every 5s = **40K Redis ops/sec.** Down from 800K — a **20x reduction**.
- For a viral video getting 100K views/sec: instead of 100K individual INCRBYs, it's 1 INCRBY of +500,000. **100,000x reduction for that key.**

**Crash recovery:**

If a consumer crashes before flushing:
1. We lose at most 5 seconds of in-flight aggregations.
2. The consumer restarts and replays from the last committed Kafka offset.
3. Events are reprocessed. Some views may be double-counted (~5 seconds worth).
4. At YouTube scale, this is a rounding error for display purposes.
5. For monetization, the daily batch reconciliation job recomputes exact counts from the raw event log.

**Optimal flush interval:**

| Interval | Write reduction | Max staleness | Crash data loss |
|----------|----------------|---------------|-----------------|
| 1 second | ~5x | 1s | 1s of views |
| 5 seconds | ~20x | 5s | 5s of views |
| 10 seconds | ~50x | 10s | 10s of views |
| 30 seconds | ~100x | 30s | 30s of views |

**Recommendation:** 5 seconds. Good balance of write reduction, staleness, and crash risk.

### Strategy 3: Multi-Level Aggregation

Extend micro-batching across multiple tiers — similar to how CDNs work.

```
Level 1: API Server (local buffer)
  Each API server maintains per-videoId counters in memory.
  Flush to Kafka every 1-2 seconds with aggregated events:
  "video abc got +847 views from this server"

Level 2: Kafka Consumer (micro-batch)
  Aggregate events from multiple API servers.
  Flush to Redis every 5 seconds.

Level 3: Regional Aggregation (multi-region)
  Each region aggregates locally.
  Send regional deltas to global aggregator every 5-10 seconds.
```

**Total write reduction:** API server (100x) × Consumer (20x) = **2,000x** for hot keys.

### Strategy 4: HyperLogLog for Unique Viewers

Total views and unique viewers are different metrics. Counting unique viewers exactly requires storing every userId — doesn't scale.

**HyperLogLog (HLL):**
- Probabilistic data structure for cardinality estimation
- ~0.81% error rate with 12 KB memory per counter
- Redis: `PFADD video:abc:unique_viewers user_789` / `PFCOUNT video:abc:unique_viewers`

| Approach | Memory per video | Error | Scalability |
|----------|-----------------|-------|-------------|
| Exact (HashSet of userIds) | Unbounded (grows with viewers) | 0% | Poor — 1M viewers = ~50MB per video |
| HyperLogLog | 12 KB (fixed) | ~0.81% | Excellent — constant memory regardless of viewers |
| Bloom Filter | Configurable (1% FP at 1M items ≈ 1.2 MB) | Configurable FP rate | Good but memory grows with expected cardinality |

**When to use each:**
- **Total view count** (most common): Simple counter (INCR). No need for HLL.
- **Unique viewer count** (creator analytics): HLL. 0.81% error is fine for "approximately 1.2M unique viewers."
- **Exact unique count** (ad billing): Exact count from batch processing on raw event log (daily Spark job).

---

## 4. Database Choice for Counters

### Redis

**Strengths:**
- `INCR` / `INCRBY` is O(1) and atomic. Perfect for counters.
- In-memory: sub-millisecond latency.
- Redis Cluster for horizontal scaling.
- Built-in HyperLogLog (`PFADD`, `PFCOUNT`).

**Weaknesses:**
- Memory is expensive (~$10/GB/month for RAM vs ~$0.10/GB for SSD).
- Persistence tradeoffs: RDB snapshots lose data between snapshots; AOF is slower.
- Single-threaded per shard — limited to ~100K ops/sec per shard.
- Async replication: can lose data on master failure.

**Best for:** Real-time counters (fast reads and writes). Accept it as a cache that may lose seconds of data.

### Cassandra

**Strengths:**
- Distributed, linearly scalable write throughput.
- Native `counter` column type for distributed counters.
- Multi-datacenter replication.
- Excellent for time-series data (wide columns).

**Weaknesses:**
- Counter columns are **not idempotent** — retrying a failed increment causes double-counting.
- Counter corruption is a known issue (tombstone accumulation, repair conflicts).
- Eventual consistency — counter reads may return stale values.
- Operational complexity (compaction, repair, tombstone management).

**Best for:** Durable counter storage. Accept eventual consistency.

### MySQL/PostgreSQL with Sharding

**Strengths:**
- ACID transactions. Strong consistency guarantees.
- Mature ecosystem, well-understood operationally.
- Row-level locking provides serializability.

**Weaknesses:**
- Row-level locking on hot rows = bottleneck. Viral video counter becomes a serialization point.
- Workaround: **sub-counter sharding** — split one video's counter into N rows, sum on read.
- Read fan-out: reading one video's count requires summing N rows.
- Sharding across multiple MySQL instances adds operational complexity.

**Best for:** Strong consistency use cases (billing reconciliation). Not ideal for hot real-time counters.

### Recommended: Hybrid Approach

```
┌─────────────────────────────────────────────────────────┐
│                 HYBRID STORAGE ARCHITECTURE              │
│                                                          │
│  ┌──────────────┐     ┌──────────────┐                  │
│  │    Redis      │     │  Cassandra   │                  │
│  │ (hot counter) │     │ (durable)    │                  │
│  │              │     │              │                  │
│  │ • Real-time  │     │ • Permanent  │                  │
│  │   reads      │     │   storage    │                  │
│  │ • INCRBY     │     │ • Append     │                  │
│  │ • < 1ms      │     │ • Eventually │                  │
│  │ • May lose   │     │   consistent │                  │
│  │   on crash   │     │ • Durable    │                  │
│  └──────┬───────┘     └──────┬───────┘                  │
│         │                    │                          │
│         │   Reconciliation   │                          │
│         │◄──────────────────→│                          │
│         │  (periodic check)  │                          │
│                                                          │
│  ┌──────────────────────────────────────────────┐       │
│  │  Kafka Event Log → S3 (source of truth)      │       │
│  │  Daily Spark job: recompute exact counts      │       │
│  │  Reconcile Redis + Cassandra                  │       │
│  └──────────────────────────────────────────────┘       │
└─────────────────────────────────────────────────────────┘
```

**Decision matrix:**

| Criterion | Redis | Cassandra | MySQL | Hybrid |
|-----------|-------|-----------|-------|--------|
| Read latency | < 1ms | < 10ms | < 5ms | < 1ms (Redis) |
| Write throughput | ~100K/shard | ~50K/node | ~10K/instance | ~100K/shard |
| Durability | Low (async repl) | High (RF=3) | High (ACID) | High (Cassandra + Kafka) |
| Hot key handling | Counter sharding | Counter columns | Sub-counter rows | Counter sharding (Redis) |
| Cost (100GB) | $$$ (RAM) | $$ (SSD) | $$ (SSD) | $$$ (Redis) + $$ (Cassandra) |
| Complexity | Low | Medium | Medium | High (two systems) |
| **Verdict** | Fast but fragile | Durable but quirky counters | Reliable but slow hot keys | **Best of both** |

---

## 5. Hot Partition Problem (Viral Videos)

The defining challenge. A video going viral means **thundering herd on a single key.** Even with sharding, one videoId maps to one shard.

### Solution 1: Counter Sharding

Split one video's counter into K sub-counters distributed across the Redis cluster:

```
Normal video (1 shard):
  video:abc:count = 142,839

Viral video (100 shards):
  video:xyz:count:0  = 12,847
  video:xyz:count:1  = 12,843
  video:xyz:count:2  = 12,851
  ...
  video:xyz:count:99 = 12,849

Write: INCRBY video:xyz:count:{random(0,99)} delta
Read:  SUM(MGET video:xyz:count:0 .. video:xyz:count:99)
       Or: read pre-computed total from video:xyz:count:total
```

**Write path:** Hash to a random shard → distributes writes across cluster nodes.
**Read path:** Sum all shards (fan-out read) or read pre-computed total.

**Dynamic sharding:** Not all videos need 100 shards.

| Video write rate | Shard count | Trigger |
|-----------------|-------------|---------|
| < 100 writes/sec | 1 (default) | — |
| 100 – 1K writes/sec | 10 | Auto-detected by hot-key monitor |
| 1K – 10K writes/sec | 50 | Auto-detected |
| > 10K writes/sec | 100 | Auto-detected + alert |

**Hot key detector:** Background process monitors write rates per key. When a key exceeds a threshold, it expands the shard count. When traffic subsides, it collapses shards back (merge and delete extras).

### Solution 2: Write-Behind Buffer

Queue writes to hot keys, flush in batches:

```
┌──────────────┐     ┌───────────────────┐     ┌──────────────┐
│  Consumer    │────→│  Write-Behind     │────→│    Redis     │
│  (detects    │     │  Buffer (in-mem)  │     │  (batched    │
│   hot key)   │     │  Flush every 1s   │     │   INCRBY)   │
└──────────────┘     └───────────────────┘     └──────────────┘
```

For detected hot keys, instead of immediate INCRBY, buffer in local memory and flush every 1 second. This converts 10,000 individual INCRBYs into 1 INCRBY of +10,000.

### Solution 3: Local Aggregation on API Servers

Each API server maintains an in-memory counter per hot videoId:

```
100 API servers, each buffering for 2 seconds:
- Viral video: 100K views/sec total → 1K views/sec per server → 2K views per flush
- 100 servers × 1 flush/2s = 50 flushes/sec to Kafka (instead of 100K events/sec)
- Reduction: 2,000x
```

**Implementation:**
1. Hot key detector service publishes a list of hot videoIds to all API servers (via Redis pub/sub or config push).
2. API servers check incoming videoIds against the hot list.
3. Hot videos: aggregate locally, flush to Kafka every 1-2 seconds.
4. Cold videos: publish to Kafka immediately (no local aggregation needed).

### Comparison

| Solution | Write reduction (hot key) | Complexity | Failure impact |
|----------|--------------------------|------------|----------------|
| Counter sharding | Distributes across shards | Medium (shard management) | Shard failure loses 1/K of writes |
| Write-behind buffer | Configurable (1s buffer → ~10Kx) | Low | Buffer loss on crash (1s of data) |
| Local aggregation | ~2,000x | Medium (hot key detection) | Server crash loses 1-2s of local buffer |
| **All three combined** | **Multiplicative** | High | Acceptable at each layer |

**Recommendation:** Use all three in production. They compose well — each layer independently reduces write pressure.

---

## 6. Idempotency and Deduplication

Network retries, load balancer rerouting, and client bugs can cause **duplicate view events.** Without deduplication, view counts inflate.

### Option 1: Client-Generated Idempotency Key

```
POST /v1/videos/abc/view
X-Idempotency-Key: 550e8400-e29b-41d4-a716-446655440000

Server checks:
  Redis SET: SADD dedup:{idempotency_key} → returns 0 if already exists
  TTL: 5 minutes (views older than 5 min won't be retried)
```

**Pros:** Exact deduplication within the TTL window.
**Cons:** Requires Redis memory for dedup set. At 800K events/sec with 5-min TTL: 800K × 300s × 36 bytes (UUID) ≈ **8.6 GB** of dedup state. Manageable but not free.

### Option 2: Accept Approximate Counts

At YouTube scale, 0.1% overcounting from duplicates is noise:
- 40B views/day × 0.1% = 40M duplicate views
- Spread across 800M videos = 50 extra views per video per day
- On a video with 1M views/day, that's 0.005% error

**For display purposes, this is perfectly acceptable.** For monetization, the daily reconciliation job deduplicates from the raw event log.

### Option 3: Kafka Exactly-Once Semantics

```
Producer: enable.idempotence = true (prevents duplicate sends to Kafka)
Consumer: isolation.level = read_committed (with Kafka transactions)
```

**Pros:** End-to-end exactly-once within Kafka.
**Cons:** Adds latency (~5-10ms per publish). Transactional consumers are more complex. Exactly-once doesn't extend past Kafka (Redis INCRBY is still at-least-once).

### Recommendation

| Use case | Strategy |
|----------|----------|
| Display counts | Accept approximate (Option 2) |
| Fraud detection | Idempotency key (Option 1) — don't want duplicate events confusing ML models |
| Monetization | Exact dedup in batch processing from raw event log |

---

## 7. End-to-End Write Path Flow

```
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                         COMPLETE WRITE PATH                                         │
│                                                                                     │
│  User clicks play                                                                   │
│       │                                                                             │
│       ▼ (after 30s or 50% watched)                                                  │
│  ┌──────────────────────────────────────────────────────────────────────┐            │
│  │ CLIENT                                                               │            │
│  │ • Generate UUID idempotency key                                      │            │
│  │ • Collect signals: watchDuration, deviceType, visibility, fingerprint│            │
│  │ • POST /v1/videos/{id}/view                                         │            │
│  └──────────────────────────────┬───────────────────────────────────────┘            │
│                                 │                                                   │
│                                 ▼                                                   │
│  ┌──────────────────────────────────────────────────────────────────────┐            │
│  │ API GATEWAY                                                          │            │
│  │ • TLS termination                                                    │            │
│  │ • Rate limit check (Redis sliding window)                            │            │
│  │ • Client signal validation (User-Agent, JA3)                        │            │
│  │ • Dedup check (Redis SET with idempotency key, TTL 5min)            │            │
│  │ • If hot video: aggregate locally (flush every 1-2s)                │            │
│  │ • Else: publish to Kafka immediately                                │            │
│  │ • Return 202 Accepted                                               │            │
│  └──────────────────────────────┬───────────────────────────────────────┘            │
│                                 │                                                   │
│                                 ▼                                                   │
│  ┌──────────────────────────────────────────────────────────────────────┐            │
│  │ KAFKA (view-events topic, 256 partitions, RF=3)                     │            │
│  │ • Partition key: hash(videoId)                                       │            │
│  │ • acks=all, retention=7 days                                         │            │
│  │ • Throughput: ~160 MB/sec                                            │            │
│  └───────────┬──────────────────────────────────┬───────────────────────┘            │
│              │                                  │                                   │
│              ▼                                  ▼                                   │
│  ┌──────────────────────┐          ┌──────────────────────────┐                     │
│  │ COUNTER CONSUMER     │          │ ANALYTICS CONSUMER       │                     │
│  │ GROUP                │          │ GROUP                    │                     │
│  │                      │          │                          │                     │
│  │ • Read batch from    │          │ • Feed to Apache Flink   │                     │
│  │   Kafka              │          │ • Windowed aggregation   │                     │
│  │ • Aggregate in-memory│          │   (1-min tumbling)       │                     │
│  │   per videoId        │          │ • Write to ClickHouse    │                     │
│  │ • Every 5s: flush    │          │   (time-series rollups)  │                     │
│  │   deltas             │          │                          │                     │
│  │ • Commit offset      │          │                          │                     │
│  └──────────┬───────────┘          └──────────────────────────┘                     │
│             │                                                                       │
│             ├────────────────────────────────┐                                       │
│             ▼                                ▼                                       │
│  ┌──────────────────────┐      ┌──────────────────────┐                             │
│  │ REDIS CLUSTER        │      │ CASSANDRA CLUSTER    │                             │
│  │ (real-time counters) │      │ (durable counters)   │                             │
│  │                      │      │                      │                             │
│  │ • INCRBY (sharded    │      │ • Append delta       │                             │
│  │   for hot keys)      │      │ • RF=3, CL=QUORUM   │                             │
│  │ • Serves read API    │      │ • Source of truth    │                             │
│  └──────────────────────┘      │   (with Kafka log)   │                             │
│                                └──────────────────────┘                             │
│                                                                                     │
│  ┌──────────────────────────────────────────────────────────────────────┐            │
│  │ DAILY RECONCILIATION (Spark job)                                     │            │
│  │ • Read raw events from Kafka → S3                                   │            │
│  │ • Deduplicate exactly                                                │            │
│  │ • Recompute exact counts                                             │            │
│  │ • Reconcile Redis + Cassandra                                        │            │
│  │ • Flag discrepancies for investigation                               │            │
│  └──────────────────────────────────────────────────────────────────────┘            │
└─────────────────────────────────────────────────────────────────────────────────────┘
```

### Latency Breakdown

| Step | Latency | Notes |
|------|---------|-------|
| Client → API Gateway | 1-5ms | Network + TLS |
| Rate limit check (Redis) | < 1ms | Local Redis |
| Dedup check (Redis) | < 1ms | SADD |
| Publish to Kafka | 5-10ms | acks=all, 3 replicas |
| **Total client-facing** | **< 15ms** | **Client sees 202 Accepted** |
| Kafka → Consumer | 10-50ms | Consumer poll interval |
| Consumer aggregation | 0-5,000ms | In-memory, flushed every 5s |
| Flush to Redis | < 1ms | INCRBY |
| **Total end-to-end** | **< 5-10 seconds** | **Count visible to readers** |

---

## Key Takeaways

1. **Never write to a database per event at this scale.** Always buffer and batch.
2. **The hot key problem is the defining challenge.** Counter sharding + local aggregation compose to give 2,000x+ write reduction for viral videos.
3. **Kafka is the backbone** — it decouples, buffers, provides durability, and enables replay.
4. **Hybrid storage** — Redis for speed, Cassandra for durability, Kafka log for truth. Each has a role.
5. **Approximate is good enough for display.** Save exact counting for billing (batch reconciliation).
6. **Design for the viral case**, not the average case. The average video gets 5 views/day. The architecture exists for the video that gets 50M views/hour.

---

*Back to [01-interview-simulation.md](01-interview-simulation.md) — Phase 5.*
