# Deep Dive: Scale Numbers & Storage Design

> Companion document to [01-interview-simulation.md](01-interview-simulation.md) — Phase 8.
> All numbers are YouTube-scale estimates for interview back-of-envelope math.

---

## 1. Back-of-Envelope Calculations

### Traffic Numbers

| Metric | Value | Derivation |
|--------|-------|------------|
| Daily active users | ~2 billion | YouTube public stats |
| Videos on platform | ~800 million | Estimated total uploaded |
| Average views per DAU | ~20 | 2B users × 20 = 40B views/day |
| **Daily views** | **~40 billion** | Core number — derive everything from this |
| Average views/sec | ~500K | 40B ÷ 86,400s |
| Peak views/sec | ~800K | ~1.6x average (peak hours + viral events) |
| Extreme peak (viral) | ~2M | World Cup final, major celebrity events |

### View Event Size

Each view event contains:

| Field | Type | Size |
|-------|------|------|
| `eventId` | UUID | 16 bytes |
| `videoId` | string | 12 bytes |
| `userId` | string (nullable) | 12 bytes |
| `sessionId` | string | 16 bytes |
| `timestamp` | int64 (epoch ms) | 8 bytes |
| `deviceType` | enum | 1 byte |
| `country` | ISO 3166-1 | 2 bytes |
| `referrer` | enum | 1 byte |
| `watchDurationSeconds` | int32 | 4 bytes |
| `videoDurationSeconds` | int32 | 4 bytes |
| `clientFingerprint` | hash | 16 bytes |
| `ipAddress` | IPv6 | 16 bytes |
| JSON overhead + headers | — | ~90 bytes |
| **Total per event** | — | **~200 bytes** |

### Raw Event Storage

```
Daily:   200 bytes × 40B events = 8 TB/day
Weekly:  56 TB
Monthly: 240 TB
Yearly:  ~3 PB/year

With compression (LZ4, ~3:1 for structured data):
Daily:   ~2.7 TB
Yearly:  ~1 PB compressed
```

### Counter Storage

```
Total videos:     800,000,000
Counter size:     8 bytes (int64 — supports up to 9.2 × 10^18)
Base counter:     800M × 8 bytes = 6.4 GB

With metadata per video (last updated timestamp, frozen flag, shard count):
  800M × 40 bytes = 32 GB

With sharded counters for hot videos:
  Top 1% hot videos: 8M videos × 100 shards × 8 bytes = 6.4 GB
  Other 99%: 792M × 1 shard × 8 bytes = 6.3 GB
  Total sharded counters: ~13 GB

Redis overhead (hash table, pointers, SDS strings):
  ~3x memory overhead → ~40 GB for counters
  Plus dedup sets, rate limit counters, HLL: ~60 GB

Total Redis budget: ~100 GB
```

### Kafka Throughput

```
Event rate:       800K events/sec (peak)
Event size:       200 bytes
Throughput:       800K × 200 = 160 MB/sec

Kafka cluster capacity: single cluster handles GB/sec easily
  → One cluster is sufficient for view events

Retention (7 days):
  160 MB/sec × 86,400 sec/day × 7 days = 96.8 TB
  With RF=3: ~290 TB total disk
  With compression (~3:1): ~100 TB disk
```

### Analytics Rollup Storage

```
Minute-level aggregates (48-hour retention):
  800M videos × 2,880 minutes × 20 bytes = 46 TB
  But only ~1% of videos have activity in any 48h window: 8M × 2,880 × 20 = 460 GB

Hourly aggregates (90-day retention):
  Active videos per hour: ~50M (generous)
  50M × 2,160 hours × 30 bytes = 3.2 TB

Daily aggregates (forever):
  800M videos × 365 days/year × 20 bytes = 5.8 TB/year
  With 10 years of data: ~58 TB

Demographics (per video per day):
  50M active videos/day × 50 bytes (country/device breakdowns) = 2.5 GB/day
  Per year: ~900 GB
```

