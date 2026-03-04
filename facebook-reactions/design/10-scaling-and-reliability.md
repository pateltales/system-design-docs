# Scaling and Reliability

This document covers the cross-cutting concerns of scaling and reliability for a Facebook Reactions system. At Facebook/Meta scale, reactions touch billions of users and trillions of stored records, making this one of the most demanding read-heavy workloads in the industry.

---

## 1. Scale Numbers (Facebook/Meta-Scale)

| Metric | Value |
|--------|-------|
| Monthly Active Users (MAU) | ~3.07 billion (Facebook alone) |
| Daily Active Users (DAU) | ~2.11 billion (Facebook alone) |
| Daily Active People across Meta apps | ~3.58 billion (Dec 2025) |
| Reactions per day | Billions (across posts, comments, messages, stories) |
| Historical reaction records | Trillions |
| Posts rendered per day | Billions, each needing a reaction summary |
| Read queries per day | Billions (reaction summaries fetched on every post render) |
| Peak reaction rate (viral/celebrity posts) | 100K+ reactions per second on a single entity |
| Read:write ratio | 100:1 to 1000:1 (extremely read-heavy) |

The core challenge: every time anyone views a post, the system must return a reaction summary (count per type, whether the viewer has reacted, and a sample of friends who reacted). This turns billions of post renders into billions of read queries daily, while writes---though fewer---arrive in enormous bursts on viral content.

---

## 2. TAO Architecture (Facebook's Actual Solution)

Facebook's reactions are stored and served through **TAO** (The Associations and Objects), a distributed graph store described in the USENIX ATC 2013 paper. TAO was purpose-built to handle the social graph and all associated edges (reactions, friendships, comments, likes) at Facebook scale.

### Data Model

TAO models everything as two primitives:

- **Objects**: Posts, users, pages, comments, stories. Each has a type, an ID, and a set of key-value data fields.
- **Associations**: Directed edges between objects. A reaction is an association from a user to a post, with the reaction type (Like, Love, Haha, etc.) stored as edge data.

### Two-Tier Caching Architecture

```
                        +-----------+
                        |  Clients  |
                        +-----+-----+
                              |
                    +---------v----------+
                    |     Followers      |   (client-facing cache tier)
                    |  (read replicas)   |
                    +---------+----------+
                              |
                    +---------v----------+
                    |      Leaders       |   (consistency-maintaining tier)
                    | (one per shard)    |
                    +---------+----------+
                              |
                    +---------v----------+
                    |       MySQL        |   (durable storage)
                    |  (hundreds of      |
                    |  thousands of      |
                    |  shards)           |
                    +--------------------+
```

- **Followers**: Client-facing cache servers. Handle the vast majority of reads. Multiple follower tiers can exist per region.
- **Leaders**: One leader per shard. Maintains cache consistency. All cache misses and writes flow through the leader.
- **MySQL**: The durable storage layer, sharded into hundreds of thousands of shards.

### Key Design Properties

| Property | Detail |
|----------|--------|
| Throughput | Billion reads/sec, millions of writes/sec |
| Sharding | Hundreds of thousands of MySQL shards |
| Write path | Write-through caching via the leader |
| Cache invalidation | Lease-based invalidation prevents thundering herds |
| Per-type caching policies | Different TTLs and caching strategies for different association types |
| Count queries | Served in constant time via pre-tracked counters (not computed on the fly) |
| Cross-region replication | Asynchronous; one region is leader for writes, all regions serve reads |

### Why TAO, Not Raw Memcache + MySQL

Before TAO, Facebook used Memcache directly in front of MySQL. This led to:
- Cache invalidation bugs (stale data)
- Thundering herds on cache misses
- No awareness of the graph data model at the caching layer
- Difficulty coordinating cross-region consistency

TAO solves all of these by making the cache layer graph-aware and introducing leader-based coordination for writes and invalidations.

---

## 3. Multi-Region Architecture

Facebook operates across multiple data centers globally. Reactions must be available at low latency worldwide.

### Write Path (Cross-Region)

```
User in Europe reacts to a post
    |
    v
Local Follower cache (Europe)
    |
    v
Leader cache (US-East, the leader region for this shard)
    |
    v
MySQL (US-East)
    |
    v
Async replication to Europe MySQL replica
    |
    v
mcsqueal daemon detects binlog change
    |
    v
Cache invalidation sent to European followers
```

