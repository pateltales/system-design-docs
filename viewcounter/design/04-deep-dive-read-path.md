# View Counter — Read Path & Caching Strategy Deep Dive

> Companion deep dive to the [interview simulation](01-interview-simulation.md). This document dissects how view counts are served to billions of daily page loads with sub-10ms latency, how multi-layer caching absorbs read traffic, and how pre-aggregated materialized views power creator analytics.

---

## Table of Contents

1. [Read Patterns and Latency Requirements](#1-read-patterns-and-latency-requirements)
2. [Caching Strategy — Multi-Layer Architecture](#2-caching-strategy--multi-layer-architecture)
3. [Consistency Model](#3-consistency-model)
4. [Materialized Views for Analytics](#4-materialized-views-for-analytics)
5. [Read Path API Implementation Details](#5-read-path-api-implementation-details)
6. [Performance Optimization](#6-performance-optimization)
7. [Interview Quick-Reference Cheat Sheet](#7-interview-quick-reference-cheat-sheet)

---

## 1. Read Patterns and Latency Requirements

The read path for a view counter system serves fundamentally different consumers with
different tolerance for latency, staleness, and query complexity. An L6 candidate must
identify these patterns up front because they drive every downstream decision — caching
TTLs, storage engine choice, and consistency guarantees.

### 1.1 The Four Read Patterns

| Pattern | Example Surface | Query Shape | Latency Target | Staleness Tolerance | QPS Estimate |
|---------|----------------|-------------|----------------|---------------------|--------------|
| **Single video count** | Video watch page | `GET /videos/{id}/viewCount` | < 10 ms | 30-60 seconds | ~2M QPS |
| **Batch video counts** | Homepage, search results, recommendations | `GET /videos/batch/viewCounts?ids=...` (20-50 IDs) | < 50 ms total | 30-60 seconds | ~500K QPS (each fanning out to 20-50 keys) |
| **Channel aggregate** | YouTube Studio dashboard | `GET /channels/{id}/totalViews` | < 200 ms | 1-5 minutes | ~50K QPS |
| **Time-series analytics** | YouTube Studio analytics tab | `GET /videos/{id}/analytics?start=...&end=...&granularity=hour` | < 2 seconds | 1-5 minutes | ~10K QPS |

### 1.2 Why These Patterns Matter for Architecture

```
                               Read Traffic Distribution

  ┌─────────────────────────────────────────────────────────────────────┐
  │  Single video count + Batch counts = ~95% of all read traffic      │
  │  ─────────────────────────────────────────────────────────────────  │
  │                                                                     │
  │  These MUST be served from cache. Hitting a database for 2.5M QPS  │
  │  of simple key lookups is wasteful and fragile.                     │
  │                                                                     │
  │  Analytics queries = ~5% of traffic but 95% of query complexity    │
  │  ─────────────────────────────────────────────────────────────────  │
  │                                                                     │
  │  These need specialized storage (ClickHouse / Druid), not the      │
  │  same Redis cluster serving real-time counts.                       │
  └─────────────────────────────────────────────────────────────────────┘
```

**Key insight**: The read path is actually simple compared to the write path.
View counts are a single integer per video. The hard part is keeping that integer
fresh with minimal infrastructure cost while serving it at CDN-like latency.

### 1.3 Traffic Amplification: Why Batch Matters

A single homepage render fetches 30-50 video thumbnails, each needing a view count.
Without batch APIs, a single page load generates 30-50 individual requests. At YouTube's
scale (~500M daily active users), this is:

```
500M users x ~10 page loads/day x 40 videos/page = 200 billion key lookups/day
                                                  = ~2.3 million lookups/second
```

Batch fetching collapses 40 network round trips into 1, reducing:
- Client-side latency by 10-20x (1 RTT vs. 40 sequential RTTs)
- Connection overhead on the server (40x fewer TCP handshakes)
- Load balancer traffic (40x fewer requests to route)

This is why `MGET` in Redis and multi-row `IN` clauses in SQL exist.

---

## 2. Caching Strategy — Multi-Layer Architecture

### 2.1 The Multi-Layer Cache Stack

```
  ┌────────────────────────────────────────────────────────────────┐
  │                        CLIENT DEVICE                           │
  │   Local cache: browser/app caches view count per video for     │
  │   the duration of the session. No refetch on back-navigation.  │
  │   TTL: session-scoped (until user leaves the page)             │
  └───────────────────────┬────────────────────────────────────────┘
                          │ HTTPS request
                          ▼
  ┌────────────────────────────────────────────────────────────────┐
  │                     CDN EDGE (CloudFront / Akamai)             │
  │                                                                │
  │   Cache-Control: public, max-age=30, stale-while-revalidate=30│
  │                                                                │
  │   Popular videos: cached at 200+ edge locations worldwide.     │
  │   ~90-95% hit rate for top 1% of videos (power law).           │
  │   TTL: 30 seconds for public counts.                           │
  │                                                                │
  │   Long-tail videos: unlikely to be cached (evicted by LRU).   │
  │   These fall through to the origin.                            │
  └───────────────────────┬────────────────────────────────────────┘
                          │ Cache MISS (5-10% of requests)
                          ▼
  ┌────────────────────────────────────────────────────────────────┐
  │              APPLICATION-LEVEL CACHE (Redis Cluster)           │
  │                                                                │
  │   Write-through: aggregation pipeline writes here directly     │
  │   after flushing to DB. No explicit invalidation needed.       │
  │                                                                │
  │   ~100% hit rate (every video has a cached count because the   │
  │   write path populates it). Misses only for brand-new videos   │
  │   before their first aggregation flush.                        │
  │                                                                │
  │   Capacity: ~800M videos x 8 bytes = ~6.4 GB of counters.     │
  │   With overhead (keys, metadata, sharded counters): ~50-100 GB │
  │   Latency: < 1 ms (in-region network hop)                     │
  └───────────────────────┬────────────────────────────────────────┘
                          │ Cache MISS (extremely rare: <0.01%)
                          ▼
  ┌────────────────────────────────────────────────────────────────┐
  │                 DURABLE STORAGE (Cassandra / MySQL)            │
  │                                                                │
  │   Source of truth. Counter value persisted durably.            │
  │   Only hit on Redis miss or cold start / cache rebuild.        │
  │   Latency: 5-20 ms depending on storage engine and load.      │
  └────────────────────────────────────────────────────────────────┘
```

### 2.2 CDN-Level Caching

**Why CDN caching works for view counts:**

View counts are *public, non-personalized data*. Every user seeing a video gets the
same count. This makes view counts a perfect CDN candidate — unlike a user's watch
history or subscription feed, which are personalized and uncacheable at the CDN.

**TTL selection — the tradeoff:**

| TTL | Staleness | Origin Load Reduction | When to Use |
|-----|-----------|----------------------|-------------|
| 10 seconds | Minimal | ~70% | Premium/monetized videos where advertisers demand accuracy |
| 30 seconds | Acceptable | ~90% | Default for all public video counts |
| 60 seconds | Noticeable for viral videos | ~95% | Long-tail videos with slow count growth |
| 5 minutes | Significant | ~99% | Archived/old videos where counts rarely change |

**Adaptive TTL** (L6-level optimization): Set TTL inversely proportional to the
video's view velocity. A viral video gaining 100K views/minute gets a 10s TTL.
A 5-year-old video getting 2 views/hour gets a 5-minute TTL. This is implementable
via `Cache-Control` headers set dynamically by the origin based on recent view rate.

```
// Pseudocode: dynamic TTL at the origin
int viewsPerMinute = getRecentViewRate(videoId);
int ttlSeconds;
if (viewsPerMinute > 10000) {
    ttlSeconds = 10;     // Viral: refresh every 10s
} else if (viewsPerMinute > 100) {
    ttlSeconds = 30;     // Active: refresh every 30s
} else {
    ttlSeconds = 300;    // Dormant: refresh every 5 min
}
response.setHeader("Cache-Control",
    "public, max-age=" + ttlSeconds + ", stale-while-revalidate=" + ttlSeconds);
```

**`stale-while-revalidate` — why it matters:**

This HTTP directive tells the CDN: "Serve the stale cached response immediately while
fetching a fresh one in the background." The user gets a fast response (cached), and the
cache is refreshed for the next request. Without it, the first request after TTL expiry
blocks on an origin fetch — bad for tail latency.

### 2.3 Application-Level Cache (Redis)

The Redis layer differs from the CDN layer in a critical way: it is **write-through**,
not read-through.

**Read-through cache** (traditional):
```
  Request → Cache miss → Read from DB → Store in cache → Return
  Problem: Who invalidates the cache when the count changes?
```

**Write-through cache** (what we use):
```
  Aggregation pipeline flushes count → Writes to Redis AND DB simultaneously
  Request → Cache hit (always) → Return

  No invalidation needed. The write path keeps the cache warm.
```

This eliminates the entire class of cache consistency bugs. The cache is never stale
relative to the DB because every write updates both. The count in Redis is always
*at least as fresh* as the count in the durable store.

**Why not Memcached?**

| Feature | Redis | Memcached |
|---------|-------|-----------|
| Atomic increment (`INCR`) | Yes | Yes |
| Data structures (sorted sets for leaderboards) | Yes | No |
| Persistence (RDB/AOF) | Yes | No |
| Cluster mode (auto-sharding) | Yes | Requires client-side sharding |
| Pub/Sub (for cache invalidation signals) | Yes | No |
| Memory efficiency | Lower (richer data model) | Higher |

Redis wins because the write path uses `INCRBY` (atomic increment by a delta), and
we also use sorted sets for trending/leaderboard features. Memcached would require
a separate system for those use cases.

### 2.4 Cache Stampede on Viral Videos

**The problem:**

When a cache entry expires for a viral video, thousands of concurrent requests arrive
within the same millisecond. All of them see a cache miss. All of them query the database.
The database gets hammered with thousands of identical queries simultaneously.

```
  Time ──────────────────────────────────────────────►

  TTL expires
       │
       ▼
  ┌─── Request 1 ──→ Cache MISS ──→ Query DB ──→ Write cache ──→ Return
  ├─── Request 2 ──→ Cache MISS ──→ Query DB ──→ Write cache ──→ Return
  ├─── Request 3 ──→ Cache MISS ──→ Query DB ──→ Write cache ──→ Return
  ├─── ...
  └─── Request N ──→ Cache MISS ──→ Query DB ──→ Write cache ──→ Return

  All N requests hit the DB simultaneously. N can be thousands for a viral video.
```

**Solution 1: Lock-based refresh (request coalescing)**

Only one request is allowed to refresh the cache. Others wait for the lock holder
to populate the cache, then read from cache.

```java
public Long getViewCount(String videoId) {
    Long count = redis.get("views:" + videoId);
    if (count != null) {
        return count;  // Cache hit — fast path
    }

    // Cache miss — try to acquire refresh lock
    String lockKey = "lock:views:" + videoId;
    boolean acquired = redis.set(lockKey, "1", SetParams.setParams().nx().ex(5));

    if (acquired) {
        // Winner: fetch from DB, populate cache
        count = db.getViewCount(videoId);
        redis.setex("views:" + videoId, 60, count.toString());
        redis.del(lockKey);
        return count;
    } else {
        // Loser: spin-wait for cache to be populated (with timeout)
        for (int i = 0; i < 50; i++) {  // Max 500ms wait
            Thread.sleep(10);
            count = redis.get("views:" + videoId);
            if (count != null) return count;
        }
        // Fallback: query DB directly (lock holder may have failed)
        return db.getViewCount(videoId);
    }
}
```

**Tradeoff**: Adds complexity and latency for "loser" requests (they spin-wait).
If the lock holder crashes, others wait until timeout. The fallback query to the DB
partially defeats the purpose.

**Solution 2: Probabilistic early expiration (XFetch)**

Each request independently decides whether to refresh the cache *before* the TTL
expires. The probability increases as the TTL approaches zero. This distributes
the refresh over time rather than concentrating it at the TTL boundary.

```java
// XFetch algorithm (Vattani et al., 2015)
public Long getViewCountXFetch(String videoId) {
    String cacheKey = "views:" + videoId;
    CacheEntry entry = redis.getWithMetadata(cacheKey);

    if (entry == null) {
        // True miss — fetch and populate
        return fetchAndCache(videoId);
    }

    long ttlRemaining = entry.ttlMillis();
    long delta = entry.computeTime();  // How long the last fetch took

    // Probability of early refresh increases as TTL approaches 0
    // XFetch formula: currentTime - (delta * beta * ln(random)) > expiry
    double beta = 1.0;  // Tuning parameter
    boolean shouldRefresh = System.currentTimeMillis()
        - (delta * beta * Math.log(Math.random())) > entry.expiryTime();

    if (shouldRefresh) {
        // Proactively refresh in background
        asyncRefresh(videoId);
    }

    return entry.value();  // Always return the (possibly stale) cached value
}
```

**Tradeoff**: Elegant and distributed (no locks), but a few requests still see stale
data during the refresh window. Also, multiple requests may simultaneously decide to
refresh (wasted work but not harmful).

**Solution 3: Never-expire write-through (recommended for view counts)**

The write path *always* updates Redis. The cache entry has no TTL — it never expires.
Cache stampede is impossible because the entry is always present.

```
  Aggregation consumer ──INCRBY──► Redis ──── always warm ────► Reads
                        └──────► Cassandra (durable backup)
```

**This is the best solution for view counts** because:
- The aggregation pipeline runs continuously (every 5-10 seconds), so Redis is
  always updated. There is no "expiry" event.
- If the pipeline has a temporary outage, the last-known count remains in Redis.
  It becomes stale but never disappears. Users see a slightly outdated count rather
  than an error.
- No locks, no probability math, no stampede. Simple and correct.

**When you still need TTL-based caching**: The CDN layer still uses TTLs because
CDN edges don't receive write-through updates from the aggregation pipeline.
That's fine — the CDN is a *read-through* cache in front of the origin.

### 2.5 Cache Hit Rates and Capacity Planning

**Power law distribution of views:**

~1% of videos account for ~80% of all view-count reads. This is the Pareto
distribution at work, and it means a relatively small cache can absorb the
vast majority of traffic.

```
  Read Frequency
  │
  │ ██
  │ ██
  │ ██ ██
  │ ██ ██
  │ ██ ██ ██
  │ ██ ██ ██ ██
  │ ██ ██ ██ ██ ██ ██ ██ ██ ██ ██ ██ ██ ██ ██ ██ ██ ██ ██ ██ ██
  └────────────────────────────────────────────────────────────────►
   Top 1%   Top 10%                   Long tail (90% of videos)

   ◄── CDN cache hits ──►
   ◄───────────── Redis cache hits (all videos) ──────────────────►
```

**Capacity planning for Redis:**

| Component | Size per Video | 800M Videos | Notes |
|-----------|---------------|-------------|-------|
| View count (int64) | 8 bytes | 6.4 GB | Base counter |
| Key overhead (`views:{videoId}`) | ~40 bytes | 32 GB | Redis key + metadata |
| Sharded counters (hot videos, 100 shards each) | 100 x 48 bytes | ~5 GB (for top 100K videos) | Only for viral videos |
| HyperLogLog (unique viewers) | 12 KB | Selective: top 10M videos = 120 GB | Too expensive for all videos |
| **Total** | | **~160-200 GB** | Fits in a single large Redis Cluster |

A Redis Cluster with 10 shards, each being an `r6g.2xlarge` (52 GB RAM), gives
~520 GB total capacity with room for growth. At ~$0.50/GB-hour on AWS, this
costs approximately $260/hour or ~$190K/month. For a system serving billions
of daily page views, this is a rounding error.

---

## 3. Consistency Model

### 3.1 The Consistency Spectrum

View counts exist on a spectrum of consistency requirements depending on who is
reading and why. An L6 candidate demonstrates maturity by matching the consistency
guarantee to the consumer rather than applying a blanket policy.

```
  ◄──── Weaker ──────────────────────────────────────── Stronger ────►

  CDN-cached       Public count       Creator Studio       Ad billing
  count            on watch page      analytics            (revenue)

  Stale by         Stale by           Stale by             Exact, auditable,
  30-60 sec        5-10 sec           1-5 min              reconciled daily

  Cheapest to      Served from        Served from          Served from batch
  serve. No        Redis write-       pre-aggregated       pipeline output
  origin hit.      through cache.     tables in            with dedup +
                                      ClickHouse.          fraud subtraction.
```

| Consumer | Consistency | Staleness Budget | Why This Level |
|----------|-------------|------------------|----------------|
| Anonymous viewer on watch page | Eventual | 30-60 seconds | Users cannot perceive second-level accuracy on a number like "1,247,832 views" |
| Homepage thumbnail grid | Eventual | 30-60 seconds | Batch fetched; CDN cacheable; stale counts are invisible |
| Creator in YouTube Studio | Eventually consistent (tighter) | 1-5 minutes | Creators are sensitive to "my count isn't updating" but minutes-level lag is acceptable |
| Advertiser billing/CPM | Strong (batch-reconciled) | Up to 24 hours but **exact** | Revenue-critical. Must subtract fraud. Daily batch reconciliation from raw event log |
| Internal trending algorithm | Eventual | 5-15 minutes | Trending is computed from view velocity; a few minutes of lag doesn't change rankings meaningfully |

### 3.2 YouTube's Historical "301 Freeze"

Before ~2015, YouTube famously froze view counts at 301 for newly uploaded videos.
This happened because YouTube ran a synchronous fraud verification pipeline before
committing view counts beyond 300. The 301st view triggered a batch verification pass.

```
  Upload video
       │
       ▼
  Views 1-300 ──► Counted immediately (low risk of fraud at small scale)
       │
       ▼
  View 301 ──► FREEZE. Count displays "301 views" on the UI.
       │          Background fraud analysis runs on accumulated events.
       ▼
  Verification completes ──► Count jumps to actual number (e.g., 50,247)
```

**Why 301?** The story goes that it was an off-by-one bug that became a cultural
phenomenon. The real reason was that YouTube's original system used a simple
`count > 300` threshold to trigger verification, and the `>=` vs `>` ambiguity
led to 301 as the displayed number.

**Modern approach**: YouTube moved to a pipeline model where views are counted in
real time with *retroactive subtraction*. Counts can increase and occasionally
decrease (when fraud is detected). This eliminated the visible freeze but
introduced the need for the multi-stage fraud detection pipeline discussed in
[05-deep-dive-fraud-detection.md](05-deep-dive-fraud-detection.md).

### 3.3 Read-Your-Writes (Or Lack Thereof)

**Question**: After a user watches a video, should they see the count increment?

**Answer**: No, and here's why:

1. **Users don't track the exact number.** If the count was 1,247,832 before you
   watched, would you notice if it still says 1,247,832 after? No. You don't remember
   the exact number.

2. **Multiple other users are watching simultaneously.** The count is changing
   continuously. Your individual view is lost in the noise.

3. **Implementing read-your-writes is expensive.** It requires either:
   - Sticky sessions (route the user to the same cache/replica that received their write)
   - Session-scoped write tracking (maintain a "pending writes" set per user session)
   Both add complexity and reduce cacheability (the CDN can no longer serve a shared response).

4. **The fraud pipeline may reject the view.** If the view fails validation (Stage 3
   watch-time check, Stage 4 bot detection), incrementing the count on the client side
   and then un-incrementing is worse UX than never showing the increment.

**The one exception**: YouTube Studio. When a creator refreshes their analytics page,
they expect to see their most recent data. Here, read-your-writes matters more — and
it's achieved by having the analytics queries read from the speed layer (real-time
stream processing output) rather than only the batch layer.

### 3.4 Consistency During Failures

What happens when components fail? The consistency guarantee degrades gracefully:

```
  Scenario                          │ Behavior
  ──────────────────────────────────┼──────────────────────────────────────
  Redis master failover             │ Replica promoted. May lose 1-2 seconds
                                    │ of increments. Count momentarily stale
                                    │ by a few thousand views. Self-heals on
                                    │ next aggregation flush.
  ──────────────────────────────────┼──────────────────────────────────────
  Aggregation pipeline lag/backlog  │ Redis count stops updating. CDN serves
                                    │ increasingly stale data. Users see a
                                    │ "frozen" count. Resumes when pipeline
                                    │ catches up. No data loss (Kafka retains
                                    │ events).
  ──────────────────────────────────┼──────────────────────────────────────
  ClickHouse node down              │ Analytics queries fail or degrade.
                                    │ Read replicas serve queries at higher
                                    │ latency. Public view counts unaffected
                                    │ (different system).
  ──────────────────────────────────┼──────────────────────────────────────
  CDN edge outage                   │ Traffic routed to next-nearest edge or
                                    │ origin. Latency increases but correct-
                                    │ ness is maintained.
  ──────────────────────────────────┼──────────────────────────────────────
  Total Redis cluster failure       │ Fallback to Cassandra/MySQL for reads.
                                    │ Latency jumps from <1ms to 5-20ms.
                                    │ CDN absorbs most impact. Alert + page
                                    │ oncall immediately.
```

---

## 4. Materialized Views for Analytics

### 4.1 Why Not Query Raw Events?

At 800K views/second, the raw event stream produces:

```
  800,000 events/sec x 200 bytes/event = 160 MB/sec
                                       = 9.6 GB/min
                                       = 576 GB/hour
                                       = ~14 TB/day
                                       = ~5 PB/year
```

A creator asking "How many views did my video get last Tuesday between 2pm and 3pm?"
would require scanning potentially billions of raw events, filtering by videoId and
timestamp. Even with columnar storage and parallel processing, this takes minutes to hours.

**The solution**: Pre-aggregate into materialized summary tables at multiple granularities.

### 4.2 Pre-Aggregation by Time Window

```
  Raw Events (Kafka)              Minute Aggregates           Hour Aggregates
  ┌──────────────────┐           ┌───────────────────┐       ┌───────────────────┐
  │ videoId: abc123   │           │ videoId: abc123    │       │ videoId: abc123    │
  │ ts: 14:03:22.417 │    ──►    │ window: 14:03      │  ──►  │ window: 14:00     │
  │ userId: u789      │  Flink   │ count: 4,271       │ Roll  │ count: 247,832    │
  │ country: US       │  window  │ unique: ~3,100     │  up   │ unique: ~189,000  │
  │ device: mobile    │  agg.    │ by_country:        │ job   │ by_country:       │
  └──────────────────┘           │   US: 1,842        │       │   US: 107,291     │
  (200 bytes, ephemeral)          │   IN: 891         │       │   IN: 52,103      │
                                  │   BR: 634         │       │   BR: 31,847      │
                                  │ by_device:         │       │ ...               │
                                  │   mobile: 2,891   │       │                   │
                                  │   desktop: 1,380  │       │                   │
                                  └───────────────────┘       └───────────────────┘
                                  (kept 48 hours)             (kept 90 days)

                                                              Day Aggregates
                                                              ┌───────────────────┐
                                                              │ videoId: abc123    │
                                                         ──►  │ date: 2025-03-15  │
                                                         Roll │ count: 5,847,291  │
                                                          up  │ unique: ~4.1M     │
                                                         job  │ by_country: ...   │
                                                              │ by_device: ...    │
                                                              └───────────────────┘
                                                              (kept forever)
```

**Retention policy and storage savings:**

| Granularity | Retention | Records per Video per Year | Storage per Video |
|-------------|-----------|---------------------------|-------------------|
| Raw events | 7 days (in Kafka), 90 days (cold S3) | ~25 billion (for a popular video) | ~5 TB |
| 1-minute | 48 hours | 525,600 | ~50 MB |
| 1-hour | 90 days | 8,760 | ~850 KB |
| 1-day | Forever | 365 | ~35 KB |
| 1-month | Forever | 12 | ~1.2 KB |

The reduction from raw events to daily aggregates is **~8 orders of magnitude**
in storage. This is why materialized views are non-negotiable for analytics at scale.

### 4.3 Lambda Architecture

The Lambda architecture uses two parallel paths to combine the accuracy of batch
processing with the freshness of stream processing.

```
                          ┌─────────────────────────────────────────────┐
                          │              Raw Event Stream               │
                          │                (Kafka)                      │
                          └──────┬───────────────────────┬──────────────┘
                                 │                       │
                    ┌────────────▼──────────┐  ┌────────▼──────────────┐
                    │     BATCH LAYER       │  │     SPEED LAYER       │
                    │                       │  │                       │
                    │  Daily Spark job:     │  │  Flink / Kafka        │
                    │  - Reads ALL raw      │  │  Streams:             │
                    │    events from S3     │  │  - Real-time window   │
                    │  - Exact dedup via    │  │    aggregation        │
                    │    event IDs          │  │  - Approximate dedup  │
                    │  - Fraud subtraction  │  │    (Bloom filter)     │
                    │  - Produces EXACT     │  │  - Produces APPROX    │
                    │    counts per video   │  │    counts per video   │
                    │    per day            │  │    per minute         │
                    │                       │  │                       │
                    │  Output: batch_views  │  │  Output: rt_views     │
                    │  table in ClickHouse  │  │  table in ClickHouse  │
                    │                       │  │                       │
                    │  Latency: T+24 hours  │  │  Latency: T+5 seconds │
                    │  Accuracy: 100%       │  │  Accuracy: ~99.5%     │
                    └────────────┬──────────┘  └────────┬──────────────┘
                                 │                       │
                    ┌────────────▼───────────────────────▼──────────────┐
                    │                SERVING LAYER                      │
                    │                                                   │
                    │  Query: "views for video X on March 15?"          │
                    │                                                   │
                    │  If March 15 batch data exists:                   │
                    │    → Return batch_views (exact)                   │
                    │  Else:                                            │
                    │    → Return rt_views (approximate, for today)     │
                    │                                                   │
                    │  Merge: batch_views (completed days) +            │
                    │         rt_views (current partial day)            │
                    └──────────────────────────────────────────────────┘
```

**Strengths of Lambda:**
- Batch layer produces *exact, deduped, fraud-adjusted* counts. It is the source of truth.
- Speed layer provides real-time freshness. Approximate but useful.
- If the speed layer has bugs, the batch layer corrects it the next day.

**Weaknesses of Lambda:**
- **Two codebases**: The batch job (Spark) and the stream job (Flink) implement the
  *same logic* in different frameworks. Keeping them in sync is a maintenance burden.
- **Merge logic is tricky**: The serving layer must know which time ranges are covered
  by batch vs. speed layer. Off-by-one errors in time boundaries cause double-counting
  or missing counts.
- **Batch lag**: Today's data is always approximate until the nightly batch runs. For
  creator analytics ("how's my video doing right now?"), this is fine. For billing
  reconciliation, you wait for the batch.

### 4.4 Kappa Architecture

The Kappa architecture eliminates the batch layer entirely. A single stream processing
pipeline handles both real-time and historical data.

```
  ┌─────────────────────────────────────────────────────────────────┐
  │                    Raw Event Stream (Kafka)                     │
  │                                                                 │
  │  Retention: 7 days (or longer for reprocessing capability)     │
  └───────────────────────────┬─────────────────────────────────────┘
                              │
                              ▼
  ┌─────────────────────────────────────────────────────────────────┐
  │               STREAM PROCESSING (Flink / Kafka Streams)        │
  │                                                                 │
  │  Single pipeline that:                                         │
  │  1. Reads events from Kafka                                    │
  │  2. Deduplicates (Bloom filter + windowed exact dedup)         │
  │  3. Aggregates by time window (minute, hour, day)              │
  │  4. Writes to ClickHouse / Druid                               │
  │                                                                 │
  │  For reprocessing (bug fix, schema change):                    │
  │  - Deploy a NEW version of the pipeline                        │
  │  - Point it at Kafka offset 0 (or S3 archive)                 │
  │  - Reprocess all events through the new pipeline               │
  │  - Swap the output table when reprocessing completes           │
  └───────────────────────────┬─────────────────────────────────────┘
                              │
                              ▼
  ┌─────────────────────────────────────────────────────────────────┐
  │              SERVING LAYER (ClickHouse / Druid)                │
  │                                                                 │
  │  Single source of truth. No batch/speed merge needed.          │
  │  Queries always hit the same tables.                            │
  └─────────────────────────────────────────────────────────────────┘
```

**Strengths of Kappa:**
- **One codebase.** The aggregation logic exists in exactly one place. No dual-maintenance.
- **Simpler serving layer.** No merge logic. One set of tables, one query path.
- **Reprocessing** is done by replaying the event stream through a new pipeline version.

**Weaknesses of Kappa:**
- **Reprocessing is expensive and slow.** To fix a bug in the aggregation logic, you must
  replay potentially months of events. At 14 TB/day, reprocessing 30 days = 420 TB of data
  through the pipeline. Even with a large Flink cluster, this takes hours to days.
- **Kafka retention limits.** Keeping 90+ days of raw events in Kafka is expensive
  (~$50K+/month for the broker storage). Alternative: archive to S3 and replay from there
  (but this requires additional tooling).
- **Exactly-once is harder in streaming.** Kafka Streams provides exactly-once within
  the Kafka ecosystem, but when writing to an external DB (ClickHouse), you need
  idempotent writes or two-phase commit. Flink's checkpoint mechanism helps but adds
  operational complexity.

### 4.5 Lambda vs. Kappa — Which to Choose?

| Dimension | Lambda | Kappa |
|-----------|--------|-------|
| Operational complexity | Higher (two systems) | Lower (one system) |
| Code maintenance | Two implementations | One implementation |
| Historical reprocessing | Fast (batch job over S3 is optimized) | Slow (replay stream) |
| Accuracy of recent data | Approximate (speed layer) | Same as historical (one pipeline) |
| Accuracy of historical data | Exact (batch layer) | Only as good as the pipeline logic |
| Industry trend (2024+) | Declining | Growing (Flink/Kafka ecosystem maturity) |

**Recommendation for a view counter**: Start with Lambda. The batch layer gives you
a safety net — if your stream processing has bugs (it will), the nightly batch job
corrects the counts. As confidence in the streaming pipeline grows and tooling matures,
migrate toward Kappa by making the batch job a "verification" run rather than the
primary source of truth.

---

## 5. Read Path API Implementation Details

### 5.1 Single Video Count — The Fast Path

```
  Client ──GET /videos/{videoId}/viewCount──► API Gateway ──► View Count Service
                                                                     │
                                                          ┌──────────▼──────────┐
                                                          │ Redis HGET or GET   │
                                                          │ Key: views:{videoId}│
                                                          │ Latency: <1ms       │
                                                          └──────────┬──────────┘
                                                                     │
                                                            Cache hit? ──► YES ──► Return
                                                                     │
                                                                    NO (rare)
                                                                     │
                                                          ┌──────────▼──────────┐
                                                          │ Cassandra / MySQL   │
                                                          │ SELECT count FROM   │
                                                          │ view_counts WHERE   │
                                                          │ video_id = ?        │
                                                          │ Latency: 5-20ms    │
                                                          └──────────┬──────────┘
                                                                     │
                                                          Write-back to Redis
                                                                     │
                                                                  Return
```

**Response format:**

```json
{
  "videoId": "dQw4w9WgXcQ",
  "viewCount": 1423847291,
  "formattedCount": "1.4B views",
  "lastUpdated": "2025-03-15T14:23:07Z",
  "precision": "approximate"
}
```

The `precision` field communicates to the client that this is an approximate count.
The `formattedCount` field saves the client from implementing locale-specific number
formatting (1.4B in English, 14億 in Japanese, 1,4 Mrd in German).

**Implementation:**

```java
@GetMapping("/videos/{videoId}/viewCount")
public ResponseEntity<ViewCountResponse> getViewCount(
        @PathVariable String videoId) {

    // 1. Try Redis
    String cached = redisTemplate.opsForValue().get("views:" + videoId);
    if (cached != null) {
        long count = Long.parseLong(cached);
        return ResponseEntity.ok()
            .cacheControl(CacheControl.maxAge(30, TimeUnit.SECONDS)
                .staleWhileRevalidate(30, TimeUnit.SECONDS))
            .body(new ViewCountResponse(videoId, count));
    }

    // 2. Fallback to DB
    long count = viewCountRepository.getCount(videoId);

    // 3. Backfill Redis (async, fire-and-forget)
    redisTemplate.opsForValue().set("views:" + videoId,
        String.valueOf(count), 300, TimeUnit.SECONDS);

    return ResponseEntity.ok()
        .cacheControl(CacheControl.maxAge(30, TimeUnit.SECONDS))
        .body(new ViewCountResponse(videoId, count));
}
```

### 5.2 Batch Count — The Workhorse

Batch fetching is where most of the read QPS lives. A single homepage render
triggers one batch request for 30-50 video IDs.

**Implementation:**

```java
@PostMapping("/videos/batch/viewCounts")
public ResponseEntity<BatchViewCountResponse> getBatchViewCounts(
        @RequestBody BatchRequest request) {

    List<String> videoIds = request.getVideoIds();
    if (videoIds.size() > 100) {
        return ResponseEntity.badRequest().build();  // Prevent abuse
    }

    // 1. MGET from Redis — single round trip for all keys
    List<String> keys = videoIds.stream()
        .map(id -> "views:" + id)
        .collect(Collectors.toList());
    List<String> cachedValues = redisTemplate.opsForValue().multiGet(keys);

    // 2. Identify misses
    Map<String, Long> results = new HashMap<>();
    List<String> missedIds = new ArrayList<>();

    for (int i = 0; i < videoIds.size(); i++) {
        String value = cachedValues.get(i);
        if (value != null) {
            results.put(videoIds.get(i), Long.parseLong(value));
        } else {
            missedIds.add(videoIds.get(i));
        }
    }

    // 3. Batch-fetch misses from DB (single query with IN clause)
    if (!missedIds.isEmpty()) {
        Map<String, Long> dbResults = viewCountRepository
            .getCountsBatch(missedIds);
        results.putAll(dbResults);

        // Backfill Redis asynchronously
        CompletableFuture.runAsync(() -> backfillRedis(dbResults));
    }

    return ResponseEntity.ok()
        .cacheControl(CacheControl.maxAge(30, TimeUnit.SECONDS))
        .body(new BatchViewCountResponse(results));
}
```

**Response format:**

```json
{
  "viewCounts": {
    "dQw4w9WgXcQ": { "count": 1423847291, "formatted": "1.4B" },
    "9bZkp7q19f0": { "count": 4892471032, "formatted": "4.8B" },
    "kJQP7kiw5Fk": { "count": 8247193642, "formatted": "8.2B" }
  },
  "timestamp": "2025-03-15T14:23:07Z"
}
```

**Why `MGET` and not a pipeline of `GET`s:**

| Approach | Network Round Trips | Latency (50 keys) |
|----------|--------------------|--------------------|
| 50 individual `GET` | 50 | ~50 ms (1ms each, sequential) |
| Pipelined `GET` | 1 (pipelined) | ~2 ms |
| `MGET` | 1 (atomic) | ~1 ms |

`MGET` is a single atomic command. Pipelining sends multiple commands in one batch
but they are processed sequentially on the Redis server. In practice, both are fast,
but `MGET` is semantically cleaner and marginally faster.

### 5.3 Analytics Queries — The Complex Path

Analytics queries differ fundamentally from count lookups. They are:
- Time-range based (not point lookups)
- Multi-dimensional (slice by country, device, traffic source)
- Tolerant of higher latency (1-2 seconds)
- Served from OLAP storage (ClickHouse/Druid), not Redis

**Example query: Views per day for a video over the last 30 days**

```sql
-- ClickHouse query against the daily aggregates table
SELECT
    toDate(window_start) AS date,
    sum(view_count) AS views,
    sum(unique_viewers) AS uniques
FROM daily_view_aggregates
WHERE video_id = 'dQw4w9WgXcQ'
  AND date >= today() - INTERVAL 30 DAY
  AND date <= today()
GROUP BY date
ORDER BY date ASC;
```

**Example query: Views by country for a video in the last 7 days**

```sql
SELECT
    country,
    sum(view_count) AS views,
    round(sum(view_count) * 100.0 / (
        SELECT sum(view_count) FROM daily_view_aggregates
        WHERE video_id = 'dQw4w9WgXcQ'
          AND date >= today() - INTERVAL 7 DAY
    ), 2) AS percentage
FROM daily_view_aggregates_by_country
WHERE video_id = 'dQw4w9WgXcQ'
  AND date >= today() - INTERVAL 7 DAY
GROUP BY country
ORDER BY views DESC
LIMIT 20;
```

**API response with pagination:**

```json
{
  "videoId": "dQw4w9WgXcQ",
  "granularity": "day",
  "startDate": "2025-02-13",
  "endDate": "2025-03-15",
  "dataPoints": [
    { "date": "2025-02-13", "views": 142384, "uniques": 98271 },
    { "date": "2025-02-14", "views": 156729, "uniques": 107432 },
    ...
    { "date": "2025-03-15", "views": 189274, "uniques": 134891 }
  ],
  "summary": {
    "totalViews": 4827391,
    "totalUniques": 3291847,
    "avgDailyViews": 160913
  },
  "pagination": {
    "hasMore": false,
    "cursor": null
  }
}
```

**Pagination for large time ranges:** When a creator queries a full year of hourly
data (8,760 data points), the response is paginated. The cursor is a timestamp
marking the last returned data point. The client fetches subsequent pages by passing
the cursor as a `startAfter` parameter.

---

## 6. Performance Optimization

### 6.1 Connection Pooling

Every read request needs a connection to Redis (and potentially to the DB). Opening
a new TCP connection per request is catastrophically slow at scale:

```
  TCP handshake:  ~0.5 ms (same region)
  TLS handshake:  ~1.5 ms (additional for encrypted connections)
  Redis command:   ~0.1 ms
  ─────────────────────────
  Total without pool: ~2.1 ms
  Total with pool:    ~0.1 ms (connection already established)
```

**Connection pool sizing:**

```java
// Redis connection pool (Lettuce / Jedis)
GenericObjectPoolConfig<StatefulRedisConnection<String, String>> poolConfig =
    new GenericObjectPoolConfig<>();
poolConfig.setMaxTotal(200);          // Max connections per app server
poolConfig.setMaxIdle(50);            // Keep 50 warm connections
poolConfig.setMinIdle(20);            // Never go below 20
poolConfig.setMaxWaitMillis(100);     // Fail fast if pool exhausted
poolConfig.setTestOnBorrow(true);     // Validate connection before use
```

**Pool size formula:**

```
connections_per_server = peak_QPS_per_server / (1000 / avg_redis_latency_ms)

Example:
  peak QPS per server = 10,000 req/s
  avg Redis latency = 0.5 ms
  connections needed = 10,000 / (1000 / 0.5) = 10,000 / 2000 = 5

  With 4x safety margin and burst headroom: 20 connections
  With 100 app servers: 2,000 total connections to Redis cluster
```

Redis Cluster can handle ~10,000 concurrent connections per node. With a 10-shard
cluster, that's 100,000 connections. Comfortable headroom for 100 app servers.

### 6.2 Redis Pipelining for Batch Operations

When a single request needs multiple Redis operations (e.g., fetching counts and
HLL unique-viewer estimates), pipeline them:

```java
// Without pipelining: 3 round trips = ~3ms
Long viewCount = redis.get("views:abc123");
Long uniqueViewers = redis.pfcount("hll:abc123");
Double trendScore = redis.zscore("trending:US", "abc123");

// With pipelining: 1 round trip = ~1ms
RedisFuture<String> viewCountFuture = asyncCommands.get("views:abc123");
RedisFuture<Long> uniqueFuture = asyncCommands.pfcount("hll:abc123");
RedisFuture<Double> trendFuture = asyncCommands.zscore("trending:US", "abc123");
asyncCommands.flushCommands();  // Send all at once

Long viewCount = Long.parseLong(viewCountFuture.get());
Long uniqueViewers = uniqueFuture.get();
Double trendScore = trendFuture.get();
```

### 6.3 Query Optimization for Time-Series Data

ClickHouse and Druid are columnar stores optimized for time-series queries. But
query performance still depends on proper table design.

**Partitioning:**

```sql
-- ClickHouse: partition by month, order by (video_id, date)
CREATE TABLE daily_view_aggregates (
    video_id     String,
    date         Date,
    view_count   UInt64,
    unique_count UInt64,
    country      LowCardinality(String),
    device_type  LowCardinality(String)
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(date)
ORDER BY (video_id, date)
SETTINGS index_granularity = 8192;
```

**Why this ordering matters:**

The `ORDER BY (video_id, date)` ensures that all data for a given video is
physically co-located on disk. A query for "video X, last 30 days" reads a
contiguous range of rows — a sequential scan rather than random I/O. With
ClickHouse's compression (~10:1 for numeric columns), this query touches
only a few kilobytes of data on disk.

**Query performance benchmarks (typical ClickHouse):**

| Query | Data Scanned | Latency |
|-------|-------------|---------|
| Single video, 30 days, daily granularity | ~30 rows | < 10 ms |
| Single video, 1 year, daily granularity | ~365 rows | < 20 ms |
| Single video, 30 days, hourly granularity | ~720 rows | < 15 ms |
| Top 100 videos by views, last 7 days | Scans full 7-day partition | 200-500 ms |
| All videos for a channel (1000 videos), 30 days | ~30,000 rows | 50-100 ms |

### 6.4 Read Replicas and Load Balancing

**Redis read replicas:**

For read-heavy workloads, configure Redis Cluster with 1-2 read replicas per shard.
Route read-only commands (`GET`, `MGET`) to replicas, keeping the master free for
writes (`INCRBY` from the aggregation pipeline).

```
  Aggregation pipeline ──INCRBY──► Redis Master (shard 1)
                                          │
                                    Async replication
                                          │
                                    ┌─────▼──────┐
                         Reads ──► │ Replica 1A  │
                         Reads ──► │ Replica 1B  │
                                    └────────────┘
```

**Replication lag concern:** Redis async replication has sub-millisecond lag under
normal conditions. During network partitions or master failover, lag can spike to
seconds. For view counts, this is irrelevant — a count that's stale by 1 second
is indistinguishable from one that's stale by 30 seconds (which the CDN already
introduces).

**ClickHouse read replicas:**

ClickHouse supports `ReplicatedMergeTree` tables with automatic data replication.
Use a load balancer (HAProxy, or ClickHouse's built-in distributed queries) to
spread analytics queries across replicas:

```
  Creator analytics request
           │
           ▼
  ┌─── Load Balancer (round-robin) ───┐
  │                                    │
  ▼                                    ▼
  ClickHouse Replica 1          ClickHouse Replica 2
  (handles query)               (handles query)
```

### 6.5 Client-Side Optimization

The API service isn't the only place to optimize reads. The client (YouTube
web/mobile app) can reduce read traffic significantly:

1. **Debounce refresh requests.** When a user scrolls through a feed, don't
   refetch view counts for videos they already have in memory.

2. **Subscribe to updates via WebSocket for live-streamed videos.** Instead
   of polling every 5 seconds for a live stream's viewer count, open a WebSocket
   and push updates from the server. Reduces QPS by orders of magnitude for
   live content.

3. **Stale-while-revalidate on the client.** Display the cached count immediately,
   fetch fresh data in the background. The user sees instant results; the update
   arrives a moment later (usually identical, so no visible flicker).

4. **Approximate formatting.** Display "1.4M views" instead of "1,423,847 views."
   This means the client doesn't need to refetch if the count changed by less than
   ~50K — the displayed string is identical. Set the refetch threshold based on
   the order of magnitude of the count.

---

## 7. Interview Quick-Reference Cheat Sheet

**When the interviewer asks about the read path, hit these points:**

| Topic | Key Point | L5 Answer | L6 Answer |
|-------|-----------|-----------|-----------|
| Caching layers | Multi-layer: CDN → Redis → DB | "We cache in Redis" | "CDN for public counts (TTL=30s, stale-while-revalidate), write-through Redis for all videos (no TTL, no stampede), DB as cold fallback" |
| Batch fetching | Use MGET, not N individual GETs | "We can batch requests" | "MGET for Redis, IN clause for DB fallback, async backfill of misses, limit batch size to 100 to prevent abuse" |
| Consistency | Eventual is fine for counts | "Eventual consistency" | Differentiate by consumer: viewers get 30-60s staleness, creators get 1-5min, billing gets exact-after-batch |
| Analytics storage | Pre-aggregate, don't scan raw | "Use a time-series DB" | "Minute/hour/day rollups in ClickHouse, partitioned by month, ordered by (video_id, date). Lambda architecture for batch-corrected accuracy + real-time freshness" |
| Cache stampede | Prevent thundering herd | "Add a lock" | "Write-through makes stampede impossible for Redis layer. CDN layer uses stale-while-revalidate. Lock-based refresh as defense-in-depth" |
| Hot keys | Viral videos concentrate load | "Shard the cache" | "Counter sharding on write path distributes writes. On read path, the CDN absorbs 90%+ of hot-key reads. Redis replicas handle the rest" |

**Numbers to have ready:**

- CDN hit rate for top 1% of videos: ~95%
- Redis latency (same-region): < 1 ms
- Redis MGET for 50 keys: < 2 ms
- ClickHouse query (single video, 30 days): < 20 ms
- Total Redis capacity for 800M video counters: ~200 GB
- Raw event volume: 14 TB/day, 5 PB/year
- Storage reduction via pre-aggregation: ~100,000,000x (raw to daily)