### Summary Table

| Component | Size | Growth Rate |
|-----------|------|-------------|
| Raw events (S3) | 8 TB/day, ~3 PB/year | Linear with views |
| Redis (counters) | ~100 GB | Slow (grows with video count) |
| Kafka (buffer) | ~100 TB (7-day retention) | Stable (rolling window) |
| ClickHouse (minute rollups) | ~460 GB | Stable (48-hour window) |
| ClickHouse (hourly rollups) | ~3.2 TB (90 days) | Stable (rolling window) |
| ClickHouse (daily rollups) | ~6 TB/year | Linear |
| Cassandra (durable counters) | ~50 GB + ~350 TB/year (hourly) | Linear |

---

## 2. Data Storage Tiers

### Three-Tier Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                    DATA TEMPERATURE MODEL                           │
│                                                                     │
│  HOT (Redis)          WARM (ClickHouse/Cassandra)    COLD (S3)     │
│  ┌──────────────┐    ┌──────────────────────┐    ┌──────────────┐  │
│  │ Current view │    │ Hourly/daily rollups  │    │ Raw view     │  │
│  │ counts for   │    │ for analytics.        │    │ events.      │  │
│  │ all 800M     │    │ Durable counter       │    │ Parquet      │  │
│  │ videos.      │    │ history.              │    │ format.      │  │
│  │              │    │                       │    │              │  │
│  │ ~100 GB      │    │ ~5-50 TB              │    │ ~3 PB/year   │  │
│  │ < 1ms reads  │    │ < 100ms queries       │    │ Sec-min      │  │
│  │ $$$          │    │ $$                    │    │ $            │  │
│  └──────────────┘    └──────────────────────┘    └──────────────┘  │
│                                                                     │
│  Accessed by:         Accessed by:                Accessed by:      │
│  • View count API     • Analytics API             • Batch fraud     │
│  • Batch view count   • Creator dashboard         • Reconciliation  │
│  • Trending calc      • Trending computation      • Ad billing      │
│                                                   • ML training     │
└─────────────────────────────────────────────────────────────────────┘
```

### Tier Details

#### Hot Tier — Redis Cluster

| Property | Value |
|----------|-------|
| **Store** | Redis Cluster (10 shards, 3 replicas each = 30 nodes) |
| **Data** | Current view counts, rate limit counters, dedup sets, HLL counters |
| **Size** | ~100 GB total (~10 GB per shard) |
| **Latency** | < 1ms reads, < 1ms writes |
| **Retention** | Forever (always current) |
| **Durability** | Low — async replication, may lose seconds of data on failure |
| **Cost** | ~$3,000/month (30 × r6g.xlarge at $0.10/hr) |

**Configuration:**
- 10 hash slots, consistent hashing
- Each shard: 16 GB memory, ~10 GB used
- Max memory policy: `noeviction` (counters must never be evicted)
- Persistence: RDB snapshots every 5 minutes (for cold-start recovery)
- Replication: async, 1 master + 2 replicas per shard

#### Warm Tier — ClickHouse + Cassandra

**ClickHouse (analytics rollups):**

| Property | Value |
|----------|-------|
| **Data** | Per-minute, per-hour, per-day view aggregates per video |
| **Size** | ~5 TB active (grows ~6 TB/year) |
| **Latency** | < 100ms for time-range queries |
| **Retention** | Minutes: 48h, Hours: 90 days, Days: forever |
| **Cluster** | 6 nodes, 2 TB SSD each, RF=2 |
| **Cost** | ~$5,000/month |

**Cassandra (durable counters):**

| Property | Value |
|----------|-------|
| **Data** | Durable view counts, counter history, hourly snapshots |
| **Size** | ~50 GB counters + ~350 TB/year hourly history |
| **Latency** | < 10ms for single key, < 100ms for range |
| **Retention** | Forever |
| **Cluster** | 12 nodes (grows as data grows), RF=3 |
| **Consistency** | Write: QUORUM, Read: ONE (fast reads) |
| **Cost** | ~$8,000/month |

#### Cold Tier — S3 + Parquet

| Property | Value |
|----------|-------|
| **Data** | Raw view events (complete event log) |
| **Format** | Parquet (columnar, compressed) — ~3:1 compression over raw JSON |
| **Size** | ~1 PB/year (compressed) |
| **Latency** | Seconds to minutes (query via Athena/Spark/Presto) |
| **Retention** | 90 days raw, summarized forever |
| **Partitioning** | `s3://view-events/year=2024/month=02/day=28/hour=14/` |
| **Cost** | ~$23,000/month per PB ($0.023/GB/month S3 Standard) |