### Read Path (Local)

```
User in Europe views a post
    |
    v
Local Follower cache (Europe) --- cache hit ---> return immediately
    |
    cache miss
    v
Local Leader cache (Europe, if follower tier exists) or forward to Leader region
    |
    v
MySQL replica (Europe)
```

### Cross-Region Properties

| Property | Detail |
|----------|--------|
| Replication lag | ~1-5 seconds cross-region |
| Consistency model | Eventual consistency for reads; writes are linearized through the leader region |
| User experience | Users in different regions may see slightly different counts for a few seconds |
| Cache invalidation propagation | mcsqueal daemons tail the MySQL binlog and broadcast invalidations across regions |
| Leader assignment | Each shard has one leader region; write traffic for that shard routes to that region |

### mcsqueal: Cross-Region Cache Invalidation

mcsqueal is a daemon that:
1. Tails the MySQL replication stream (binlog)
2. Extracts cache keys that need invalidation
3. Broadcasts invalidation messages to Memcache/TAO caches in remote regions
4. Ensures that when data changes in the leader region, stale cache entries in follower regions are purged

This avoids the alternative of having every write explicitly invalidate caches in every region, which would be fragile and slow.

---

## 4. Failure Modes and Mitigations

| Failure | Impact | Mitigation |
|---------|--------|------------|
| **Cache failure** | Thundering herd on database | Lease-based invalidation (TAO) prevents multiple concurrent fills; gutter pools (Memcache) absorb load from failed cache servers |
| **Shard failure** | Reactions for affected entities unavailable | Replica promotion to restore availability; redirect reads to healthy shard replicas |
| **Kafka lag** | Notifications delayed, aggregated counts stale in downstream consumers | Monitor consumer lag with alerts; auto-scale consumer groups to catch up |
| **Count drift** | Displayed reaction counts diverge from actual stored reactions | Periodic reconciliation job re-computes counts from source-of-truth associations and corrects drift |
| **Hot key (viral post)** | Single cache server or DB shard overwhelmed by traffic to one entity | Sharded counters split the hot key across N sub-keys; write buffering coalesces updates; hot/cold routing detects and redirects hot keys to dedicated capacity |
| **Network partition** | Split-brain reads across regions | Leader-based writes prevent write conflicts; reads may be stale but not inconsistent |
| **Full region outage** | All writes for shard leaders hosted in that region fail | Failover shard leadership to a secondary region (can be manual or automated depending on severity) |

### Lease-Based Invalidation (Detail)

When a cache miss occurs in TAO:
1. The follower requests a **lease** from the leader to fill the cache entry.
2. Only the holder of the lease is allowed to populate the cache.
3. If a write arrives while the lease is outstanding, the lease is invalidated.
4. This prevents stale data from being written to cache after a concurrent update.

### Gutter Pools (Detail)

From Facebook's Memcache paper:
- When a Memcache server fails, its keys are temporarily redirected to a **gutter pool** (a small reserve pool of cache servers).
- The gutter pool absorbs the thundering herd that would otherwise hit the database.
- Entries in the gutter pool have short TTLs to avoid long-term staleness.
- This is a critical defense mechanism: without gutter pools, the failure of a single popular cache server could cascade into a database outage.

---

## 5. Monitoring and Alerting

### Key Metrics

| Metric | Target / Threshold | Why It Matters |
|--------|-------------------|----------------|
| Reaction rate (global, per-region, per-entity) | Baseline + anomaly detection | Detect viral events, abuse, or system issues |
| Count accuracy / drift | < 0.01% drift rate | Users notice wrong counts; drift indicates bugs or lost writes |
| Cache hit rate | 99%+ | Below this, database load increases dangerously |
| Write latency p50 / p95 / p99 | p50 < 5ms, p99 < 50ms | Slow writes degrade user experience on reaction tap |
| Read latency p50 / p95 / p99 | p50 < 1ms, p99 < 10ms | Reaction summaries are on the critical path for feed rendering |
| Notification delivery latency | < 30 seconds p99 | Delayed "X reacted to your post" notifications reduce engagement |
| Hot-key detection | Alert when any entity exceeds N reactions/sec | Trigger sharded counter activation before the shard is overwhelmed |
| Kafka consumer lag | < 10 seconds | Lagging consumers mean stale aggregations and delayed notifications |
| Shard health and replication lag | All shards healthy, replication lag < 5s | Unhealthy shards or high lag means potential data loss on failover |
| Reconciliation job success rate | 100% of scheduled runs complete | Failed reconciliation means drift goes undetected |

