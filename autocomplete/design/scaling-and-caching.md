# Autocomplete System — Scaling & Caching Deep Dive

> This document covers how to scale the autocomplete system to handle millions of requests per second with sub-100ms latency. Caching is the primary strategy — the trie itself is small enough to fit in memory, so the challenge is reducing the number of requests that even reach the trie servers. Sharding is the secondary strategy for future growth.

---

## Table of Contents

1. [Multi-Layer Caching Strategy](#1-multi-layer-caching-strategy)
2. [Browser / Client-Side Caching](#2-browser--client-side-caching)
3. [CDN Caching](#3-cdn-caching)
4. [Application-Level Redis Cache](#4-application-level-redis-cache)
5. [Trie In-Memory (The Server IS the Cache)](#5-trie-in-memory-the-server-is-the-cache)
6. [Cache Invalidation Strategies](#6-cache-invalidation-strategies)
7. [Trie Sharding Strategies](#7-trie-sharding-strategies)
8. [Replication for Availability](#8-replication-for-availability)
9. [Geographic Distribution (Multi-Region)](#9-geographic-distribution-multi-region)
10. [Capacity Planning](#10-capacity-planning)
11. [Load Testing and Performance Benchmarks](#11-load-testing-and-performance-benchmarks)

---

## 1. Multi-Layer Caching Strategy

The autocomplete system uses four layers of caching, each reducing the traffic that reaches the next layer. This is the primary mechanism for achieving sub-100ms latency at 1.7M req/sec.

### Request Flow Through Cache Layers

```
User types "ama" → debounce (150ms)
       │
       ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  LAYER 1: Browser Cache                                                │
│  Cache-Control: max-age=60, private                                   │
│  Key: prefix + language                                                │
│  HIT RATE: ~30%                                                        │
│  Latency: 0ms (local)                                                  │
│                                                                         │
│  Result: 510K req/sec never leave the browser                          │
└────────┬────────────────────────────────────────────────────────────────┘
         │ MISS (70% = 1.19M req/sec)
         ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  LAYER 2: CDN (CloudFront)                                             │
│  Cache-Control: max-age=300, public (non-personalized only)            │
│  Key: prefix + language (no user-specific params)                      │
│  HIT RATE: ~60% of remaining                                           │
│  Latency: ~5ms (edge POP)                                              │
│                                                                         │
│  Result: 714K req/sec served from CDN edge                             │
└────────┬────────────────────────────────────────────────────────────────┘
         │ MISS (28% of total = 476K req/sec)
         ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  LAYER 3: Redis Application Cache                                      │
│  TTL: 10 minutes                                                       │
│  Key: suggestions:{trie_version}:{prefix}                              │
│  HIT RATE: ~80% of remaining                                           │
│  Latency: ~2-5ms (network hop to Redis cluster)                        │
│                                                                         │
│  Result: 380K req/sec served from Redis                                │
└────────┬────────────────────────────────────────────────────────────────┘
         │ MISS (5.6% of total = 95K req/sec)
         ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  LAYER 4: Trie Server (in-memory trie)                                 │
│  No TTL (trie is always in memory, swapped on deploy)                  │
│  Always hits (the trie IS the source of truth)                         │
│  Latency: ~0.1-1ms (in-memory trie traversal)                         │
│                                                                         │
│  Result: 95K req/sec — the trie server's actual load                   │
│  Also: populates Redis cache for future hits                           │
└─────────────────────────────────────────────────────────────────────────┘
```

### Traffic Reduction Summary

| Layer | Input Rate | Hit Rate | Output Rate (Misses) | Requests Served |
|---|---|---|---|---|
| Browser | 1,700K/sec | 30% | 1,190K/sec | 510K/sec |
| CDN | 1,190K/sec | 60% | 476K/sec | 714K/sec |
| Redis | 476K/sec | 80% | 95K/sec | 381K/sec |
| Trie Server | 95K/sec | 100% | 0/sec | 95K/sec |

**94.4% of requests never reach the backend.** This is the power of multi-layer caching for a read-heavy, slowly-changing dataset.

---

## 2. Browser / Client-Side Caching

The first defense against server load is the browser itself. Since autocomplete results change slowly (hourly trie rebuilds), short-lived browser caching is highly effective.

### HTTP Caching Headers

```
HTTP/1.1 200 OK
Cache-Control: max-age=60, private
Vary: Accept-Language
ETag: "trie_v42_ama_en"
```

- `max-age=60`: Cache for 1 minute. Short enough that users get reasonably fresh data; long enough to avoid redundant requests.
- `private`: Don't cache in shared proxies — results may be personalized.
- `Vary: Accept-Language`: Different cache entries per language.
- `ETag`: Enables conditional requests (304 Not Modified) after cache expiry.

### Client-Side JavaScript Optimizations

Beyond HTTP caching, the client-side JavaScript implements additional optimizations:

#### Debouncing

```
// Only send a request after user stops typing for 150ms
let debounceTimer = null;

function onKeyPress(prefix) {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => {
        fetchSuggestions(prefix);
    }, 150);
}
```

**Impact**: Reduces requests by ~50%. For a 20-character query typed at 5 chars/sec (200ms between keystrokes), debouncing eliminates most intermediate requests.

#### Local Result Filtering

```
// If we have cached results for "am", and user types "ama",
// filter locally instead of making a new request

let cachedResults = {};

function getSuggestions(prefix) {
    // Check if we have a cached superset
    for (let len = prefix.length - 1; len >= 1; len--) {
        let shorterPrefix = prefix.substring(0, len);
        if (cachedResults[shorterPrefix]) {
            let filtered = cachedResults[shorterPrefix]
                .filter(s => s.text.startsWith(prefix));
            if (filtered.length >= 5) {
                // Enough results from local filtering
                return filtered.slice(0, 10);
            }
            break;  // not enough results, need server request
        }
    }
    // No suitable cache — fetch from server
    return fetchFromServer(prefix);
}
```

**Impact**: For incrementally typed prefixes ("a" → "am" → "ama" → "amaz"), the request for "am" can potentially serve "ama" and "amaz" locally — reducing requests by another 30-40%.

#### Prefetching

```
// When user focuses the search box, prefetch popular starting prefixes
function onSearchBoxFocus() {
    // Prefetch the most common 1-character prefixes
    ['a', 'i', 's', 'b', 'c', 'n', 'p', 'h', 'm', 'w'].forEach(prefix => {
        prefetch(`/v1/suggestions?prefix=${prefix}&limit=10`);
    });
}
```

**Impact**: By the time the user starts typing, we already have cached results for common first characters.

---

## 3. CDN Caching

The CDN (CloudFront) serves as a geographically distributed cache, providing low-latency responses from edge locations close to users.

### CloudFront Configuration

```
CloudFront Distribution:
    Origin: autocomplete-api.internal.amazon.com
    Behaviors:
        /v1/suggestions*:
            Allowed Methods: GET, HEAD
            Cache Policy:
                TTL: min=60s, default=300s, max=600s
                Cache Key: prefix, language (query params)
                Headers forwarded: Accept-Language
                Cookies forwarded: None
            Origin Request Policy:
                Forward all query params to origin on miss
```

### Cache Key Design

The CDN cache key must balance hit rate against correctness:

| Component | Included? | Rationale |
|---|---|---|
| `prefix` | ✅ Yes | Core differentiator — "ama" and "amb" have different results |
| `language` | ✅ Yes | Different languages have different tries |
| `limit` | ❌ No (standardize to 10) | Avoid cache fragmentation. Always return 10, client truncates |
| `user_id` | ❌ No | Would make every user a cache miss. Personalized requests bypass CDN |
| `category` | ❌ No (for CDN) | Category-filtered requests bypass CDN, go directly to origin |

### Personalized vs Global Request Routing

```
                                ┌────────────────┐
                                │  API Gateway   │
                                │  (Route53)     │
                                └───────┬────────┘
                                        │
                          ┌─────────────┼──────────────┐
                          │             │              │
                     Global         Personalized    Admin
                   (cacheable)      (not cacheable) (internal)
                          │             │              │
                    ┌─────▼─────┐ ┌────▼──────┐  ┌───▼────┐
                    │ CloudFront│ │ ALB       │  │ ALB    │
                    │ (CDN)     │ │ (direct   │  │(admin) │
                    │           │ │  to origin)│  │        │
                    └───────────┘ └───────────┘  └────────┘
```

- **Global requests** (no `X-User-Id` header): Routed through CloudFront. Cacheable.
- **Personalized requests** (with `X-User-Id` header): Bypass CloudFront, go directly to the autocomplete service. Not cacheable at CDN level (but still cached in Redis keyed by `user_id:prefix`).

### CDN Hit Rate Optimization

Short prefixes have very high CDN hit rates because many users type the same starting characters:

| Prefix Length | Example | Unique Prefixes | CDN Hit Rate |
|---|---|---|---|
| 1 character | "a", "i", "s" | ~36 | ~99% |
| 2 characters | "am", "ip", "sa" | ~1,300 | ~95% |
| 3 characters | "ama", "iph", "sam" | ~20,000 | ~85% |
| 4 characters | "amaz", "ipho", "sams" | ~200,000 | ~60% |
| 5+ characters | "amazo", "iphon" | ~2,000,000+ | ~30% |
| **Weighted average** | | | **~60%** |

The 60% hit rate comes from the power-law distribution of prefixes — a small number of short prefixes account for a large fraction of traffic.

---

## 4. Application-Level Redis Cache

Redis serves as the last caching layer before the trie server. It catches CDN misses and provides sub-5ms responses.

### Redis Cluster Configuration

| Parameter | Value | Rationale |
|---|---|---|
| Cluster nodes | 6 (3 primary + 3 replica) | High availability across 3 AZs |
| Memory per node | 16 GB | Total: 96 GB (only ~500 MB used for suggestions cache) |
| Instance type | r6g.xlarge | Memory-optimized, graviton for cost |
| Max connections | 10,000 per node | Handle burst from autocomplete service fleet |

### Cache Key Design

```
Key format: suggestions:{trie_version}:{normalized_prefix}
Example:    suggestions:v42:ama

Value: JSON string
{
    "suggestions": [
        {"text": "amazon prime", "score": 0.95, "category": "general"},
        {"text": "amazon kindle", "score": 0.87, "category": "electronics"},
        ...
    ],
    "trie_version": "v42",
    "cached_at": 1707350400000
}

TTL: 600 seconds (10 minutes)
```

Including `trie_version` in the key prevents serving stale results after a trie deployment:
- Old entries (`v41:ama`) naturally expire via TTL
- New entries (`v42:ama`) are populated on first miss
- No explicit invalidation needed = no thundering herd

### Memory Sizing

```
Estimated unique cached prefixes:  500,000
Average value size:                1 KB (10 suggestions × ~100 bytes each)
Total memory:                      500K × 1 KB = 500 MB
With Redis overhead (~2x):         ~1 GB

This is tiny — fits easily on a single Redis node.
We have 96 GB total → 99% of Redis memory is unused.
```

### Warm-Up Strategy

After a new trie deployment, the Redis cache is cold (all keys use the new version prefix). We pre-populate the cache to avoid a burst of trie server traffic:

```
function warmUpCache(trie_version):
    // Get the top 100K most popular prefixes from the previous cache
    popular_prefixes = getPopularPrefixes(limit=100000)

    // Batch lookup and cache (rate-limited to not overwhelm trie servers)
    for batch in chunks(popular_prefixes, size=1000):
        results = trieServer.batchSearch(batch)
        for (prefix, suggestions) in results:
            redis.setex(
                key=f"suggestions:{trie_version}:{prefix}",
                ttl=600,
                value=json.dumps(suggestions)
            )
        sleep(100ms)  // rate limit: 10K prefixes/sec

    // Total warm-up time: 100K / 10K = 10 seconds
```

### Eviction Policy

Redis is configured with `maxmemory-policy: allkeys-lru` (Least Recently Used). Since we use only ~1 GB of 96 GB, eviction rarely triggers. If it does, rarely-accessed long-tail prefixes are evicted first — they'll be re-cached on the next request.

---

## 5. Trie In-Memory (The Server IS the Cache)

The trie servers are the ultimate "cache" — the trie data structure lives entirely in RAM, providing sub-millisecond lookups.

### Memory-Mapped File Loading

```
TRIE SERVER STARTUP SEQUENCE:

1. Check S3 for latest trie version
   → manifest.json: {"latest": "trie_v42.bin", "checksum": "sha256:..."}

2. Download trie binary to local disk
   → /opt/trie/trie_v42.bin (7.2 GB, ~30 seconds on EC2)

3. Memory-map the file
   → fd = open("/opt/trie/trie_v42.bin", O_RDONLY)
   → trie_data = mmap(fd, 7.2 GB, PROT_READ, MAP_PRIVATE)

4. Pre-fault all pages (avoid page faults during serving)
   → for offset in range(0, file_size, PAGE_SIZE=4096):
   →     volatile_read(trie_data[offset])
   → Time: ~5 seconds for 7.2 GB

5. Register health check
   → GET /health → 200 OK if trie loaded and pre-faulted

6. Accept traffic
   → Load balancer starts routing requests

TOTAL STARTUP: ~40 seconds
```

### Memory Layout Considerations

#### Off-Heap Memory (for JVM-based servers)

If the trie server is written in Java/Kotlin, the 7 GB trie must live **off-heap** to avoid GC pauses:

```
// Using direct ByteBuffer (off-heap)
MappedByteBuffer trieData = FileChannel.open(triePath)
    .map(MapMode.READ_ONLY, 0, fileSize);

// GC never touches this memory — no pause risk
// OS page cache manages the mmap'd region
```

GC pauses on a 7 GB heap could cause 50-100ms latency spikes — violating our p99 SLA.

#### Native Implementation (C++ / Rust)

For the lowest latency and most predictable performance, the trie server can be implemented in C++ or Rust:

| Language | Pros | Cons |
|---|---|---|
| Java/Kotlin | Familiar to most Amazon teams, good libraries | GC pauses (mitigated by off-heap), JVM startup time |
| C++ | No GC, lowest latency, mmap is natural | Memory safety risks, harder to maintain |
| Rust | No GC, memory-safe, good performance | Smaller talent pool, newer ecosystem |

**Recommendation**: Java with off-heap mmap for most teams. C++/Rust if latency requirements tighten below 10ms p99.

### Page Pre-Touching

After mmap, the OS has only created virtual memory mappings — no physical pages are loaded. The first access to each page triggers a **page fault**, causing a ~1ms delay. At 7.2 GB with 4 KB pages, that's 1.8M potential page faults.

Pre-touching forces all pages into memory at startup:

```
// Pre-touch all pages (linear scan)
void preTouchPages(void* data, size_t size) {
    volatile char c;
    for (size_t offset = 0; offset < size; offset += PAGE_SIZE) {
        c = ((char*)data)[offset];  // trigger page fault
    }
}
// Time: ~5 seconds for 7.2 GB (sequential I/O)
```

After pre-touching, all subsequent accesses are pure memory reads — no I/O, no page faults.

---

## 6. Cache Invalidation Strategies

Cache invalidation is one of the two hard problems in computer science. For autocomplete, we need to balance freshness (serving new trie data quickly) against stability (avoiding thundering herd).

### Strategy Comparison

| Strategy | How It Works | Thundering Herd? | Staleness | Complexity |
|---|---|---|---|---|
| **TTL-based** | Entries expire after fixed time (e.g., 10 min) | No | Up to TTL duration | Low |
| **Event-based** | Invalidate all entries on trie deploy | ⚠️ Yes — all traffic hits backend simultaneously | None | Medium |
| **Versioned keys** | Include trie version in cache key | No | During transition period | Low |
| **Staggered invalidation** | Invalidate 10% at a time over 5 min | Minimal | Variable | High |
| **Background refresh** | Refresh entries before TTL expiry | No | Minimal | Medium |

### Recommended: Versioned Keys + TTL

Our approach combines versioned cache keys with TTL-based expiration:

```
BEFORE TRIE DEPLOY (v41 active):
    Cache entries: suggestions:v41:ama, suggestions:v41:iph, ...
    All requests use trie version v41 as part of the key

AFTER TRIE DEPLOY (v42 active):
    New requests generate keys: suggestions:v42:ama, suggestions:v42:iph, ...
    Old entries (v41:*) still exist but are no longer hit
    They expire naturally via TTL (10 min)

    TIMELINE:
    t=0:   Deploy v42. 100% of requests are cache misses (new version).
    t=30s: Redis warm-up populates top 100K prefixes for v42.
    t=1m:  ~90% of popular prefixes cached for v42.
    t=10m: All v41 entries have expired. Only v42 remains.
```

### Thundering Herd Prevention

Even with versioned keys, a new trie deploy means the cache is effectively cold for the new version. The warm-up strategy (Section 4) mitigates this, but there's still a burst of cache misses in the first 30 seconds.

Additional mitigation: **request coalescing** (also called "single-flight"):

```
function getSuggestionsWithCoalescing(prefix, trie_version):
    cache_key = f"suggestions:{trie_version}:{prefix}"

    // Check cache
    result = redis.get(cache_key)
    if result:
        return result

    // Check if another request is already fetching this prefix
    if inflightRequests.contains(cache_key):
        // Wait for the in-flight request to complete
        return inflightRequests.await(cache_key)

    // I'm the first — fetch from trie server
    inflightRequests.register(cache_key)
    try:
        result = trieServer.search(prefix)
        redis.setex(cache_key, 600, result)
        inflightRequests.complete(cache_key, result)
        return result
    except:
        inflightRequests.fail(cache_key)
        raise
```

This ensures that for a given prefix, only **one** request hits the trie server — all concurrent requests for the same prefix wait for the first one to complete.

---

## 7. Trie Sharding Strategies

Currently, the trie fits in ~7 GB of memory — comfortably on a single server. We use **full replication** (every server has the complete trie). But what if the trie grows to 50+ GB?

### Strategy 1: Full Replication (Current)

```
                    ┌──────────────┐
                    │ Load Balancer│
                    │ (round-robin)│
                    └──────┬───────┘
                           │
              ┌────────────┼────────────┐
              │            │            │
       ┌──────▼──────┐ ┌──▼──────┐ ┌──▼──────────┐
       │ Trie Server │ │ Trie    │ │ Trie Server │
       │ A (AZ-1)    │ │ Server  │ │ C (AZ-3)    │
       │ Full trie   │ │ B (AZ-2)│ │ Full trie   │
       │ (7 GB)      │ │ Full    │ │ (7 GB)      │
       │             │ │ trie    │ │             │
       └─────────────┘ │ (7 GB)  │ └─────────────┘
                        └─────────┘

    Any server can handle any prefix.
    Load balancer distributes requests round-robin.
```

| Pros | Cons |
|---|---|
| Simplest architecture | Memory-limited per server |
| No routing logic needed | Every server stores the full trie |
| Any server handles any request | Can't scale beyond single-server memory |
| Easy to add/remove servers | Trie deploy must update all servers |

**When to use**: Trie fits in memory (our current case — 7 GB). This is the right default.

### Strategy 2: Shard by First Character

```
                    ┌──────────────────┐
                    │   API Gateway    │
                    │ Route by prefix  │
                    │   first char     │
                    └──────┬───────────┘
                           │
         ┌─────────────────┼─────────────────┐
         │                 │                 │
    prefix[0]='a'     prefix[0]='b'    prefix[0]='c'    ...
         │                 │                 │
  ┌──────▼──────┐   ┌─────▼─────┐   ┌──────▼──────┐
  │ Shard A     │   │ Shard B   │   │ Shard C     │
  │ All queries │   │           │   │             │
  │ starting    │   │           │   │             │
  │ with 'a'    │   │           │   │             │
  └─────────────┘   └───────────┘   └─────────────┘

    Total shards: 36 (a-z + 0-9)
    Each replicated 3x for availability.
```

| Pros | Cons |
|---|---|
| Simple routing logic | **Highly uneven**: 's' gets 10x traffic of 'x' |
| Easy to understand | Hot shards need more replicas |
| Natural partitioning | 36 shards might not be enough for very large tries |

**When to use**: When simplicity is paramount and you can tolerate uneven load (over-provision hot shards).

### Strategy 3: Shard by First 2 Characters

```
    Prefix → Shard mapping:
    "aa" → Shard 0
    "ab" → Shard 1
    ...
    "az" → Shard 25
    "ba" → Shard 26
    ...
    "zz" → Shard 675

    Total: 676 shards (26 × 26)
    Each replicated 3x.
```

| Pros | Cons |
|---|---|
| More even distribution than first-char | Still some imbalance |
| 676 shards provides fine granularity | Routing for single-char prefixes (e.g., just "a") requires querying 26 shards |
| Each shard is small (~10-100 MB) | More shards to manage |

**Handling single-character prefixes**: When the user types just "a", we need results from shards "aa", "ab", ..., "az" (26 shards). Fan out in parallel, merge results, return top-K. This adds ~5ms latency but only affects the first keystroke.

### Strategy 4: Consistent Hashing on Prefix

```
    Hash ring with virtual nodes:

    hash("aa") → position 0.15 → Shard 2
    hash("ab") → position 0.72 → Shard 5
    hash("ac") → position 0.31 → Shard 3
    ...

    Each shard owns a range of the hash ring.
    Adding/removing a shard redistributes only neighboring ranges.
```

| Pros | Cons |
|---|---|
| Most even distribution | Complex routing (need hash ring lookup) |
| Smooth shard rebalancing | Adjacent prefixes may be on different shards |
| Well-understood (DynamoDB, Cassandra) | Single-char prefix lookup requires querying ALL shards |

**When to use**: Very large tries (100+ GB) where even distribution is critical.

### Recommendation

| Trie Size | Strategy | Servers (with 3x replication) |
|---|---|---|
| < 20 GB | Full replication | 6-12 servers |
| 20-100 GB | Shard by first 2 characters | 60-200 servers |
| 100+ GB | Consistent hashing on prefix | 200+ servers |

**Our current choice**: Full replication (7 GB trie). Simplest, most reliable.

---

## 8. Replication for Availability

### Replication Architecture

Regardless of sharding strategy, each trie shard (or the full trie in our case) is replicated across multiple Availability Zones:

```
                    ┌──────────────┐
                    │ Load Balancer│
                    │ (NLB/ALB)   │
                    │              │
                    │ Health check:│
                    │ GET /health  │
                    │ every 5s     │
                    └──────┬───────┘
                           │
         ┌─────────────────┼─────────────────┐
         │                 │                 │
    ┌────▼─────┐     ┌────▼─────┐     ┌────▼─────┐
    │ AZ-1     │     │ AZ-2     │     │ AZ-3     │
    │          │     │          │     │          │
    │ Trie     │     │ Trie     │     │ Trie     │
    │ Server   │     │ Server   │     │ Server   │
    │ (v42)    │     │ (v42)    │     │ (v42)    │
    │          │     │          │     │          │
    │ 7 GB     │     │ 7 GB     │     │ 7 GB     │
    └──────────┘     └──────────┘     └──────────┘
```

### Blue-Green Deployment

During trie deployment, we maintain two pools of servers:

```
BEFORE DEPLOY:
    BLUE pool (active):  3 servers running trie v41 ◄── traffic
    GREEN pool (standby): 3 servers idle

DURING DEPLOY:
    BLUE pool: still serving v41 ◄── traffic
    GREEN pool: loading v42 (download + mmap + pre-touch) ~40s

AFTER DEPLOY (health check passed):
    BLUE pool: draining connections (v41)
    GREEN pool: serving v42 ◄── traffic switched

NEXT DEPLOY:
    GREEN pool (active): serving v42 ◄── traffic
    BLUE pool (standby): loads v43
```

**Zero-downtime guarantee**: Traffic switches only after health checks pass on the new pool. If v42 fails to load (OOM, corrupt file, etc.), BLUE stays active with v41.

### Health Check

```
GET /health

200 OK:
{
    "status": "healthy",
    "trie_version": "v42",
    "trie_loaded": true,
    "pages_pre_faulted": true,
    "uptime_seconds": 3600,
    "last_query_latency_p99_ms": 0.5,
    "queries_per_second": 15000
}

503 Service Unavailable:
{
    "status": "unhealthy",
    "reason": "trie_loading"
}
```

---

## 9. Geographic Distribution (Multi-Region)

For global latency and resilience, the autocomplete system is deployed across multiple AWS regions.

### Multi-Region Architecture

```
                         ┌──────────────────┐
                         │  Route53 (DNS)   │
                         │  Latency-based   │
                         │  routing         │
                         └──────┬───────────┘
                                │
           ┌────────────────────┼────────────────────┐
           │                    │                    │
    ┌──────▼──────┐     ┌──────▼──────┐     ┌──────▼──────┐
    │ us-east-1   │     │ eu-west-1   │     │ ap-northeast│
    │             │     │             │     │ -1          │
    │ CDN edge PoPs│    │ CDN edge PoPs│    │ CDN edge PoPs│
    │ Redis cluster│    │ Redis cluster│    │ Redis cluster│
    │ Trie servers │    │ Trie servers │    │ Trie servers │
    │             │     │             │     │             │
    │ Trie: global│     │ Trie: global│     │ Trie: global│
    │ + us trending│    │ + eu trending│    │ + ap trending│
    └─────────────┘     └─────────────┘     └─────────────┘

                    ┌──────────────────┐
                    │  S3 (us-east-1)  │
                    │  Trie artifacts  │
                    │  (cross-region   │
                    │   replication)   │
                    └──────────────────┘
```

### Regional vs Global Data

| Data | Scope | Strategy |
|---|---|---|
| Base trie (batch-built) | Global | Same trie deployed to all regions. Built from global query data. |
| Trending overlay | Regional | Each region runs its own Flink pipeline on regional Kafka. Regional trends may differ. |
| Redis cache | Regional | Each region has its own Redis cluster. No cross-region cache sharing. |
| User profiles (personalization) | Regional | User data stored in the region closest to them. |

### Why Global Trie + Regional Trending?

- **Global trie**: Query patterns are largely similar worldwide ("iphone", "amazon prime" are popular everywhere). One trie reduces build complexity.
- **Regional trending**: Breaking news, local events, and cultural moments are region-specific. Japanese users searching for local events shouldn't affect US suggestions.

### Cross-Region Failover

If an entire region goes down, Route53 routes traffic to the next-closest region:

```
NORMAL:
    US users → us-east-1 (30ms)
    EU users → eu-west-1 (20ms)
    AP users → ap-northeast-1 (15ms)

us-east-1 FAILS:
    US users → us-west-2 (50ms) or eu-west-1 (80ms)
    EU users → eu-west-1 (20ms)
    AP users → ap-northeast-1 (15ms)
```

Latency increases but service remains available. The CDN absorbs most of the impact (60% hit rate means only 40% of requests are affected by region routing).

---

## 10. Capacity Planning

### Per-Server Capacity

| Resource | Spec | Usage | Headroom |
|---|---|---|---|
| Instance type | c6g.2xlarge (8 vCPU, 16 GB RAM) | | |
| Memory | 16 GB | 7 GB (trie) + 2 GB (OS/app) = 9 GB | 44% free |
| CPU | 8 vCPU | At 50K req/sec: ~60% utilization | 40% free |
| Network | Up to 10 Gbps | 50K req/sec × 1 KB = 50 MB/sec | 99% free |

### Fleet Sizing

```
TRIE SERVER FLEET:

Peak traffic reaching trie servers: 95K req/sec (after caching)
Per-server capacity: 50K req/sec (at 60% CPU utilization target)

Minimum servers: 95K / 50K = 2 servers

With 3x replication (across 3 AZs): 6 servers
With N+1 redundancy per AZ: 9 servers

Per region: 9 servers
Across 3 regions: 27 servers

REDIS FLEET:

6 nodes per region (3 primary + 3 replica) × 3 regions = 18 nodes
Instance type: r6g.xlarge (4 vCPU, 32 GB RAM)
```

### Cost Estimate (Monthly)

| Component | Instances | Type | Monthly Cost |
|---|---|---|---|
| Trie servers | 27 | c6g.2xlarge | ~$5,400 |
| Redis | 18 | r6g.xlarge | ~$5,400 |
| CloudFront | N/A | Data transfer | ~$10,000 |
| S3 (trie artifacts + logs) | N/A | Storage + requests | ~$2,000 |
| Spark (EMR, hourly) | On-demand cluster | m5.2xlarge × 100 | ~$3,000 |
| Flink (managed) | 3 nodes per region | m5.xlarge | ~$2,000 |
| **Total** | | | **~$27,800/month** |

This is remarkably cost-effective for a system handling **1.7M req/sec** and serving billions of suggestion requests per day.

---

## 11. Load Testing and Performance Benchmarks

### Test Scenarios

| Scenario | Traffic Pattern | Duration | Goal |
|---|---|---|---|
| **Steady state** | 580K req/sec (normal) | 1 hour | Verify p99 < 100ms |
| **Peak load** | 1.7M req/sec (3x normal) | 30 min | Verify system handles peak without degradation |
| **Prime Day spike** | 5M req/sec (10x normal) | 15 min | Identify breaking point, validate auto-scaling |
| **Cold cache** | 580K req/sec, all caches empty | 10 min | Verify trie servers handle full load during cache warm-up |
| **Trie deployment** | 580K req/sec, deploy new trie mid-test | 5 min | Verify zero-downtime deployment |
| **Regional failover** | Kill one region, observe recovery | 10 min | Verify Route53 failover, increased latency in other regions |
| **Single server failure** | Kill one trie server | 5 min | Verify load balancer routes around failed server |

### Expected Results

| Metric | Steady State | Peak (3x) | Target |
|---|---|---|---|
| p50 latency | ~5ms | ~10ms | < 50ms |
| p95 latency | ~15ms | ~30ms | < 80ms |
| p99 latency | ~30ms | ~60ms | < 100ms |
| p99.9 latency | ~50ms | ~80ms | < 200ms |
| Error rate | 0% | < 0.01% | < 0.1% |
| CDN hit rate | 60% | 55% | > 50% |
| Redis hit rate | 80% | 75% | > 70% |
| Trie server CPU | 30% | 60% | < 80% |

### Stress Test: Finding the Breaking Point

Gradually increase load until p99 exceeds 100ms or error rate exceeds 0.1%:

```
EXPECTED BREAKING POINTS:

With caching:
    ~8M req/sec total (~450K hitting trie servers)
    Breaking point: trie server CPU saturation

Without caching (cold cache):
    ~200K req/sec (all hitting trie servers)
    Breaking point: trie server CPU + Redis write throughput

Mitigation at breaking point:
    1. Auto-scale trie servers (horizontal)
    2. Increase Redis cache TTL (reduce backend load)
    3. Increase CDN TTL (shift more traffic to edge)
    4. Enable request coalescing (reduce duplicate backend calls)
```

### Performance Regression Testing

After each trie server code change, run a benchmark suite:

```
BENCHMARK SUITE (runs in CI/CD):

1. Single-query latency:
   - 10,000 random prefix lookups on a reference trie
   - Assert: p99 < 1ms

2. Throughput:
   - Sustain 50K req/sec for 60 seconds
   - Assert: no errors, p99 < 1ms

3. Memory:
   - Load reference trie, assert RSS < 10 GB
   - Run 1M queries, assert no memory growth (no leak)

4. Startup time:
   - Load trie from disk, pre-touch, ready to serve
   - Assert: < 60 seconds
```

---

*This deep dive complements the [Interview Simulation](interview-simulation.md) and is referenced by the [System Flows](flow.md) document.*