### Cost Optimization — Power Law Distribution

```
Video view distribution (Zipf's law):
┌──────────────────────────────────────────────────────────┐
│                                                          │
│  Top 0.01% (80K videos)    → 40% of all views            │
│  Top 0.1%  (800K videos)   → 60% of all views            │
│  Top 1%    (8M videos)     → 80% of all views            │
│  Top 10%   (80M videos)    → 95% of all views            │
│  Bottom 90% (720M videos)  → 5% of all views             │
│                                                          │
└──────────────────────────────────────────────────────────┘
```

**Implications:**
- Only ~8M videos (1%) need sharded counters and hot-key optimization
- CDN cache is extremely effective — top 1% of videos cached = 80% of read requests served from edge
- Bottom 90% of videos could use cheaper storage (no sharding, longer cache TTL)
- Analytics rollups: only ~50M videos/day have meaningful activity. Don't pre-compute rollups for videos with 0 views

---

## 3. Sharding Strategy

### Consistent Hashing

```
                    Consistent Hashing Ring

                         node_0
                        ╱      ╲
                   node_7        node_1
                  ╱                    ╲
              node_6                    node_2
                  ╲                    ╱
                   node_5        node_3
                        ╲      ╱
                         node_4

  hash(videoId) → position on ring → closest clockwise node

  With virtual nodes: each physical node owns ~32 virtual positions
  → More even distribution of keys
```

**Key design decisions:**

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Partition key | `hash(videoId)` | All operations for one video go to the same shard |
| Virtual nodes | 32 per physical node | Prevents uneven distribution when nodes are added/removed |
| Initial shards | 256 | 800K events/sec ÷ 256 = ~3,125/shard. Comfortable headroom |
| Max shards | 1024+ | Scale-out capacity for 4x traffic growth |
| Rebalancing | Automatic with virtual nodes | Adding a node only moves ~1/N of keys |

### Hot Shard Mitigation

Even with consistent hashing, a viral video maps to one shard. Solutions (detailed in [03-deep-dive-write-path.md](03-deep-dive-write-path.md)):

1. **Counter sharding within a video** — split one video's counter into K sub-counters across different shards
2. **Local aggregation** — buffer at API server level before hitting the shard
3. **Dynamic shard scaling** — auto-detect hot keys, expand shard count

### Resharding

When adding/removing nodes:

```
Before: 8 nodes, 256 virtual nodes
  video_abc → virtual_node_42 → physical_node_3

After adding node_8: 9 nodes, 288 virtual nodes
  video_abc → virtual_node_42 → physical_node_3 (unchanged)

  Only ~1/9 of keys move to the new node
  Data migration: Redis MIGRATE or lazy population (read-through)
```

**Zero-downtime resharding:**
1. Add new node to the ring
2. Route new writes to both old and new owner (dual-write)
3. Background migration of existing keys
4. Flip reads to new owner
5. Remove old copies

---

## 4. Replication and Durability

### Redis Replication

```
┌─────────────┐     async repl     ┌─────────────┐
│   Master    │───────────────────→│  Replica 1  │
│ (reads +    │───────────────────→│  Replica 2  │
│  writes)    │                    └─────────────┘
└─────────────┘
      │
      │ RDB snapshot every 5 min
      ▼
┌─────────────┐
│   Disk      │
│ (recovery)  │
└─────────────┘
```