### Alerting Tiers

1. **P0 (page immediately)**: Cache hit rate drops below 95%, any shard unavailable, write error rate > 1%
2. **P1 (page within 15 minutes)**: Replication lag > 10 seconds, Kafka consumer lag > 60 seconds, reconciliation job failure
3. **P2 (ticket, next business day)**: Count drift detected in sampling, cache hit rate between 95-99%, elevated latency at p99

---

## 6. Graceful Degradation

The system should degrade gracefully rather than fail completely. Each degradation level preserves core functionality while shedding non-essential work.

### Degradation Strategies

| Condition | Response |
|-----------|----------|
| **Cache tier is down** | Serve stale counts from client-side cache. For posts with no cached data, show "reactions unavailable" rather than hammering the database. Activate gutter pools if available. |
| **Write path is slow or partially unavailable** | Queue reactions client-side with exponential backoff retry. The user sees their reaction applied optimistically in the UI. If the write ultimately fails after retries, revert the UI and show a gentle error. |
| **Notification pipeline is backed up** | Increase the coalescing window (e.g., from 5 minutes to 30 minutes). Batch notifications more aggressively ("Alice and 47 others reacted" instead of individual notifications). Drop low-priority notification channels first (email before push). |
| **Hot key detected** | Automatically activate sharded counters for the affected entity. Route reads through an additional local cache layer with short TTL. Throttle non-essential queries (e.g., "who reacted" lists) while preserving count queries. |
| **Database shard overloaded** | Shed read traffic to replicas. Queue non-critical writes. Return cached (potentially stale) data rather than timing out. |
| **Full region outage** | Redirect all traffic to surviving regions. Accept higher latency for users who were served by the failed region. Initiate shard leader failover for affected shards. |

### Client-Side Resilience

The client (mobile app, web app) also participates in graceful degradation:
- **Optimistic updates**: When a user taps a reaction, the UI updates immediately without waiting for the server response.
- **Local queue**: Failed reaction writes are stored locally and retried.
- **Stale data tolerance**: Reaction counts are cached client-side and refreshed periodically, not on every render.
- **Timeout budgets**: If the reaction summary API does not respond within 200ms, the client renders the post without reactions and fills them in later.

---

## 7. Contrast with Smaller Scale

The architecture described above is necessary at Facebook/Meta scale. At smaller scales, most of this complexity is unnecessary and harmful (increased development cost, operational burden, and debugging difficulty).

### What You Actually Need at Different Scales

| Scale | Architecture | Why |
|-------|-------------|-----|
| **< 1M users** | Single PostgreSQL with a `reactions` table. `COUNT(*)` queries with appropriate indexes. Simple Redis cache for popular posts. | A single Postgres instance handles thousands of QPS easily. `COUNT(*)` on an indexed column is fast enough. Redis handles the hot-key problem. |
| **1M - 50M users** | PostgreSQL with read replicas. Pre-computed count columns (updated via triggers or application logic). Redis cluster for caching. | Read replicas handle read scaling. Pre-computed counts avoid `COUNT(*)` on every request. Redis cluster provides cache redundancy. |
| **50M - 500M users** | Sharded MySQL or PostgreSQL. Dedicated cache layer (Memcached or Redis cluster). Message queue for async processing (notifications, analytics). | Sharding is needed when a single database can no longer hold all the data or handle the write throughput. A message queue decouples the write path from downstream consumers. |
| **500M+ users** | TAO-like architecture. Multi-region deployment. Sharded counters for hot keys. Full observability and graceful degradation. | This is where the full complexity described in this document becomes necessary. |

### The Golden Rule

**Do not build for scale you do not have.** Every layer of caching, sharding, and async processing adds:
- Debugging complexity (where is the bug: cache, queue, database, replication?)
- Operational burden (more services to deploy, monitor, and maintain)
- Consistency challenges (more places where data can diverge)
- Development velocity cost (every feature must work across all layers)

Start simple. Add complexity only when measurements prove it is needed. A well-indexed PostgreSQL database with a Redis cache in front of it can serve millions of users before you need anything resembling TAO.
