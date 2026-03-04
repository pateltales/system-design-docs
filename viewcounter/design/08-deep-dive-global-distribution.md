# Deep Dive: Global Distribution & Multi-Region Architecture

## Table of Contents

1. [Why Multi-Region?](#1-why-multi-region)
2. [Multi-Region Ingestion](#2-multi-region-ingestion)
3. [Global View Count Consistency](#3-global-view-count-consistency)
4. [Failover and Disaster Recovery](#4-failover-and-disaster-recovery)
5. [Data Sovereignty and Compliance](#5-data-sovereignty-and-compliance)
6. [Network Architecture](#6-network-architecture)

---

## 1. Why Multi-Region?

A YouTube-scale view counter processes billions of events per day originating from
every continent. Funneling all of that traffic into a single region is untenable for
three independent reasons: **latency**, **availability**, and **regulation**.

### 1.1 Latency

A user in Mumbai hitting an API gateway in us-east-1 pays ~200 ms round-trip just for
the network hop. Multiply that by every view event, every thumbnail impression, every
analytics read — the cumulative cost is enormous. A view event that takes 200 ms to
acknowledge adds visible lag to the client UI (the view count increment appears to
"stick" slower) and forces longer client-side timeouts.

With regional ingestion endpoints, the same user hits ap-south-1 at ~15 ms RTT. That
is a 13x improvement on the hot path.

### 1.2 Single Point of Failure

If your only region goes down, your entire view counting pipeline stops. No events are
ingested, no counts are updated, and read traffic either fails or returns stale data.
At YouTube scale (800K+ views per second peak), even a five-minute outage means ~240
million lost view events. Multi-region turns a region failure into a partial
degradation instead of a total outage.

### 1.3 Regulatory Requirements

- **GDPR (EU):** Personal data of EU residents must be processed with adequate
  protections. In practice many organizations keep EU user data within EU borders to
  simplify compliance.
- **Data Residency Laws:** Countries like Russia (Federal Law No. 242-FZ), China
  (PIPL), and India (proposed DPDP Act) impose or are moving toward data localization
  mandates.
- **Content Regulation:** Some jurisdictions require that view counts and engagement
  metrics for locally produced content be auditable within that jurisdiction.

Ignoring these is not an option at scale. You either deploy regionally or you spend
your engineering budget on legal fees.

---

## 2. Multi-Region Ingestion

### 2.1 Regional Deployment Topology

Each major region runs its own complete ingestion stack:

```
Regions:  US-East (Virginia)  |  US-West (Oregon)  |  EU (Frankfurt)  |  APAC (Singapore)
          ──────────────────     ─────────────────     ──────────────     ──────────────────
Stack:    API Gateway            API Gateway            API Gateway        API Gateway
          Kafka Cluster          Kafka Cluster          Kafka Cluster      Kafka Cluster
          View Validators        View Validators        View Validators    View Validators
          Redis (regional)       Redis (regional)       Redis (regional)   Redis (regional)
```

Every component from the API gateway through the initial Kafka topic is local. A view
event from Frankfurt never crosses the Atlantic just to land on a Kafka broker.

### 2.2 Kafka Topic Design per Region

Each regional Kafka cluster has the same topic structure:

```
view-events-raw         # raw ingest, partitioned by video_id
view-events-validated   # after dedup + fraud filtering
view-counts-delta       # 5-second windowed deltas (video_id -> count)
```

Partition count is tuned per region based on traffic volume. US-East might have 256
partitions on `view-events-raw`; APAC might have 128.

### 2.3 Cross-Region Data Flow

There are two fundamentally different approaches. Both work. They optimize for
different things.

#### Approach A: Centralized Processing

Every region replicates its raw (or validated) events to a single "processing" region
where Flink runs the aggregation pipeline.

```
┌──────────┐    MirrorMaker 2    ┌───────────────────────────────────┐
│ US-East  │ ──────────────────> │                                   │
│  Kafka   │                     │         US-East (Primary)         │
└──────────┘                     │                                   │
                                 │   ┌─────────────────────────┐     │
┌──────────┐    MirrorMaker 2    │   │  Unified Kafka Cluster  │     │
│ US-West  │ ──────────────────> │   │  (all regions merged)   │     │
│  Kafka   │                     │   └────────────┬────────────┘     │
└──────────┘                     │                │                  │
                                 │                v                  │
┌──────────┐    MirrorMaker 2    │   ┌─────────────────────────┐     │
│ EU       │ ──────────────────> │   │  Flink Aggregation      │     │
│  Kafka   │                     │   │  (global counts)        │     │
└──────────┘                     │   └────────────┬────────────┘     │
                                 │                │                  │
┌──────────┐    MirrorMaker 2    │                v                  │
│ APAC     │ ──────────────────> │   ┌─────────────────────────┐     │
│  Kafka   │                     │   │  Global Redis / DB      │     │
└──────────┘                     │   │  (source of truth)      │     │
                                 │   └─────────────────────────┘     │
                                 └───────────────────────────────────┘
```

**MirrorMaker 2 (MM2)** or **Confluent Replicator** handles the cross-region
replication of Kafka topics. MM2 preserves topic names (with a configurable prefix
like `eu.view-events-validated`), offsets, and consumer group state.

**Pros:**
- Single Flink cluster to operate. One place to reason about global aggregation logic.
- Exactly-once semantics are straightforward — only one writer to the global count.
- Simpler debugging: all events end up on one cluster.

**Cons:**
- Cross-region bandwidth cost. At 100 bytes/event and 500K events/sec from EU alone,
  that is ~50 MB/s sustained cross-region. At AWS inter-region pricing (~$0.02/GB),
  that is ~$86K/month for EU alone.
- Added latency before counts update: event originates in EU, replicates to US-East
  (~80-100 ms), then Flink processes it (~seconds). EU users see a count delay.
- The processing region is still a soft SPOF for writes.

#### Approach B: Distributed Processing, Global Aggregation

Each region runs its own Flink consumers that produce partial (regional) counts. A
lightweight global aggregator merges them.

```
┌──────────────────────┐   ┌──────────────────────┐
│      US-East         │   │       US-West        │
│                      │   │                      │
│  Kafka ─> Flink ─┐   │   │  Kafka ─> Flink ─┐   │
│                  │   │   │                  │   │
│   Regional Redis <┘   │   │   Regional Redis <┘   │
│        │             │   │        │             │
└────────┼─────────────┘   └────────┼─────────────┘
         │                          │
         └──────────┐  ┌────────────┘
                    v  v
          ┌─────────────────────┐
          │  Global Aggregator  │
          │  (merges deltas     │
          │   from all regions) │
          └────────┬────────────┘
                   v
          ┌─────────────────────┐
          │  Global Count Store │
          │  (Cassandra / Redis │
          │   with replication) │
          └─────────────────────┘
                   ^  ^
         ┌─────────┘  └──────────┐
         │                       │
┌────────┼─────────────┐   ┌────┼──────────────────┐
│        │             │   │    │                   │
│   Regional Redis <───┘   │  Regional Redis <──────┘
│                      │   │                       │
│  Kafka ─> Flink      │   │  Kafka ─> Flink       │
│       EU             │   │       APAC            │
└──────────────────────┘   └───────────────────────┘
```

Each region's Flink job produces a **delta stream**: every 5 seconds, it emits a
message like `{video_id: "abc", region: "eu", delta: 1437, window_end: "..."}`. These
deltas are small (kilobytes, not megabytes) and are sent to the global aggregator
which sums them.

**Pros:**
- Dramatically lower cross-region bandwidth. Deltas are orders of magnitude smaller
  than raw events.
- Each region can serve local reads from its own Redis with low latency.
- No single processing SPOF. If one region's Flink goes down, the others keep running.

**Cons:**
- Operational complexity: N Flink clusters to manage instead of one.
- Counts are eventually consistent across regions (typically 5-15 seconds behind).
- Need careful clock synchronization and window alignment across regions.

#### Which to Choose?

For a YouTube-scale system, **Approach B (distributed processing) is strongly
preferred**. The bandwidth savings alone justify it, and the eventual consistency
window of 5-15 seconds is invisible to users — nobody notices if a view count is
"304,517" vs "304,529."

Approach A makes sense for smaller systems where operational simplicity outweighs the
cross-region costs, or where you need strict global ordering of events (rare for view
counts).

---

## 3. Global View Count Consistency

This is the core design challenge. A view count is a single global number — "this
video has 1,247,893 views" — but the events that contribute to it arrive in four
different regions, each with its own processing pipeline.

### 3.1 Option 1: Centralized Counter

All regions send their increments to one global Redis instance (or cluster) that holds
the authoritative count.

```
US-East Flink ──── INCRBY video:abc 583 ────┐
US-West Flink ──── INCRBY video:abc 241 ────┤
EU Flink      ──── INCRBY video:abc 1437 ───┼──> Global Redis (us-east-1)
APAC Flink    ──── INCRBY video:abc 892 ────┘       video:abc = 1,247,893
```

**How it works:**
1. Each region's Flink job aggregates view events into 5-second tumbling windows.
2. At the end of each window, it sends a single `INCRBY` command to the global Redis.
3. The global Redis holds the exact current count.

**Latency characteristics:**

| Source Region | RTT to Global Redis (us-east-1) | Write Latency (INCRBY) |
|---------------|----------------------------------|------------------------|
| US-East       | < 1 ms                           | < 1 ms                 |
| US-West       | ~60 ms                           | ~60 ms                 |
| EU (Frankfurt)| ~85 ms                           | ~85 ms                 |
| APAC (Singapore)| ~200 ms                        | ~200 ms                |

With 5-second batching, the absolute latency of the INCRBY itself does not matter
much — you are amortizing one cross-region call over thousands of events. The concern
is **availability**: if the global Redis or the network path to it is down, writes
queue up and counts stall.

**When to use:** Systems with < 100K events/sec globally, or when you need a single
strongly consistent counter and can tolerate the availability risk.

### 3.2 Option 2: CRDT Counter (G-Counter)

A Grow-only Counter (G-Counter) is a conflict-free replicated data type. Each region
maintains its own independent counter. The global count is the sum of all regional
counters.

```
G-Counter for video "abc":

  Region     │ Local Counter
  ───────────┼──────────────
  US-East    │   312,451
  US-West    │   198,227
  EU         │   489,102
  APAC       │   248,113
  ───────────┼──────────────
  GLOBAL     │ 1,247,893   (sum)
```

**How it works:**
1. Each region's Flink job increments only its own entry in the G-Counter.
2. There is never a write conflict — each region owns exactly one slot.
3. To read the global count, you either:
   - Query all regions and sum (accurate but slow: ~200 ms for the farthest region).
   - Pre-aggregate in the background and cache (fast but slightly stale).

**Properties:**
- **Conflict-free:** By construction, concurrent increments in different regions never
  conflict. The merge function is commutative, associative, and idempotent.
- **No cross-region write latency:** Each region writes locally. Zero cross-region
  calls on the write path.
- **Eventually consistent:** After all messages propagate, every region sees the same
  global sum. The convergence window depends on the replication schedule (typically
  5-10 seconds).

**Implementation with Redis:**

```
# Each region writes only to its own hash field:
# In US-East:
HINCRBY view_count:abc us-east 1

# In EU:
HINCRBY view_count:abc eu 1

# Read global count (locally cached, periodically refreshed):
HVALS view_count:abc  -->  [312451, 198227, 489102, 248113]
SUM = 1,247,893
```

Each region replicates its counter value to other regions every N seconds. This can
use a lightweight gossip protocol, a dedicated Kafka topic, or Redis CRDT modules
(e.g., RedisGears, or Redis Enterprise's Active-Active with CRDTs).

**When to use:** Systems where write availability is paramount and you can tolerate
5-10 seconds of staleness on reads.

### 3.3 Option 3: Hybrid Approach

Combine regional Redis for fast local reads with a global aggregation pipeline as the
source of truth.

```
┌─────────────────────────────────────────────────────────────────────┐
│                        WRITE PATH                                  │
│                                                                    │
│  User ──> Regional API ──> Regional Kafka ──> Regional Flink       │
│                                                  │                 │
│                                                  v                 │
│                                          Regional Redis            │
│                                          (local increment)         │
│                                                  │                 │
│                                                  v                 │
│                                          Delta Stream              │
│                                   (view_count_deltas topic)        │
└──────────────────────────────────────────┬──────────────────────────┘
                                           │
                            cross-region (small deltas only)
                                           │
                                           v
┌─────────────────────────────────────────────────────────────────────┐
│                     GLOBAL AGGREGATION                              │
│                                                                    │
│  Delta Consumer (merges deltas from all regions)                   │
│         │                                                          │
│         v                                                          │
│  Global Count Store (Cassandra / Aurora Global DB)                 │
│         │                                                          │
│         v                                                          │
│  Broadcast updated global count back to all regions (every 5-10s)  │
└──────────────────────────────────────────┬──────────────────────────┘
                                           │
                              fan-out to all regions
                                           │
                                           v
┌─────────────────────────────────────────────────────────────────────┐
│                        READ PATH                                   │
│                                                                    │
│  Regional Redis stores cached global count                         │
│  Read request ──> Regional Redis ──> return cached count           │
│  (< 1 ms local read, at most 10s stale)                           │
└─────────────────────────────────────────────────────────────────────┘
```

**How it works step by step:**

1. **Local write:** View event hits regional Kafka, regional Flink aggregates it,
   increments regional Redis. This is entirely local — sub-millisecond.

2. **Delta emission:** Every 5 seconds, each region emits a delta:
   `{video_id, region, delta, window_end}` onto a cross-region Kafka topic.

3. **Global merge:** A central aggregator consumes deltas from all regions, computes
   the authoritative global count, writes it to a durable store (Cassandra).

4. **Broadcast:** The global aggregator publishes the updated global count back to
   each region's Redis. This can be done via a `global-counts` Kafka topic that each
   region consumes, or via direct Redis replication.

5. **Local read:** Any read request for the view count hits regional Redis, which
   returns the most recently synced global count. Maximum staleness: one sync interval
   (5-10 seconds).

### 3.4 Comparison

| Dimension              | Centralized Counter     | CRDT (G-Counter)         | Hybrid                    |
|------------------------|-------------------------|--------------------------|---------------------------|
| Write latency          | Cross-region (60-200ms) | Local (< 1ms)            | Local (< 1ms)             |
| Read latency           | Local if cached         | Local (sum precomputed)  | Local (< 1ms)             |
| Consistency            | Strong                  | Eventually consistent    | Eventually consistent     |
| Staleness window       | 0 (real-time)           | 5-10 seconds             | 5-10 seconds              |
| Write availability     | SPOF on global Redis    | Fully available          | Fully available            |
| Cross-region bandwidth | Medium (batched INCRBYs)| Low (gossip/sync)        | Low (deltas only)         |
| Operational complexity | Low                     | Medium (CRDT libraries)  | Medium-High               |
| Failure blast radius   | Global (all writes)     | Regional (one counter)   | Regional (writes local)   |
| Accuracy               | Exact                   | Exact (after convergence)| Exact (after convergence) |
| Best for               | Small scale, strict     | Write-heavy, AP systems  | YouTube-scale production  |

### 3.5 Recommendation: Hybrid Approach

For a YouTube-scale view counter, the **hybrid approach** is the right choice. Here is
the justification:

1. **Write path must be local.** At 800K+ events/sec globally, you cannot afford
   cross-region writes on every increment. The hybrid approach keeps the write path
   entirely within the local region.

2. **5-10 second staleness is invisible.** No user watches a view count tick up in
   real-time and complains that it is 7 seconds behind. YouTube itself shows rounded
   counts ("1.2M views") that mask far larger imprecision.

3. **Operational clarity.** Unlike a pure CRDT approach, the hybrid has a clear source
   of truth (the global aggregation pipeline and its durable store). If something goes
   wrong, you know where the authoritative data lives. CRDTs distribute state in a way
   that makes debugging harder.

4. **Graceful degradation.** If the global aggregator goes down, regional counts keep
   working. Users in each region see slightly stale but functional data. When the
   aggregator recovers, it catches up from the delta Kafka topic (which has hours of
   retention) and reconciles.

5. **Clean separation of concerns.** The write path (local Kafka + Flink + Redis) is
   optimized purely for throughput. The global aggregation path is optimized for
   correctness. The read path (regional Redis) is optimized for latency. Each can be
   scaled and tuned independently.

---

## 4. Failover and Disaster Recovery

### 4.1 Failure Modes

| Failure                    | Impact                            | Detection Time | Recovery Target |
|----------------------------|-----------------------------------|----------------|-----------------|
| Single Kafka broker        | None (replicated)                 | Seconds        | Automatic       |
| Regional Kafka cluster     | Region cannot ingest              | 30-60 seconds  | Minutes         |
| Regional Flink cluster     | Region counts stop updating       | 1-2 minutes    | Minutes         |
| Regional Redis             | Regional reads serve stale data   | Seconds        | Automatic (replica promote) |
| Global aggregator          | Global counts freeze              | 1-2 minutes    | Minutes         |
| Entire region outage       | All services in region down       | 30-60 seconds  | Minutes (DNS failover) |
| Inter-region network       | Deltas stop flowing               | 1-2 minutes    | Variable        |

### 4.2 DNS-Based Traffic Failover

Traffic routing to the nearest healthy region is the first line of defense.

```
                        ┌─────────────────┐
                        │    GeoDNS /     │
User ──────────────────>│    Route 53     │
                        │  Health Checks  │
                        └───────┬─────────┘
                                │
                    ┌───────────┼───────────┐
                    │           │           │
                    v           v           v
              ┌──────────┐ ┌────────┐ ┌──────────┐
              │ US-East  │ │  EU    │ │  APAC    │
              │ healthy  │ │ DOWN   │ │ healthy  │
              └──────────┘ └────────┘ └──────────┘
                                │
                         failover routing
                                │
                    ┌───────────┴───────────┐
                    v                       v
              ┌──────────┐           ┌──────────┐
              │ US-East  │           │  APAC    │
              │ absorbs  │           │ absorbs  │
              │ EU West  │           │ EU East  │
              └──────────┘           └──────────┘
```

**Implementation:**
- AWS Route 53 (or equivalent) with latency-based routing and health checks.
- Health checks ping the regional API gateway every 10 seconds.
- If a region fails 3 consecutive checks (30 seconds), traffic reroutes to the next
  nearest healthy region.
- TTL on DNS records: 60 seconds. Effective failover time: 30s detection + 60s DNS
  propagation = ~90 seconds.

### 4.3 Kafka Replication and Event Durability

Events are never lost during a regional failure because of Kafka's replication within
a region and cross-region replication of deltas.

**Intra-region:**
- `replication.factor = 3` on all topics.
- `min.insync.replicas = 2`.
- A single broker failure is invisible to producers.

**Cross-region (delta stream):**
- MirrorMaker 2 replicates the `view-counts-delta` topic to the global aggregation
  region.
- MM2 tracks source offsets, so on recovery it resumes from the last replicated offset.
- If the source region goes down before replication completes, the unsynced deltas are
  recovered when the region comes back (Kafka retains data for hours/days).

**Worst-case data loss window:**
- Deltas that were aggregated locally but not yet replicated cross-region.
- With a 5-second window and MM2 lag of ~1-2 seconds, this is at most ~7 seconds of
  delta data per video.
- On recovery, the regional Flink job replays from its last Kafka checkpoint and
  re-emits the deltas, filling in the gap.

### 4.4 View Count Divergence During Failover

When a region goes down, its delta stream stops. The global count freezes the
contribution from that region. Here is what happens:

```
Timeline:
  T=0      EU goes down. Global count = 1,247,893
  T=0-5min EU events lost (users rerouted, new events go to US-East/APAC)
           Global count keeps incrementing from other regions
           Global count at T=5min = 1,252,100
  T=5min   EU recovers. Flink restarts from checkpoint.
           Replays ~5 minutes of buffered Kafka events.
           Emits catch-up deltas: EU delta = 4,891 (events during outage)
  T=6min   Global aggregator processes catch-up deltas.
           Global count = 1,256,991 (reconciled)
```

**Key insight:** The view count is monotonically increasing. You never need to "undo"
a count. Recovery is always additive — replay missed events, emit the delta, and the
global aggregator absorbs it. This makes reconciliation trivial compared to systems
that support decrements or arbitrary mutations.

### 4.5 Recovery Runbooks

#### Runbook: Regional Kafka Cluster Down

```
1. ALERT: Kafka health check fails for region X.
2. AUTOMATIC: GeoDNS reroutes ingestion traffic to neighboring regions.
3. VERIFY: Check that rerouted traffic is being processed (monitor
   ingestion rate in neighboring regions — should increase by ~region X's
   normal throughput).
4. DIAGNOSE: Identify root cause (disk failure, network partition, AZ outage).
5. RECOVER:
   a. If AZ-level: Kafka automatically rebalances across remaining AZs.
   b. If cluster-level: Restore from latest backup / rebuild cluster.
6. REPLAY: Once Kafka is back, MirrorMaker 2 resumes replication from last
   offset. No manual intervention needed.
7. VERIFY: Monitor delta stream lag. Should catch up within minutes.
8. RESTORE: Re-enable GeoDNS routing to recovered region.
9. POST-MORTEM: Document incident, update capacity planning.
```

#### Runbook: Global Aggregator Down

```
1. ALERT: Global aggregator health check fails.
2. IMPACT: Regional counts continue working. Global count is frozen.
   Users see slightly stale (but functional) counts.
3. NO PANIC: No data is lost. Deltas are buffered in Kafka
   (retention: 72 hours).
4. DIAGNOSE: Check Flink job status, resource exhaustion, dependency
   failures.
5. RECOVER:
   a. Restart Flink job from last successful checkpoint.
   b. If checkpoint is corrupted: restart from earliest available Kafka
      offset and recompute global counts from deltas.
6. CATCH-UP: Flink replays buffered deltas. At 10x processing speed,
   a 1-hour outage catches up in ~6 minutes.
7. VERIFY: Compare global count against sum of regional counters.
   Discrepancy should be zero after catch-up.
8. BROADCAST: Updated global counts propagate to regional Redis
   automatically.
```

#### Runbook: Entire Region Outage

```
1. ALERT: Multiple health checks fail across all services in region X.
2. AUTOMATIC:
   - GeoDNS reroutes user traffic to neighboring regions.
   - Cross-region Kafka replication pauses (source is down).
3. CAPACITY: Verify neighboring regions can absorb the extra load.
   - Check CPU, memory, Kafka partition lag, Flink backpressure.
   - Auto-scale consumer groups and Flink parallelism if needed.
4. COMMUNICATION: Update status page. View counts are functional but
   may show counts that are 5-10 seconds stale (normal) plus the
   unreplicated delta from the failed region (likely < 10 seconds
   of data).
5. WAIT: Region recovery is typically infrastructure-level (cloud
   provider). ETA varies.
6. RECOVERY (once region is back):
   a. Kafka brokers rejoin cluster, partitions rebalance.
   b. Flink jobs restart from checkpoints.
   c. Regional Redis warms up from global count broadcast.
   d. Flink replays buffered events, emits catch-up deltas.
7. VERIFY:
   a. Regional ingestion rate returns to baseline.
   b. Delta stream lag drops to near-zero.
   c. Global count reconciles (should happen automatically).
8. RESTORE: Re-enable GeoDNS routing to recovered region. Do this
   gradually (10% -> 50% -> 100%) to avoid thundering herd.
```

---

## 5. Data Sovereignty and Compliance

### 5.1 GDPR and EU Data Residency

A view event contains:
- `video_id` — not personal data.
- `user_id` — personal data (identifies a natural person).
- `ip_address` — personal data under GDPR.
- `user_agent` — potentially personal data (browser fingerprinting).
- `timestamp` — not personal data on its own.
- `geo_location` — potentially personal data at fine granularity.

**The core tension:** You need `user_id` for deduplication (same user watching twice
should count once) but GDPR constrains how you move it across borders.

**Architecture for compliance:**

```
┌───────────────────────────────────────────────────┐
│                   EU REGION                        │
│                                                    │
│  View Event (full PII):                           │
│  {user_id, video_id, ip, timestamp, geo}          │
│         │                                          │
│         v                                          │
│  ┌─────────────────┐                               │
│  │  Dedup + Fraud  │  <── needs user_id            │
│  │  Filter         │                               │
│  └────────┬────────┘                               │
│           │                                        │
│           v                                        │
│  ┌─────────────────┐    ┌──────────────────────┐   │
│  │ Anonymize /     │───>│ EU Data Lake          │   │
│  │ Pseudonymize    │    │ (full PII retained    │   │
│  └────────┬────────┘    │  under EU controls)   │   │
│           │              └──────────────────────┘   │
│           v                                        │
│  Anonymized Delta:                                 │
│  {video_id, delta_count, region: "eu"}             │
│  (NO user_id, NO ip, NO geo)                       │
│         │                                          │
└─────────┼──────────────────────────────────────────┘
          │
          │  <── only this crosses the border
          v
┌─────────────────────────────────────────┐
│        GLOBAL AGGREGATION               │
│  Receives: {video_id, delta, region}    │
│  No PII. GDPR-safe.                     │
└─────────────────────────────────────────┘
```

**Key design decisions:**
1. PII never leaves its origin region. Deduplication and fraud detection happen locally
   using `user_id` and `ip_address`.
2. Only anonymized aggregates (video_id + count delta) cross region boundaries.
3. The EU data lake retains full PII for analytics, auditing, and right-to-erasure
   compliance, all within the EU region.

### 5.2 Right to Erasure (Article 17)

When a user requests deletion:
1. The regional data lake deletes or anonymizes all records for that `user_id`.
2. Kafka topics with PII have finite retention (7 days). After retention expires, the
   data is gone.
3. View counts themselves are already anonymized — they are just numbers. No need to
   decrement a count when a user is deleted (the count represents "this video was
   viewed N times," not "these specific users viewed it").
4. Dedup caches (Bloom filters, Redis sets) containing `user_id` hashes are flushed on
   a rolling basis (TTL-based). A deleted user's hash naturally expires.

### 5.3 Data Masking for Cross-Region Analytics

Sometimes you need cross-region analytics (e.g., "what is the global watch time
distribution?") that requires more detail than just count deltas.

**Approach: k-anonymity + differential privacy**
- Before exporting from a region, aggregate data into buckets of at least k=100 users.
- Add calibrated noise (Laplacian mechanism) to prevent re-identification.
- Export only these anonymized aggregates for global analytics.

### 5.4 Region-Specific Retention Policies

| Region     | Raw Events | Validated Events | Count Deltas | Aggregated Counts |
|------------|-----------|------------------|--------------|-------------------|
| EU         | 7 days    | 30 days          | 90 days      | Indefinite        |
| US         | 30 days   | 90 days          | 1 year       | Indefinite        |
| APAC       | 14 days   | 60 days          | 90 days      | Indefinite        |
| India      | 30 days   | 90 days          | 1 year       | Indefinite        |

Retention is enforced via:
- Kafka topic-level `retention.ms` configuration.
- Data lake lifecycle policies (S3 lifecycle rules, GCS object lifecycle).
- Automated compliance scans that flag any data exceeding its retention window.

---

## 6. Network Architecture

### 6.1 Global Load Balancing

Traffic must reach the nearest healthy region with minimal latency. There are two
primary mechanisms.

#### GeoDNS

DNS resolves to different IP addresses based on the client's geographic location.

```
Client in Berlin
    │
    v
DNS query: viewcount.youtube.com
    │
    v
GeoDNS (Route 53 / Cloud DNS)
    │  Client IP geo-lookup: Europe
    │
    v
Returns: 52.57.x.x (eu-west-1 API Gateway)
```

**Characteristics:**
- Resolution granularity: country or region level.
- TTL: 60 seconds (balance between failover speed and DNS cache efficiency).
- Limitation: relies on client DNS resolver location, which can be inaccurate (e.g.,
  a user in India using Google DNS 8.8.8.8 might resolve to a US endpoint).

#### Anycast

A single IP address is advertised via BGP from multiple regions. The network itself
routes packets to the nearest announcement.

```
Client in Berlin
    │
    v
Destination: 198.51.100.1 (anycast IP)
    │
    v
BGP routing ──> nearest advertisement
    │
    v
Frankfurt PoP (eu-central-1)
```

**Characteristics:**
- No DNS dependency. Works at the IP layer.
- Instant failover: if a region withdraws its BGP announcement, traffic re-routes
  within seconds.
- Works correctly regardless of the client's DNS resolver location.
- Used by Google, Cloudflare, and most large-scale CDN providers.

**Recommendation:** Use Anycast for the ingestion endpoint (high volume, latency
sensitive) and GeoDNS for the read API (where you want more control over routing
policy).

### 6.2 Inter-Region Backbone

Cross-region traffic (delta streams, global count broadcasts, MirrorMaker replication)
should not traverse the public internet.

```
┌──────────────┐                              ┌──────────────┐
│   US-East    │ ────── Dedicated Backbone ──> │     EU       │
│              │ <───── (private fiber, not ── │              │
│              │         public internet)      │              │
└──────┬───────┘                              └──────┬───────┘
       │                                              │
       │         Dedicated Backbone                   │
       │                                              │
┌──────┴───────┐                              ┌──────┴───────┐
│   US-West    │ ────── Dedicated Backbone ──> │    APAC      │
└──────────────┘                              └──────────────┘
```

**Options:**

| Approach                  | Latency    | Bandwidth Cost  | Reliability       |
|---------------------------|------------|-----------------|-------------------|
| Public internet           | Variable   | Standard egress | Best-effort       |
| Cloud provider backbone   | Consistent | Premium egress  | SLA-backed        |
| Dedicated interconnect    | Lowest     | Fixed + commit  | Highest           |
| VPN over internet         | Variable   | Standard egress | Encrypted overlay |

**At YouTube scale, use the cloud provider's backbone.** AWS has Global Accelerator
and inter-region VPC peering over the AWS backbone. GCP has Premium Tier networking.
These provide consistent latency (no public internet jitter) and SLA guarantees,
without the capital expense of dedicated fiber.

For the delta stream specifically:
- US-East <-> EU: ~85 ms over backbone (vs ~100-120 ms over public internet).
- US-East <-> APAC: ~180 ms over backbone (vs ~200-250 ms over public internet).
- The improvement is less about raw latency and more about **consistency** — p99
  latency on the backbone is much tighter than on the public internet.

### 6.3 Latency Optimization Strategies

#### 6.3.1 Connection Pooling and Keep-Alive

Cross-region connections are expensive to establish (TCP handshake + TLS = 3-4 RTTs).
Maintain persistent connection pools between regions.

```
Regional Flink ──> Connection Pool (50 persistent connections)
                       │
                       │  keep-alive, TCP_NODELAY
                       │
                       v
                   Global Aggregator
```

- Pool size: 50-100 connections per region pair.
- Keep-alive interval: 30 seconds.
- Connection recycling: every 1 hour (to rebalance across backend instances).

#### 6.3.2 Compression

Delta messages are small but numerous. Compress them at the batch level.

| Compression | Ratio  | CPU Cost | Best For                    |
|-------------|--------|----------|-----------------------------|
| None        | 1.0x   | Zero     | < 1 MB/s cross-region       |
| LZ4         | 2-3x   | Very low | Default choice for deltas   |
| Zstd        | 3-5x   | Low      | Higher compression needed   |
| Gzip        | 3-4x   | Medium   | Compatibility requirements  |

Use **LZ4** for Kafka topic compression (set `compression.type=lz4` on the delta
topics). It gives meaningful compression with negligible CPU overhead.

#### 6.3.3 Batching and Coalescing

Never send one delta per video per window. Batch them:

```
Instead of:
  {video: "a", delta: 5}    # 1 message
  {video: "b", delta: 12}   # 1 message
  {video: "c", delta: 3}    # 1 message
  ... (100,000 messages)

Send:
  {
    window_end: "2026-02-27T10:05:00Z",
    region: "eu",
    deltas: [
      {"a": 5}, {"b": 12}, {"c": 3}, ...
    ]
  }
  # 1 message (compressed: ~200 KB for 100K videos)
```

This reduces cross-region message count from 100K to 1, and compresses well because
video IDs and small integers have high redundancy.

#### 6.3.4 Regional Read Caches with Tiered Freshness

Not all videos need the same freshness guarantee.

```
Tier 1 — Viral videos (> 10K views/hour):
  Sync global count every 2 seconds.
  These are the videos users are actively watching counts change on.

Tier 2 — Active videos (100-10K views/hour):
  Sync global count every 10 seconds.
  Standard freshness.

Tier 3 — Long-tail videos (< 100 views/hour):
  Sync global count every 60 seconds.
  Nobody is watching these counts tick.
```

This tiered approach reduces cross-region sync traffic by ~90% compared to syncing
every video every 2 seconds. Tier classification is done by the global aggregator
based on the rolling view rate.

---

## Summary

The multi-region architecture for a YouTube-scale view counter is not a single design
decision — it is a set of interlocking choices that must be consistent with each other:

| Layer                | Decision                              | Rationale                              |
|----------------------|---------------------------------------|----------------------------------------|
| Ingestion            | Regional Kafka + API Gateway          | Latency, availability                  |
| Processing           | Regional Flink (distributed)          | Bandwidth, fault isolation             |
| Consistency model    | Hybrid (local write, global merge)    | Write availability + eventual accuracy |
| Failover             | GeoDNS + Anycast + Kafka replay       | Automated, no data loss                |
| Compliance           | PII stays in region, only deltas cross| GDPR, data residency                   |
| Network              | Cloud backbone + Anycast + LZ4        | Consistent latency, low overhead       |

The system tolerates any single region failure with zero data loss and < 90-second
failover time. View counts are globally consistent within 5-10 seconds under normal
operation, and automatically reconcile after any failure.