| Scenario | Data loss | Impact | Acceptable? |
|----------|-----------|--------|-------------|
| Master crash, replica promotes | 0-500ms of writes (async repl lag) | ~400 views lost | Yes — rounding error |
| Master crash, no replica | Up to 5 min (since last RDB snapshot) | ~150M views lost | Rebuild from Cassandra + Kafka replay |
| Entire shard lost (master + replicas) | Up to 5 min | Rare (requires 3 simultaneous failures) | Rebuild from Cassandra |
| Network partition (split brain) | Possible double-counting during partition | Over-count by seconds of traffic | Reconcile from Kafka |

**Durability guarantee:** Redis is NOT the durable store. It's the fast read/write cache. Durability comes from:
- **Kafka** (7-day retention, RF=3) — source of truth for recent events
- **Cassandra** (RF=3, QUORUM writes) — durable counter values
- **S3** (11 nines durability) — permanent event archive

### Cassandra Replication

| Setting | Value | Rationale |
|---------|-------|-----------|
| Replication Factor | 3 | Survives 2 node failures |
| Write Consistency | QUORUM (2 of 3) | Strong write durability |
| Read Consistency | ONE | Fast reads (< 5ms). Accept possible staleness |
| Consistency for reconciliation | ALL | When recomputing exact counts |

**Why QUORUM writes + ONE reads?**

Counter updates don't need to be read immediately. The write path is:
1. Consumer flushes delta to Redis (fast, for real-time reads)
2. Consumer also writes to Cassandra (durable, for recovery)
3. Reads go to Redis, not Cassandra (except during recovery)

So Cassandra write consistency matters (must be durable), but read consistency doesn't (reads are rare — only during recovery or reconciliation).

---

## 5. Capacity Planning

### Year 1 → Year 5 Projections

| Metric | Year 1 | Year 2 | Year 3 | Year 5 |
|--------|--------|--------|--------|--------|
| Daily views | 40B | 50B | 60B | 80B |
| Peak events/sec | 800K | 1M | 1.2M | 1.6M |
| Videos on platform | 800M | 1B | 1.2B | 1.6B |
| Raw events (S3) | 3 PB | 3.75 PB | 4.5 PB | 6 PB |
| Redis (counters) | 100 GB | 125 GB | 150 GB | 200 GB |
| ClickHouse (rollups) | 5 TB | 11 TB | 17 TB | 29 TB |
| Kafka (7-day buffer) | 100 TB | 125 TB | 150 TB | 200 TB |

### Scaling Triggers

| Component | Trigger Metric | Threshold | Action |
|-----------|---------------|-----------|--------|
| Redis | Memory usage | > 80% per shard | Add shard (consistent hashing) |
| Redis | Ops/sec per shard | > 80K | Add shard |
| Kafka | Consumer lag | > 1 minute | Add consumers / increase partitions |
| Kafka | Disk usage | > 70% | Add brokers |
| ClickHouse | Query latency p99 | > 500ms | Add replicas or shards |
| Cassandra | Disk usage per node | > 60% | Add nodes |
| API Gateway | CPU utilization | > 70% | Auto-scale instances |

### Cost Estimate (Year 1)

| Component | Monthly Cost | Notes |
|-----------|-------------|-------|
| Redis Cluster (30 nodes) | $3,000 | r6g.xlarge, 16GB each |
| Kafka Cluster (20 brokers) | $8,000 | i3.xlarge, 1TB SSD each |
| Cassandra Cluster (12 nodes) | $8,000 | i3.xlarge |
| ClickHouse Cluster (6 nodes) | $5,000 | r6g.2xlarge |
| S3 (raw events, 1 PB) | $23,000 | S3 Standard |
| API Gateway (50 instances) | $7,000 | c6g.xlarge |
| Flink Cluster (10 nodes) | $4,000 | c6g.xlarge |
| Network/transfer | $10,000 | Inter-AZ + inter-region |
| **Total** | **~$68,000/month** | **~$816K/year** |

At YouTube's scale (~$30B/year ad revenue), $816K/year for the view counting infrastructure is 0.003% of revenue — negligible.

---

## 6. Data Lifecycle Management

### TTL Policies

```
Event lifecycle:

  Event created → Kafka (7 days) → S3 raw (90 days) → Deleted
                      │
                      └→ ClickHouse minute (48 hours) → Rolled up to hourly
                                                              │
                                                              └→ Hourly (90 days) → Rolled up to daily
                                                                                          │
                                                                                          └→ Daily (forever)
```

| Data | Store | TTL | Cleanup Method |
|------|-------|-----|----------------|
| Raw events | Kafka | 7 days | Kafka log compaction / retention |
| Raw events | S3 | 90 days | S3 lifecycle policy (move to Glacier at 30 days, delete at 90) |
| Minute aggregates | ClickHouse | 48 hours | TTL clause in table definition |
| Hourly aggregates | ClickHouse | 90 days | TTL clause |
| Daily aggregates | ClickHouse | Forever | No TTL |
| Dedup sets | Redis | 5 minutes | Key TTL |
| Rate limit counters | Redis | 1 hour | Key TTL |
| Current view counts | Redis | Forever | Never expires |
| Durable counters | Cassandra | Forever | Never expires |

### Data Compaction

**ClickHouse:**
- Uses MergeTree engine — automatically merges and compacts small parts into larger ones
- Compression: LZ4 for hot data (fast decompression), ZSTD for cold data (better ratio)
- Partition by month: `PARTITION BY toYYYYMM(event_date)` → easy to drop old partitions

**Cassandra:**
- Leveled compaction strategy (LCS) for counter tables — good read performance
- Tombstone cleanup: run `nodetool repair` weekly to clean up deleted data

**S3:**
- Raw events stored as Parquet files, partitioned by `year/month/day/hour`
- File size target: 128 MB (optimal for Spark/Athena queries)
- Small file consolidation job runs daily (merges small files from late-arriving events)

### Archival Strategy

```
┌────────────┐     30 days     ┌──────────────┐     90 days     ┌─────────────┐
│ S3 Standard│───────────────→│ S3 Glacier   │───────────────→│   Deleted   │
│ (hot)      │                │ Instant      │                │             │
│ $0.023/GB  │                │ $0.004/GB    │                │             │
└────────────┘                └──────────────┘                └─────────────┘
```

**Why 90-day retention for raw events?**
- Fraud detection needs 30-60 days of history for pattern analysis
- Regulatory compliance may require 90 days
- Beyond 90 days, daily aggregates serve all analytical needs
- Keeping raw events forever would cost ~$23K/month per PB — not justified

**What's kept forever:**
- Daily view count aggregates per video (small: ~6 TB/year)
- Durable counter values in Cassandra (small: grows slowly)
- Counter adjustment audit trail (tiny: ~100 adjustments/day)

---

## Key Numbers to Memorize for Interviews

| Metric | Value | Quick derivation |
|--------|-------|------------------|
| Views/sec (peak) | 800K | 40B/day ÷ 86,400 × 1.6 peak factor |
| Views/sec (average) | 500K | 40B/day ÷ 86,400 |
| Event size | 200 bytes | ~15 fields, mostly small strings/ints |
| Raw storage/day | 8 TB | 200B × 40B |
| Counter storage | 6.4 GB | 800M × 8 bytes |
| Redis budget | 100 GB | Counters + metadata + overhead |
| Kafka throughput | 160 MB/s | 800K × 200B |
| CDN cache hit rate | 90%+ | Power law: top 1% of videos = 80% of reads |
| Micro-batch reduction | 20-1000x | Depends on video popularity distribution |
| Fraud rejection rate | 15-20% | YouTube reported numbers |

---

*Back to [01-interview-simulation.md](01-interview-simulation.md) — Phase 8.*
