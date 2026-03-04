# TinyURL System — Scaling & Caching Deep Dive

> This document covers how to scale the TinyURL system from a single-server deployment to a globally distributed, multi-region architecture serving 15,000 redirects/sec at peak with sub-5ms average latency. The key insight: URL mappings are **immutable write-once data** — once created, a short URL's target never changes (or changes extremely rarely). This makes every cache layer maximally effective because cache invalidation is trivial.

---

## Table of Contents

1. [Iterative Scaling Progression](#1-iterative-scaling-progression)
2. [Browser / Client-Side Caching](#2-browser--client-side-caching)
3. [CDN Strategy](#3-cdn-strategy)
4. [Redis Application Cache](#4-redis-application-cache)
5. [Hot Key Analysis](#5-hot-key-analysis)
6. [Sharding Strategies Deep Dive](#6-sharding-strategies-deep-dive)
7. [Multi-Region Deployment Topology](#7-multi-region-deployment-topology)
8. [Capacity Planning](#8-capacity-planning)
9. [Load Testing and Performance Benchmarks](#9-load-testing-and-performance-benchmarks)
10. [Failure Modes and Resilience](#10-failure-modes-and-resilience)

---

## System Context (Quick Reference)

| Metric | Value |
|---|---|
| URL creation (writes) | 230/sec avg, 1,000/sec peak |
| URL redirect (reads) | 3,800/sec avg, 15,000/sec peak |
| Read:Write ratio | **17:1** |
| Total URLs (100-year horizon) | ~720 billion |
| Total storage (100 years) | ~48 TB |
| Average long URL length | ~50 bytes |
| Short URL suffix length | 8 characters (base62) |
| Redirect type | 302 by default (configurable to 301) |
| Primary database | PostgreSQL + Citus for sharding |
| Cache layer | Redis Cluster |
| Multi-region | US-EAST (leader), EU-WEST, AP-EAST (followers) |

---

## 1. Iterative Scaling Progression

This is the headline story of the document. We start with a naive "every request hits the database" architecture and progressively add cache layers. Each iteration shows **quantitative server reduction** and **latency improvement** — demonstrating that for immutable data like URL mappings, caching is extraordinarily effective.

---

### Iteration 1: No Cache — Direct to Database

The simplest possible architecture. Every redirect request goes through the app server directly to PostgreSQL.

```
┌─────────────────┐           ┌──────────────────┐           ┌──────────────────────────┐
│                 │           │                  │           │                          │
│   15,000        │           │   App Servers    │           │   PostgreSQL             │
│   req/sec       │──────────▶│   (stateless)    │──────────▶│   (Leader +              │
│   (peak)        │           │                  │           │    Read Replicas)        │
│                 │◀──────────│                  │◀──────────│                          │
│                 │           │                  │           │                          │
└─────────────────┘           └──────────────────┘           └──────────────────────────┘
```

#### Math: How Many Servers?

**Database nodes:**
- Each PostgreSQL read replica handles ~5,000 simple `SELECT` queries/sec (indexed lookup by primary key)
- Peak redirect reads: 15,000/sec
- Read replicas needed: 15,000 / 5,000 = **3 read replicas**
- Plus 1 leader node for writes (1,000 peak writes/sec is comfortable for a single node)
- With 3x replication factor for high availability across AZs: (1 leader + 3 replicas) x 3 = **12 DB nodes**

**App servers:**
- Each app server handles ~2,000 req/sec (stateless, just routing and HTTP handling)
- Peak total: 15,000 reads + 1,000 writes = 16,000 req/sec
- App servers needed: 16,000 / 2,000 = **8 app servers**

**Total infrastructure:**
```
┌──────────────────────────────────────────────┐
│  Iteration 1: No Cache                       │
│                                              │
│  DB nodes:  12  (1 leader + 3 replicas x 3)  │
│  App nodes:  8                               │
│  ─────────────────────────────               │
│  TOTAL:     20 nodes                         │
│                                              │
│  Avg redirect latency: ~10ms                 │
│  P99 redirect latency: ~50ms                 │
│  DB reads/sec (peak):  15,000                │
└──────────────────────────────────────────────┘
```

#### Why This Is Wasteful

The core problem: **every single redirect hits the database**. At 15K/sec this technically works, but consider what is happening on each redirect:

```
SELECT long_url, expiry_time
FROM url_mappings
WHERE suffix = 'ab3k9x12';
```

This is an indexed primary key lookup returning a single row that **almost never changes**. Once a URL mapping is created, it is immutable — the same query returns the same result for its entire lifetime (potentially years). We are paying for a database round trip (~10ms including network) on every request for data that could be served from a cache in ~1ms.

The read:write ratio of 17:1 means for every time the data is written, it is read 17 times. In practice, popular URLs are read millions of times. This is the textbook use case for caching.

#### When Is Iteration 1 Acceptable?

- Small scale: < 1,000 req/sec
- Prototyping phase
- When operational simplicity outweighs performance
- When budget is extremely constrained

---

### Iteration 2: Add Redis Cache Layer

The first and most impactful improvement. We place a Redis cluster between the app servers and PostgreSQL.

```
┌─────────────┐    ┌────────────┐    ┌─────────────┐    ┌──────────────────────┐
│             │    │            │    │             │    │                      │
│  15,000     │    │  App       │    │  Redis      │    │  PostgreSQL          │
│  req/sec    │───▶│  Servers   │───▶│  Cache      │───▶│  (on cache miss)     │
│  (peak)     │    │            │    │  Hit: 80%   │    │                      │
│             │◀───│            │◀───│  TTL: 1hr   │◀───│                      │
│             │    │            │    │             │    │                      │
└─────────────┘    └────────────┘    └─────────────┘    └──────────────────────┘
```

#### Why 80% Hit Rate?

URL access follows a **Zipf/power-law distribution**. This is empirically observed in every URL shortener:

- A small number of URLs (viral tweets, popular articles) receive the vast majority of traffic
- The "long tail" of URLs are accessed rarely (personal links, one-time shares)
- Empirical data from Bitly: ~20% of URLs account for ~80% of all redirects

With a Redis cache sized to hold the top 10% of URLs by access frequency (see Section 4 for sizing details), we achieve approximately **80% cache hit rate**. This is conservative — real-world systems with Zipf distributions often see 85-95% hit rates.

```
Traffic Distribution (Zipf):

  Accesses │
  per URL  │ ██
           │ ██
           │ ██ ██
           │ ██ ██
           │ ██ ██ ██
           │ ██ ██ ██ ██
           │ ██ ██ ██ ██ ██ ██
           │ ██ ██ ██ ██ ██ ██ ██ ██ ██ ██ ██ ██ ██ ██ ██ ██ ██
           └────────────────────────────────────────────────────▶ URLs
            ◀── top 20% ──▶◀──────────── bottom 80% ───────────▶
            (~80% traffic)   (~20% traffic, mostly 1-2 accesses)
```

#### Math: Infrastructure Reduction

**Database load after Redis:**
- Peak reads: 15,000/sec
- Redis hit rate: 80%
- DB reads: 15,000 x 0.20 = **3,000 reads/sec** (5x reduction!)
- DB replicas needed: 3,000 / 5,000 = 1 replica (round up for safety)
- With HA: 1 leader + 1 replica + 1 standby = **3 DB nodes**

**Redis cluster:**
- 3 primary nodes + 3 replica nodes = **6 Redis nodes** (standard Redis Cluster minimum for HA)
- Each primary handles ~100K+ reads/sec — massively over-provisioned for our 15K peak
- Memory: ~5GB per primary (details in Section 4) — fits in the smallest Redis instance

**App servers:**
- Still 8 (same total request volume; the app server still processes every request, it just checks Redis first)

**Total infrastructure:**
```
┌──────────────────────────────────────────────────┐
│  Iteration 2: + Redis Cache                      │
│                                                  │
│  DB nodes:     3  (1 leader + 1 replica + 1 HA)  │
│  Redis nodes:  6  (3 primary + 3 replica)        │
│  App nodes:    8                                 │
│  ────────────────────────────────                │
│  TOTAL:       17 nodes                           │
│                                                  │
│  Avg redirect latency: ~3ms   (was ~10ms)        │
│  P99 redirect latency: ~15ms  (was ~50ms)        │
│  DB reads/sec (peak):  3,000  (was 15,000)       │
└──────────────────────────────────────────────────┘
```

#### Latency Breakdown

```
                          Iteration 1       Iteration 2
                          (No cache)        (+ Redis)
                          ──────────        ──────────
Cache hit (80%):          N/A               ~1-2ms (Redis GET)
Cache miss (20%):         ~10ms (DB)        ~10ms (DB) + ~1ms (Redis SET)
Weighted average:         ~10ms             0.8 x 2ms + 0.2 x 11ms = ~3.8ms
```

**Key improvement**: Average redirect latency drops from ~10ms to ~3ms. The 80% of requests served from Redis see a 5x latency improvement.

---

### Iteration 3: Add CDN Layer

The next cache layer sits at the network edge — a Content Delivery Network. This is where URL shorteners face a unique challenge.

#### The 302 Challenge

```
┌─────────────────────────────────────────────────────────────────────┐
│                                                                     │
│  ⚠️  CDN CHALLENGE: 302 redirects are NOT cacheable by default!    │
│                                                                     │
│  Standard behavior:                                                 │
│  HTTP/1.1 302 Found                                                 │
│  Location: https://example.com/very-long-url                        │
│  ← CDN sees 302 status code as "temporary" → does NOT cache        │
│                                                                     │
│  This means every single redirect request passes through the CDN    │
│  to your origin servers. CDN becomes a pass-through, not a cache.   │
│                                                                     │
│  Solutions:                                                         │
│  1. Use 301 for permanent URLs → CDN caches the redirect itself     │
│  2. Add Cache-Control: public, max-age=300 to 302 responses         │
│     → CDN caches for 5 min even though it's a 302                   │
│  3. CDN edge worker resolves the mapping at the edge                │
│     → No round-trip to origin at all                                │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

We choose **Solution 2** (Cache-Control override) as the default because it balances performance with flexibility. Details in Section 3.

#### Architecture with CDN

```
┌─────────────┐    ┌────────────┐    ┌────────────┐    ┌─────────────┐    ┌────────────┐
│             │    │            │    │            │    │             │    │            │
│  15,000     │    │  CDN       │    │  App       │    │  Redis      │    │ PostgreSQL │
│  req/sec    │───▶│ CloudFront │───▶│  Servers   │───▶│  Cache      │───▶│ (on miss)  │
│  (peak)     │    │ Hit: 50%   │    │            │    │  Hit: 80%   │    │            │
│             │◀───│ TTL: 5min  │◀───│            │◀───│  TTL: 1hr   │◀───│            │
│             │    │            │    │            │    │             │    │            │
└─────────────┘    └────────────┘    └────────────┘    └─────────────┘    └────────────┘
```

#### Why Only 50% CDN Hit Rate?

CDN hit rate for a URL shortener is lower than for static assets because:

1. **Short TTL (5 min)**: We use 302 + Cache-Control: max-age=300, so entries expire quickly
2. **Long tail distribution**: Many URLs are accessed infrequently — they expire from CDN cache before the next access
3. **Geographic distribution**: Each CDN POP has its own cache. A URL popular in New York is not cached in the Tokyo POP
4. **URL space is enormous**: Billions of URLs, each is a separate cache key. CDN can't cache them all

That said, 50% is still a massive win for the hot URLs — viral links that drive most of the traffic.

#### Math: Infrastructure Reduction

**Traffic flow:**
```
15,000 req/sec (peak)
    │
    ├── CDN hit (50%): 7,500 req/sec → served from CDN edge (~5ms)
    │
    └── CDN miss (50%): 7,500 req/sec → origin
         │
         ├── Redis hit (80%): 6,000 req/sec → served from Redis (~2ms)
         │
         └── Redis miss (20%): 1,500 req/sec → PostgreSQL (~10ms)
```

**Database:**
- DB reads: 1,500/sec (was 3,000 in Iteration 2, another 2x reduction)
- 1 read replica handles this comfortably
- 1 leader + 1 replica = **2 DB nodes**

**Redis:**
- 6 nodes (same cluster, handling 6,000 reads/sec instead of 12,000 — even more headroom)

**App servers:**
- Only 7,500 req/sec reach origin (CDN absorbs the other half)
- 7,500 / 2,000 = 4 app servers (was 8)

**CDN:**
- No "nodes" to manage — CloudFront is pay-per-request
- Cost: ~$0.0075 per 10,000 requests (see Section 8)

**Total infrastructure:**
```
┌──────────────────────────────────────────────────┐
│  Iteration 3: + CDN                              │
│                                                  │
│  DB nodes:     2  (1 leader + 1 replica)         │
│  Redis nodes:  6  (3 primary + 3 replica)        │
│  App nodes:    4                                 │
│  CDN:          managed (pay-per-request)          │
│  ────────────────────────────────                │
│  TOTAL:       12 nodes + CDN                     │
│                                                  │
│  Avg redirect latency: ~4ms   (was ~3ms)*        │
│  P99 redirect latency: ~12ms  (was ~15ms)        │
│  DB reads/sec (peak):  1,500  (was 3,000)        │
│                                                  │
│  * Average includes CDN latency for cache hits   │
│    which adds ~5ms for those requests, but the   │
│    reduced DB load improves miss latency          │
└──────────────────────────────────────────────────┘
```

> Note: The average latency with CDN can appear slightly higher than Iteration 2 because CDN cache hits include network latency to the CDN POP (~5ms) whereas Redis hits are ~2ms from the app server's perspective. However, the CDN hit latency is measured from the user's perspective, and the CDN POP is geographically closer to the user than the origin — so the **end-to-end user-perceived latency** is actually lower.

---

### Iteration 4: Add Browser Cache + Multi-Region (Final Architecture)

The final iteration adds two more layers: browser caching (the ultimate "zero latency" cache) and multi-region deployment for global users.

```
User visits tinyurl.com/ab3k9x12
       │
       ▼
┌──────────────────────────────────────────────────────────────────────┐
│  LAYER 0: Browser Cache                                              │
│                                                                      │
│  301 (permanent):  Cached indefinitely → instant redirect (0ms)      │
│  302 + max-age=300: Cached 5 minutes → instant redirect (0ms)        │
│                                                                      │
│  Impact: ~30% of requests are repeat visits from same browser        │
│  After browser: 15,000 x 0.70 = 10,500 req/sec leave browser        │
└──────┬───────────────────────────────────────────────────────────────┘
       │ MISS (70% of total = 10,500 req/sec)
       ▼
┌──────────────────────────────────────────────────────────────────────┐
│  LAYER 1: CDN (CloudFront, regional POPs)                            │
│                                                                      │
│  Cache-Control: public, max-age=300 (5 min TTL)                      │
│  Hit rate: ~50% of remaining traffic                                 │
│                                                                      │
│  After CDN: 10,500 x 0.50 = 5,250 req/sec reach origin              │
│  Latency for hits: ~5ms (served from nearest CDN POP)                │
└──────┬───────────────────────────────────────────────────────────────┘
       │ MISS (35% of total = 5,250 req/sec)
       ▼
┌──────────────────────────────────────────────────────────────────────┐
│  LAYER 2: Redis Cache (regional cluster)                             │
│                                                                      │
│  TTL: 1 hour (or until URL expiry, whichever is sooner)              │
│  Hit rate: ~80% of remaining traffic                                 │
│                                                                      │
│  After Redis: 5,250 x 0.20 = 1,050 req/sec hit database             │
│  Latency for hits: ~2ms (single Redis GET)                           │
└──────┬───────────────────────────────────────────────────────────────┘
       │ MISS (7% of total = 1,050 req/sec)
       ▼
┌──────────────────────────────────────────────────────────────────────┐
│  LAYER 3: PostgreSQL (regional read replica)                         │
│                                                                      │
│  Always resolves (source of truth)                                   │
│  Populates Redis on read (cache-aside pattern)                       │
│                                                                      │
│  DB reads: 1,050/sec total, ~350/sec per region (3 regions)          │
│  Latency: ~10ms (indexed primary key lookup)                         │
└──────────────────────────────────────────────────────────────────────┘
```

#### Multi-Region Split

With 3 regions (US-EAST, EU-WEST, AP-EAST) and roughly equal traffic distribution:

```
Total peak: 15,000 req/sec
After browser cache: 10,500 req/sec
Per region: ~3,500 req/sec

Per-region breakdown:
  3,500 req/sec enter CDN
    → 1,750 CDN hits (50%)
    → 1,750 CDN misses → Redis
       → 1,400 Redis hits (80%)
       → 350 Redis misses → DB

Each region sees only ~350 DB reads/sec at peak.
```

#### Per-Region Infrastructure

```
┌───────────────────────────────────────────┐
│  Per Region (e.g., US-EAST)               │
│                                           │
│  CDN:          CloudFront (managed)       │
│  App servers:  2  (1,750 req/sec ÷ 2K)   │
│  Redis:        6  (3 primary + 3 replica) │
│  DB:           2  (1 leader* + 1 replica) │
│  ──────────────────────────               │
│  TOTAL:        10 nodes + CDN per region  │
│                                           │
│  * Leader only in US-EAST; EU/AP have     │
│    read replicas that follow US-EAST      │
└───────────────────────────────────────────┘

Simplified per-region for EU-WEST and AP-EAST (follower regions):
  App servers:  2
  Redis:        6
  DB replicas:  2  (follow US-EAST leader)
  TOTAL:        10 nodes + CDN
```

---

### Evolution Summary Table

| Iteration | Cache Layers | DB Read Load (peak) | Infra Nodes | Avg Redirect Latency | Key Change |
|---|---|---|---|---|---|
| **1. No cache** | App -> DB | 15,000/sec | ~20 | ~10ms | Baseline |
| **2. + Redis** | App -> Redis -> DB | 3,000/sec (5x reduction) | ~17 | ~3ms | 80% served from memory |
| **3. + CDN** | CDN -> App -> Redis -> DB | 1,500/sec (10x reduction) | ~12 + CDN | ~4ms (user-perceived ~3ms) | 50% never reach origin |
| **4. + Browser + Multi-region** | Browser -> CDN -> Redis -> DB | ~350/sec per region (43x reduction) | ~10 per region + CDN | ~2ms (avg) | 93% resolved before DB |

#### What Makes URL Shortening Caching So Effective?

```
┌──────────────────────────────────────────────────────────────────────┐
│                                                                      │
│  Why caching works so well for TinyURL (compared to other systems):  │
│                                                                      │
│  1. IMMUTABLE DATA: URL mappings are write-once. Once created,       │
│     suffix -> longUrl NEVER changes. Cache invalidation is trivial   │
│     (no invalidation needed — just TTL-based expiry).                │
│                                                                      │
│  2. SMALL ENTRIES: Each cache entry is ~66 bytes. You can cache      │
│     72 million URLs in ~4.75 GB of RAM.                              │
│                                                                      │
│  3. ZIPF DISTRIBUTION: A tiny fraction of URLs get most traffic.     │
│     Caching just the top 10% absorbs 80% of reads.                   │
│                                                                      │
│  4. SIMPLE ACCESS PATTERN: Every read is a point lookup by key.      │
│     No range queries, no joins, no complex cache key computation.    │
│                                                                      │
│  Compare to autocomplete (where the trie changes hourly) or          │
│  e-commerce (where prices/inventory change constantly) — TinyURL     │
│  is the ideal caching use case.                                      │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

> **Bottom line**: From 20 nodes handling 15K/sec DB reads to ~10 nodes per region handling 350/sec DB reads — a 43x reduction in database load. Average latency from 10ms to 2ms. The key insight: URL mappings are immutable write-once data, so every cache layer is maximally effective because invalidation is trivial.

---

## 2. Browser / Client-Side Caching

The browser is the closest cache to the user — a cache hit here means **zero network traffic** and **zero server load**. For a URL shortener, the browser caching strategy depends on the HTTP redirect status code.

### 301 vs 302: The Fundamental Choice

| Aspect | 301 Permanent Redirect | 302 Temporary Redirect |
|---|---|---|
| **Semantics** | "This URL has permanently moved" | "This URL is temporarily at another location" |
| **Browser caches?** | Yes, indefinitely (until cache cleared) | No (unless `Cache-Control` header set) |
| **CDN caches?** | Yes (by default in most CDNs) | No (unless `Cache-Control` header set) |
| **Can change target?** | No — browser won't re-check the server | Yes — browser re-checks on every visit |
| **Analytics possible?** | No (repeat visits from same browser never hit server) | Yes (every visit hits server, can count/log) |
| **Can delete/disable URL?** | No (browser ignores server — cached forever) | Yes (server can return 410 Gone) |
| **Best for** | Links that will never change, maximum performance | Links that might change, need tracking/analytics |
| **Used by** | Google (goo.gl used 301) | Bitly (uses 301), TinyURL (uses 302 by default) |

### Why 302 Is Our Default

```
Scenario: Company shares tinyurl.com/annual-report

Year 1: Points to /reports/2025-annual-report.pdf  ← users cache this in browser
Year 2: Company wants to point to /reports/2026-annual-report.pdf

With 301: Users who visited last year have the OLD URL cached in their browser.
          They will NEVER see the new target (unless they clear browser cache).
          The company has no way to fix this.

With 302: Users re-check the server on every visit (or after short cache TTL).
          Company can update the target anytime.
          Server can track visit counts for analytics.
```

### How We Handle Both: Configurable Redirect Strategy

**Default (302 + short browser cache):**
```
HTTP/1.1 302 Found
Location: https://example.com/original-long-url
Cache-Control: private, max-age=300
```
- `private`: only the browser caches (not CDN, not proxies)
- `max-age=300`: browser caches for 5 minutes
- Effect: repeat visits within 5 minutes are instant; after 5 minutes, browser re-checks server
- Server still gets analytics data every 5 minutes per user

**Permanent option (301 + long CDN cache):**
```
HTTP/1.1 301 Moved Permanently
Location: https://example.com/original-long-url
Cache-Control: public, max-age=86400
```
- `public`: CDN and proxies can cache this too
- `max-age=86400`: cache for 24 hours
- Effect: maximum performance, but URL target cannot be changed
- User must opt in at URL creation time (`permanent: true`)

### The `private` vs `public` Distinction

```
                                private                        public
                                ────────                       ──────
Browser cache?                  Yes                            Yes
Shared proxy (corporate)?       No                             Yes
CDN edge cache?                 No                             Yes
ISP transparent proxy?          No                             Yes

Use private when:               You want per-user caching
                                and need analytics/control

Use public when:                Maximum performance matters
                                and you accept loss of
                                per-visit analytics
```

### Impact on Server Load

Estimating browser cache hit rate is tricky — it depends on user behavior:

```
Factors affecting browser cache hit rate:
─────────────────────────────────────────
1. How often the SAME user visits the SAME short URL
   - Bookmarked links: very high repeat rate
   - Social media links: low repeat rate (one-time clicks)

2. Cache TTL
   - 301 (permanent): cached indefinitely → very high hit rate for repeat visits
   - 302 + 5 min TTL: only hits if user revisits within 5 minutes

3. User's browser behavior
   - Incognito/private mode: no cache
   - User clears cache: cache evicted
   - Multiple devices: each device has separate cache

Conservative estimate: ~30% of peak traffic is absorbed by browser cache
(mostly from users with bookmarked short URLs and rapid reshares on social media)
```

### Vary Header Considerations

```
HTTP/1.1 302 Found
Location: https://example.com/original-long-url
Cache-Control: private, max-age=300
Vary: Accept-Language
```

Why `Vary: Accept-Language`? If we ever add language-specific redirects (e.g., redirect French users to the French version of a page), the `Vary` header ensures the browser maintains separate cache entries per language. For now, this is future-proofing — the redirect target is the same regardless of language.

---

## 3. CDN Strategy

### The 302 Challenge — In Detail

Standard CDN behavior is governed by HTTP caching rules:

```
Status Code    CDN Default Behavior
───────────    ───────────────────────────────────────
200 OK         Cacheable (if Cache-Control allows)
301 Moved      Cacheable (most CDNs cache by default)
302 Found      NOT cacheable (considered temporary)
404 Not Found  NOT cacheable (some CDNs cache briefly)
```

Since we use 302 by default, the CDN will **pass every request through to origin** unless we explicitly override with `Cache-Control` headers.

### Three CDN Solutions

---

#### Solution 1: Use 301 for Permanent URLs

**How it works:**
- User creates URL with `permanent: true` flag in the API
- Server returns `301 Moved Permanently` instead of `302 Found`
- CDN automatically caches the 301 response (no special configuration needed)
- Cache key: the short URL path (e.g., `/ab3k9x12`)

**Response headers:**
```
HTTP/1.1 301 Moved Permanently
Location: https://example.com/long-url
Cache-Control: public, max-age=86400
X-TinyURL-Permanent: true
```

**Tradeoffs:**

| Pro | Con |
|---|---|
| Maximum CDN cache hit rate | Cannot update or delete URL from server side |
| No special CDN configuration needed | Browser caches permanently (even after server-side expiry) |
| Lowest latency (CDN serves directly) | No analytics for repeat visits from same CDN POP |
| Standard HTTP semantics | If target site goes down, short URL still redirects to broken page |

**When to use:** Links to stable content that will never change (e.g., documentation, Wikipedia articles, permanent resources).

---

#### Solution 2: Cache-Control Override on 302 (Recommended)

**How it works:**
- Server returns `302 Found` with explicit `Cache-Control` header
- `s-maxage` directive tells CDN to cache, while `max-age` controls browser cache
- CDN caches the 302 response for the specified duration

**Response headers:**
```
HTTP/1.1 302 Found
Location: https://example.com/long-url
Cache-Control: public, s-maxage=300, max-age=300
```

**Header breakdown:**
```
Cache-Control: public, s-maxage=300, max-age=300
               ──────  ────────────  ───────────
               │       │             │
               │       │             └── Browser: cache for 5 minutes
               │       └──────────────── CDN/Proxy: cache for 5 minutes
               └──────────────────────── Both CDN and browser may cache
```

**Why 5 minutes (300 seconds)?**
- Long enough to absorb bursts (viral URL shared on Twitter = thousands of hits/minute)
- Short enough that URL updates/deletions propagate within 5 minutes
- Empirical: most URL "bursts" have hits clustered within seconds of each other

**Tradeoffs:**

| Pro | Con |
|---|---|
| URL can be updated/deleted (5 min staleness max) | 5-minute window where deleted URLs still resolve |
| Analytics still work (every 5 min per CDN POP) | Lower hit rate than 301 (entries expire frequently) |
| Works with all major CDNs | Requires understanding of Cache-Control semantics |
| Good balance of performance and flexibility | Long-tail URLs may not benefit (evicted before revisit) |

---

#### Solution 3: CDN Edge Workers (Lambda@Edge / Cloudflare Workers)

**How it works:**
- Deploy a lightweight function at every CDN edge location
- Edge worker maintains a local cache (e.g., Cloudflare KV, DynamoDB Global Tables)
- On request: edge worker looks up the suffix in its local KV store, issues redirect directly
- Cache miss: edge worker calls origin, populates KV for future requests

```
┌──────────┐    ┌──────────────────────────────────────────┐    ┌──────────┐
│          │    │  CDN Edge POP (e.g., Tokyo)               │    │          │
│  User    │───▶│                                          │───▶│  Origin  │
│  (Tokyo) │    │  Edge Worker:                            │    │  (US)    │
│          │◀───│    1. Check KV: "ab3k9x12" → longUrl    │◀───│          │
│          │    │    2. If found: return 302 directly       │    │          │
│          │    │    3. If not: fetch from origin           │    │          │
│          │    │       → populate KV → return 302          │    │          │
│          │    │                                          │    │          │
│          │    │  Latency: ~1-2ms (KV is at the edge)     │    │          │
│          │    └──────────────────────────────────────────┘    └──────────┘
```

**Tradeoffs:**

| Pro | Con |
|---|---|
| Lowest possible latency (resolve at edge) | Most complex to implement |
| Full control over caching logic | Edge storage costs (Cloudflare KV: $0.50/M reads) |
| Can implement custom analytics at edge | Cold start latency for Lambda@Edge (~5-50ms) |
| Can handle URL expiry logic at edge | Debugging distributed edge functions is hard |

---

#### Recommendation

```
┌───────────────────────────────────────────────────────────────────┐
│                                                                   │
│  Start with Solution 2 (Cache-Control on 302):                    │
│    - Simple to implement (just add headers to responses)          │
│    - Works with any CDN (CloudFront, Cloudflare, Fastly)          │
│    - Good performance/flexibility tradeoff                        │
│                                                                   │
│  Graduate to Solution 3 (Edge Workers) when:                      │
│    - Latency is the top priority                                  │
│    - Team has edge computing expertise                            │
│    - Budget allows for edge storage costs                         │
│    - Scale exceeds 100K+ req/sec (CDN cache hit rate matters more)│
│                                                                   │
│  Offer Solution 1 (301) as a per-URL opt-in:                      │
│    - User flag: permanent=true at creation time                   │
│    - Best for stable, permanent links                             │
│    - Document the tradeoff clearly in API docs                    │
│                                                                   │
└───────────────────────────────────────────────────────────────────┘
```

### CloudFront Configuration

```yaml
# CloudFront Distribution Configuration
Distribution:
  Origins:
    - Id: tinyurl-origin
      DomainName: origin.tinyurl.example.com
      CustomOriginConfig:
        HTTPPort: 80
        HTTPSPort: 443
        OriginProtocolPolicy: https-only
        OriginReadTimeout: 10      # seconds — short, since our origin is fast

  DefaultCacheBehavior:
    TargetOriginId: tinyurl-origin
    ViewerProtocolPolicy: redirect-to-https

    # Cache Policy
    CachePolicyId: custom-tinyurl-cache-policy
    # Custom policy settings:
    #   MinimumTTL: 0
    #   MaximumTTL: 300           # 5 minutes max
    #   DefaultTTL: 0             # Respect origin Cache-Control
    #   CacheKeyParameters:
    #     HeadersConfig: none     # Don't vary by headers
    #     CookiesConfig: none     # Don't vary by cookies
    #     QueryStringsConfig: none # Don't vary by query strings
    #   Cache key = path only (e.g., /ab3k9x12)

    # Origin Request Policy
    OriginRequestPolicyId: custom-tinyurl-origin-policy
    # Forward to origin:
    #   Headers: Host only
    #   Cookies: none
    #   Query strings: none

    AllowedMethods: [GET, HEAD]
    Compress: false               # Redirects have no body to compress

  # Custom error pages (optional)
  CustomErrorResponses:
    - ErrorCode: 404
      ErrorCachingMinTTL: 60      # Cache 404s for 1 minute to prevent hammering
    - ErrorCode: 410
      ErrorCachingMinTTL: 300     # Cache 410 (Gone) for 5 minutes
```

### CDN Cache Key Strategy

```
Cache Key Design:
──────────────────
Key:   Path only (e.g., /ab3k9x12)
NOT:   Path + query string + headers + cookies

Why path only?
  - Short URL has no query parameters that affect the redirect target
  - No user-specific content (the redirect is the same for all users)
  - No Accept-Language variation (redirect target doesn't depend on language)
  - Minimal cache key = maximum cache efficiency (no fragmentation)

Example:
  tinyurl.com/ab3k9x12?utm_source=twitter
  tinyurl.com/ab3k9x12?utm_source=facebook
  tinyurl.com/ab3k9x12

  All three have the SAME cache key: /ab3k9x12
  → Only one origin request needed, all three served from cache

  Note: We strip query params at the CDN level. The original query
  params are NOT forwarded to origin (they're meaningless for redirect).
  If the user wants UTM params, they go in the long URL target.
```

---

## 4. Redis Application Cache

Redis is the workhorse cache layer — it sits between the app servers and PostgreSQL, absorbing 80% of database reads. This section covers the detailed design of the Redis caching layer.

### 4.1 Cache Sizing (Zipf Distribution Analysis)

URL access follows a Zipf/power-law distribution. This means a small fraction of URLs accounts for a disproportionately large share of traffic. The question is: **how many URLs do we need to cache to achieve a given hit rate?**

#### Cache Entry Size Calculation

```
Single cache entry:
  Key:    "url:" prefix (4 bytes) + suffix (8 bytes) = 12 bytes
  Value:  longUrl (50 bytes avg) + ":" separator (1 byte) + expiryTimestamp (10 bytes) = 61 bytes
  Redis overhead per key: ~50 bytes (hash table entry, SDS strings, etc.)
  ─────────────────────────────────────────────────────────────────────
  Total per entry: ~123 bytes → round to ~130 bytes for safety

  Simplified estimate: ~66 bytes of pure data + ~64 bytes Redis overhead = ~130 bytes
```

#### Zipf Distribution: Cache Size vs Hit Rate

Using the Zipf distribution model with parameter s=1.0 (typical for web access patterns):

| Cache Coverage | URLs Cached | Memory Required | Traffic Absorbed | Comment |
|---|---|---|---|---|
| Top 0.01% | 72K | ~9 MB | ~15% | Barely useful |
| Top 0.1% | 720K | ~94 MB | ~30% | Viral URLs only |
| Top 1% | 7.2M | ~936 MB (~1 GB) | ~50% | Good start |
| Top 5% | 36M | ~4.7 GB | ~70% | Strong coverage |
| **Top 10%** | **72M** | **~9.4 GB** | **~80%** | **Recommended** |
| Top 20% | 144M | ~18.7 GB | ~90% | Diminishing returns |
| Top 50% | 360M | ~46.8 GB | ~97% | Expensive, marginal gain |

```
Hit Rate vs Cache Size (Zipf s=1.0):

100% │                                          ___________________
     │                                    _____/
 90% │                               ____/
     │                          ____/
 80% │ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─/─ ─ ─ ─ ─ ─ ─ ─ ─ ← SWEET SPOT
     │                     __/                        (10%, 80% hit)
 70% │                   _/
     │                 _/
 60% │               _/
     │             _/
 50% │           _/
     │         _/
 40% │       _/
     │      /
 30% │    _/
     │   /
 20% │  /
     │ /
 10% │/
     │
  0% └───────────────────────────────────────────▶
     0%   5%   10%  15%  20%  25%  30%  40%  50%
                    Cache Size (% of total URLs)
```

#### Recommendation

Cache the **top 10% of URLs by access frequency** (~72M entries, ~9.4 GB). This provides:
- **80% cache hit rate** — the "knee" of the diminishing returns curve
- Fits in a single `r6g.xlarge` (32GB RAM) with room to spare
- Doubling cache size to 20% only gains 10% more hit rate — not worth the cost

### 4.2 Redis Cluster Design

| Parameter | Value | Rationale |
|---|---|---|
| Topology | 3 primary + 3 replica | Standard Redis Cluster HA configuration |
| Instance type | r6g.xlarge (4 vCPU, 32GB RAM) | Memory-optimized, ARM-based (cost efficient) |
| Total memory | 96 GB (3 x 32 GB primary) | ~10 GB used, 86 GB headroom |
| Hash slots | 16,384 (Redis Cluster default) | Distributed across 3 primaries |
| Availability zones | 3 AZs | One primary + one replica per AZ |
| Max read throughput | ~300K reads/sec total | Far exceeds our 15K peak |
| Max write throughput | ~100K writes/sec total | Far exceeds our 1K peak |
| Network | Enhanced networking, 10 Gbps | Redis is network-bound, not CPU-bound |

```
AZ Layout:

┌───── AZ-1 ──────┐    ┌───── AZ-2 ──────┐    ┌───── AZ-3 ──────┐
│                  │    │                  │    │                  │
│  Primary-1       │    │  Primary-2       │    │  Primary-3       │
│  (slots 0-5460)  │    │  (slots 5461-    │    │  (slots 10923-   │
│                  │    │   10922)         │    │   16383)         │
│  Replica-3       │    │  Replica-1       │    │  Replica-2       │
│  (follows P-3)   │    │  (follows P-1)   │    │  (follows P-2)   │
│                  │    │                  │    │                  │
└──────────────────┘    └──────────────────┘    └──────────────────┘

If AZ-1 goes down:
  - Primary-1 is lost → Replica-1 (in AZ-2) is promoted to primary
  - Replica-3 (in AZ-1) is lost → Primary-3 (in AZ-3) continues serving
  - No data loss, no downtime
```

### 4.3 Cache Key Design

```
Key format:    url:{suffix}
Example key:   url:ab3k9x12

Value format:  {longUrl}:{expiryTimestamp}
Example value: https://example.com/very/long/page/with/params?id=123:1735689600

Why this format?
  - Simple string (not hash/set) → minimal memory overhead
  - Single GET retrieves all needed data
  - Parse on read: split by last ":" to get URL and expiry
  - Expiry timestamp is Unix epoch seconds (compact, easy to compare)
```

**Alternative considered: Redis Hash**
```
HSET url:ab3k9x12 longUrl "https://..." expiry "1735689600"

Pro:  Cleaner access to individual fields
Con:  ~40 bytes MORE overhead per key (hash ziplist metadata)
      Need HMGET instead of GET (slightly more complex)
      For our use case, we ALWAYS need both fields

Verdict: Simple string is better for our access pattern.
```

### 4.4 TTL Strategy

```
TTL = min(url_expiry_time - now(), MAX_CACHE_TTL)

Where MAX_CACHE_TTL depends on URL type:
  - Standard URL (302): MAX_CACHE_TTL = 3,600 seconds (1 hour)
  - Permanent URL (301): MAX_CACHE_TTL = 86,400 seconds (24 hours)
  - Custom/vanity URL: MAX_CACHE_TTL = 1,800 seconds (30 min, changes more likely)
```

**Why cap at 1 hour for standard URLs?**

```
Scenario: URL is deleted at time T

With 1-hour TTL:
  T+0:   URL deleted from database
  T+0 to T+60min: Cache still serves the old URL (stale)
  T+60min: TTL expires, next request → cache miss → DB returns null → 404
  Worst case: 1 hour of stale data

With 24-hour TTL:
  T+0:   URL deleted
  T+0 to T+24hr: Cache still serves the old URL
  Worst case: 24 hours of stale data ← unacceptable for deleted URLs

With no TTL (infinite):
  URL deleted but cache serves it forever until evicted ← terrible
```

**Active invalidation option:**
When a URL is deleted or updated, we can explicitly `DEL url:{suffix}` from Redis. But in a multi-region setup, this requires cross-region cache invalidation — added complexity for a rare operation. The 1-hour TTL is a simpler guarantee that staleness is bounded.

### 4.5 Eviction Policy

```
Redis eviction policy: allkeys-lru

How allkeys-lru works:
  1. Redis monitors memory usage
  2. When maxmemory is reached and a new write arrives:
     a. Sample N random keys (default N=5, configurable)
     b. Among the sampled keys, evict the one with the oldest last-access time
     c. Repeat until enough memory is freed
  3. Hot keys (recently accessed) survive; cold keys are evicted

Why allkeys-lru is ideal for us:
  - Zipf distribution: hot URLs are accessed frequently → high last-access time → survive eviction
  - Cold URLs (long tail): rarely accessed → low last-access time → evicted first
  - This naturally maintains the "cache the hot URLs" property
  - No configuration needed per key — Redis handles it automatically

Alternative: volatile-lru (only evict keys with TTL set)
  - Since ALL our keys have TTLs, this would be equivalent to allkeys-lru
  - allkeys-lru is more explicit about intent: "evict anything if needed"
```

### 4.6 Cache-Aside Pattern (Full Implementation)

```python
# Pseudocode for the redirect handler

function redirect(suffix):
    """
    Resolve a short URL suffix to its long URL and redirect.
    Uses cache-aside pattern: check cache first, fall back to DB on miss.
    """

    # ──────────────────────────────────────────────
    # Step 1: Check Redis cache
    # ──────────────────────────────────────────────
    cache_key = "url:" + suffix
    cached_value = redis.GET(cache_key)

    if cached_value != null:
        # Cache HIT — parse the value
        long_url, expiry_str = cached_value.rsplit(":", 1)
        expiry_time = int(expiry_str)

        # Check if URL has expired
        if expiry_time > 0 and expiry_time < now_unix():
            # URL has expired — remove from cache and return 410
            redis.DEL(cache_key)    # async, fire-and-forget
            log_analytics(suffix, "expired")
            return HTTP_410_GONE

        # Valid cache hit — redirect
        log_analytics(suffix, "cache_hit")
        return HTTP_302_REDIRECT(long_url)

    # ──────────────────────────────────────────────
    # Step 2: Cache MISS — query PostgreSQL
    # ──────────────────────────────────────────────
    row = db.read_replica.query(
        "SELECT long_url, expiry_time FROM url_mappings WHERE suffix = $1",
        [suffix]
    )

    if row == null:
        # URL never existed
        log_analytics(suffix, "not_found")
        return HTTP_404_NOT_FOUND

    if row.expiry_time != null and row.expiry_time < now_unix():
        # URL exists but has expired
        log_analytics(suffix, "expired")
        return HTTP_410_GONE

    # ──────────────────────────────────────────────
    # Step 3: Populate cache (async, fire-and-forget)
    # ──────────────────────────────────────────────
    expiry_epoch = row.expiry_time if row.expiry_time else 0   # 0 = no expiry
    cache_value = row.long_url + ":" + str(expiry_epoch)

    # TTL = min(time until URL expires, max cache TTL)
    if row.expiry_time:
        ttl_seconds = min(row.expiry_time - now_unix(), 3600)  # max 1 hour
    else:
        ttl_seconds = 3600  # no expiry → cache for 1 hour anyway

    # SET with EX (expiry in seconds) — fire and forget
    redis.SET(cache_key, cache_value, EX=ttl_seconds)  # async

    # ──────────────────────────────────────────────
    # Step 4: Return redirect
    # ──────────────────────────────────────────────
    log_analytics(suffix, "cache_miss")
    return HTTP_302_REDIRECT(row.long_url)
```

#### Cache Write-Through on URL Creation

```python
function create_url(long_url, custom_suffix=null, permanent=false, ttl_days=365):
    """
    Create a new short URL. Write to DB and warm the cache.
    """

    # ... (key generation logic — see key-generation-deep-dive.md) ...
    suffix = generate_or_claim_suffix(custom_suffix)
    expiry_time = now_unix() + (ttl_days * 86400) if ttl_days else null

    # Write to PostgreSQL (leader)
    db.leader.execute(
        "INSERT INTO url_mappings (suffix, long_url, expiry_time, creator_id, permanent) "
        "VALUES ($1, $2, $3, $4, $5)",
        [suffix, long_url, expiry_time, current_user_id, permanent]
    )

    # Warm the cache immediately (write-through)
    # This way, the first redirect doesn't need to hit the DB
    expiry_epoch = expiry_time if expiry_time else 0
    cache_value = long_url + ":" + str(expiry_epoch)
    cache_ttl = min(expiry_time - now_unix(), 3600) if expiry_time else 3600

    redis.SET("url:" + suffix, cache_value, EX=cache_ttl)

    return {"short_url": BASE_URL + "/" + suffix, "suffix": suffix}
```

### 4.7 Cache Warming Strategies

Beyond the write-through pattern above, there are situations where we need to proactively warm the cache:

```
┌──────────────────────────────────────────────────────────────────────┐
│ Cache Warming Strategies                                             │
│                                                                      │
│ 1. WRITE-THROUGH (at creation time)                                  │
│    When: URL is created                                              │
│    Action: SET in Redis immediately after DB write                   │
│    Benefit: First redirect is always a cache hit                     │
│                                                                      │
│ 2. POPULARITY-BASED PRE-WARMING                                      │
│    When: Redis restart, new region deployment, failover              │
│    Action: Query DB for top N URLs by access count, load into Redis  │
│    Implementation:                                                   │
│      SELECT suffix, long_url, expiry_time                            │
│      FROM url_mappings                                               │
│      ORDER BY access_count DESC                                      │
│      LIMIT 1000000;                                                  │
│    Benefit: Avoids "cold start" thundering herd to DB                │
│                                                                      │
│ 3. ACCESS-LOG REPLAY                                                 │
│    When: Redis restart                                               │
│    Action: Replay last 1 hour of access logs, load accessed URLs     │
│    Benefit: Warms exactly the URLs that are currently hot            │
│                                                                      │
│ 4. CROSS-REGION WARMING                                              │
│    When: New region deployed (e.g., adding AP-SOUTHEAST)             │
│    Action: Copy top 10M entries from US-EAST Redis to new region     │
│    Benefit: New region starts with warm cache                        │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 5. Hot Key Analysis

### The Viral URL Problem

A single short URL shared in a viral tweet, Reddit post, or news article can receive **millions of hits per minute**. This creates a "hot key" problem where one Redis key receives disproportionate load.

```
Example scenario:
  - Celebrity tweets a TinyURL link
  - Tweet gets 10M impressions in 5 minutes
  - 2% click-through rate = 200,000 clicks in 5 minutes
  - = 667 requests/sec to ONE URL for 5 minutes

Extreme scenario:
  - Breaking news event (election results, Super Bowl link)
  - Could see 10,000+ requests/sec to a single URL
```

### Why Hot Keys Are a Problem

```
Normal case (15K req/sec distributed across millions of URLs):
  Each Redis key: ~0.01 requests/sec average
  Each Redis primary: ~5,000 reads/sec (evenly distributed across 3 primaries)
  → No problem

Hot key case (10K req/sec to ONE key):
  One Redis primary handles ALL requests for that key
  (Redis Cluster routes by hash slot — one key = one primary)
  That primary: 10,000 + ~5,000 normal = 15,000 reads/sec
  → Network saturation on one primary, other two are idle
  → Latency spike for ALL keys on that primary's hash slots
```

### Solution 1: Local In-Process Cache (Caffeine/Guava)

The most effective solution for hot keys. Each app server maintains a small in-memory cache.

```
┌──────────┐    ┌──────────────────────────────────┐    ┌───────────┐
│          │    │  App Server                       │    │           │
│  User    │───▶│                                  │───▶│  Redis    │
│          │    │  ┌─────────────────────────────┐  │    │           │
│          │◀───│  │  Local Cache (Caffeine)     │  │◀───│           │
│          │    │  │  Size: 1,000 entries        │  │    │           │
│          │    │  │  TTL: 10 seconds            │  │    │           │
│          │    │  │  Eviction: LRU              │  │    └───────────┘
│          │    │  │                             │  │
│          │    │  │  For viral URL:             │  │    ┌───────────┐
│          │    │  │  - Receives 667 req/sec     │  │    │           │
│          │    │  │  - First request: miss → Redis│  │───▶│PostgreSQL│
│          │    │  │  - Next 6,670 requests in   │  │    │           │
│          │    │  │    10s: ALL hits (local)    │  │◀───│           │
│          │    │  │  - Hit rate: 99.98%         │  │    │           │
│          │    │  └─────────────────────────────┘  │    └───────────┘
│          │    │                                  │
│          │    └──────────────────────────────────┘
```

**Configuration:**
```java
// Java with Caffeine cache
Cache<String, CachedUrl> localCache = Caffeine.newBuilder()
    .maximumSize(1_000)            // Only top 1000 keys
    .expireAfterWrite(10, SECONDS) // Very short TTL to limit staleness
    .recordStats()                 // For monitoring
    .build();

// Lookup order: local cache → Redis → PostgreSQL
CachedUrl resolve(String suffix) {
    return localCache.get(suffix, key -> {
        // This lambda runs on cache miss
        String redisValue = redis.get("url:" + key);
        if (redisValue != null) return parse(redisValue);

        // Redis miss → DB
        Row row = db.query("SELECT ... WHERE suffix = ?", key);
        if (row != null) {
            // Populate Redis for other app servers
            redis.set("url:" + key, serialize(row), 3600);
            return new CachedUrl(row.longUrl, row.expiryTime);
        }
        return null; // 404
    });
}
```

**Why 10-second TTL?**
```
Staleness window: 10 seconds maximum
  - URL deleted at T=0
  - Local cache serves stale data until T=10
  - After T=10: local cache expires → checks Redis → Redis serves or misses

  10 seconds is acceptable because:
  - URL deletion is rare (< 0.01% of operations)
  - 10 seconds of staleness is imperceptible to users
  - The alternative (no local cache) means 10K+/sec to Redis for hot keys
```

**Why only 1,000 entries?**
```
  - 1,000 entries x ~130 bytes = ~130 KB of memory per app server (negligible)
  - With Zipf distribution, the top 1,000 URLs cover ~15% of traffic
  - Hot keys (viral URLs) will always be in the top 1,000
  - Small size = fast LRU eviction, no GC pressure
  - Larger sizes have diminishing returns (Redis already caches 72M entries)
```

### Solution 2: Redis Read Replicas

Redis Cluster already has replica nodes. By reading from replicas, we distribute the load for hot keys across multiple nodes.

```
Standard flow (read from primary only):
  Client → Primary-1 (handles ALL reads for slot)

With READONLY on replicas:
  Client → Round-robin between:
    - Primary-1
    - Replica-1 (follows Primary-1)
  → 2x read capacity for each slot

Configuration:
  Redis client setting: readFrom = REPLICA_PREFERRED
  (Read from replica if available, fall back to primary)
```

**Limitation:** Redis replicas can have slight lag (usually < 1ms). For URL redirects, this is completely acceptable — the data is immutable.

### Solution 3: Hotspot Detection and Auto-Promotion

```python
class HotspotDetector:
    """
    Track per-key access counts and auto-promote to local cache
    when a key exceeds the hot threshold.
    """

    def __init__(self, threshold_per_sec=500, window_sec=10):
        self.threshold = threshold_per_sec * window_sec  # 5000 hits in 10s
        self.window_sec = window_sec
        self.counters = {}  # key → (count, window_start)

    def record_access(self, suffix):
        now = time.time()
        if suffix not in self.counters:
            self.counters[suffix] = (1, now)
            return False

        count, window_start = self.counters[suffix]
        if now - window_start > self.window_sec:
            # New window
            self.counters[suffix] = (1, now)
            return False

        count += 1
        self.counters[suffix] = (count, window_start)

        if count >= self.threshold:
            # HOT KEY DETECTED
            log.warn(f"Hot key detected: url:{suffix} = {count} hits in {self.window_sec}s")
            promote_to_local_cache(suffix)
            alert_ops_team(suffix, count)
            return True

        return False
```

### Hot Key Summary

| Solution | Latency Impact | Complexity | When to Use |
|---|---|---|---|
| Local in-process cache | Best (0ms for hits) | Low (Caffeine/Guava) | Always (default approach) |
| Redis read replicas | Good (2x capacity) | None (config change) | Always (enable by default) |
| Hotspot detection | Best (adaptive) | Medium (monitoring) | At scale (>50K req/sec) |

**Recommendation:** Implement Solution 1 (local cache) from day one. It is trivial to add and eliminates the hot key problem entirely for the most common case.

---

## 6. Sharding Strategies Deep Dive

### When to Shard

```
PostgreSQL single-node limits (practical, not theoretical):
  - Storage: ~5-10 TB before performance degrades
  - Connections: ~500 concurrent (with pgBouncer: ~10,000)
  - Write throughput: ~10,000 writes/sec (simple inserts)
  - Read throughput: ~50,000 reads/sec (indexed lookups)

Our growth trajectory:
  Year 1:   ~533 GB (7.2B URLs)   → single node is fine
  Year 5:   ~2.7 TB (36B URLs)    → still fine, but getting large
  Year 10:  ~5.3 TB (72B URLs)    → approaching limits, should shard
  Year 15:  ~8 TB (108B URLs)     → must shard
  Year 100: ~48 TB (720B URLs)    → definitely sharded

Decision: Start with Citus extension from day one.
  - Citus makes sharding transparent to the application
  - Single-shard queries have negligible overhead (~1ms routing)
  - Can start with 1 worker node and add more as data grows
  - No application rewrite needed when it's time to shard
```

### Strategy 1: Shard by Suffix (Recommended)

```
┌──────────────────────────────────────────────────────────────────────┐
│                        Citus Coordinator                             │
│              Receives queries, routes to correct shard                │
│              Maintains shard map: hash(suffix) → worker node         │
└─────────┬───────────┬────────────┬────────────┬────────────┬────────┘
          │           │            │            │            │
   ┌──────▼─────┐ ┌───▼──────┐ ┌──▼────────┐ ┌▼─────────┐ │    ...
   │ Worker 0   │ │ Worker 1 │ │ Worker 2  │ │ Worker 3 │ │
   │            │ │          │ │           │ │          │ │
   │ Shards:    │ │ Shards:  │ │ Shards:   │ │ Shards:  │ │
   │ hash 0-127 │ │ hash     │ │ hash      │ │ hash     │ │
   │            │ │ 128-255  │ │ 256-383   │ │ 384-511  │ │
   │ ~4.8 TB    │ │ ~4.8 TB  │ │ ~4.8 TB   │ │ ~4.8 TB  │ │
   │ (at 100yr) │ │          │ │           │ │          │ │
   └────────────┘ └──────────┘ └───────────┘ └──────────┘ │
                                                           │
                                              ┌────────────▼───────┐
                                              │ ... Worker 9       │
                                              │ hash 896-1023      │
                                              │ ~4.8 TB            │
                                              └────────────────────┘
```

#### Distribution Function

```
shard_id = hash(suffix) % num_shards

Example with 10 shards:
  suffix "ab3k9x12" → hash = 7293847 → 7293847 % 10 = 7 → Worker 7
  suffix "xz9p2m4q" → hash = 3918274 → 3918274 % 10 = 4 → Worker 4

Why this distributes evenly:
  - Suffixes are random base62 strings (generated from pre-shuffled key pool)
  - hash(random_string) is uniformly distributed
  - Modulo over uniform distribution = uniform distribution across shards
  - No hotspots, no rebalancing needed
```

#### Query Routing Analysis

```
Query: SELECT long_url FROM url_mappings WHERE suffix = 'ab3k9x12'

Step 1: Coordinator receives query
Step 2: Coordinator computes hash('ab3k9x12') % 10 = 7
Step 3: Coordinator routes query to Worker 7
Step 4: Worker 7 executes local query (indexed lookup, ~1ms)
Step 5: Worker 7 returns result to Coordinator
Step 6: Coordinator returns result to client

Total overhead: ~1-2ms for routing (compared to ~5ms for the query itself)
Single shard touched: YES — this is the key benefit
```

#### Citus DDL

```sql
-- Step 1: Create the table on the coordinator (same DDL as non-sharded)
CREATE TABLE url_mappings (
    suffix       VARCHAR(12) PRIMARY KEY,
    long_url     TEXT NOT NULL,
    creator_id   BIGINT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expiry_time  TIMESTAMPTZ,
    permanent    BOOLEAN NOT NULL DEFAULT FALSE,
    access_count BIGINT NOT NULL DEFAULT 0
);

-- Step 2: Tell Citus to distribute by suffix
SELECT create_distributed_table('url_mappings', 'suffix');

-- Citus automatically:
--   1. Creates 32 shards (default, configurable)
--   2. Distributes shards across worker nodes
--   3. Routes queries based on hash(suffix)
--   4. Handles shard placement, rebalancing, and failover

-- Step 3: Create the pre-generated keys table (also sharded by suffix)
CREATE TABLE pre_generated_keys (
    suffix      VARCHAR(12) PRIMARY KEY,
    status      SMALLINT NOT NULL DEFAULT 0,  -- 0=available, 1=claimed
    claimed_at  TIMESTAMPTZ
);

SELECT create_distributed_table('pre_generated_keys', 'suffix');

-- Step 4: Verify shard distribution
SELECT * FROM citus_shards WHERE table_name = 'url_mappings';
-- Shows: shard_id, shard_name, node_name, shard_size
```

### Strategy 2: Shard by creator_id (Not Recommended)

Why someone might consider it: "List all URLs by user X" is a common query (user dashboard). If sharded by creator_id, this query hits a single shard.

#### Access Pattern Comparison

| Access Pattern | Frequency | Shard by suffix | Shard by creator_id |
|---|---|---|---|
| `GET /{suffix}` (redirect) | 15,000/sec (HOT PATH) | **1 shard** | **ALL shards (scatter-gather!)** |
| `POST /urls` (create) | 1,000/sec | **1 shard** | **1 shard** |
| `GET /users/{id}/urls` (dashboard) | ~10/sec (COLD PATH) | **ALL shards** | **1 shard** |
| `DELETE /urls/{suffix}` (delete) | ~1/sec | **1 shard** | **ALL shards** |

**The redirect is our hot path** — it is 17x more frequent than creation and 1,500x more frequent than the dashboard query. Optimizing for the hot path is critical.

#### What Happens with Scatter-Gather on Redirect?

```
Shard by creator_id — redirect query:
  SELECT long_url FROM url_mappings WHERE suffix = 'ab3k9x12'

  Coordinator doesn't know which creator made this URL.
  Must ask ALL 10 shards:
    → Worker 0: SELECT ... WHERE suffix = 'ab3k9x12'  (not here)
    → Worker 1: SELECT ... WHERE suffix = 'ab3k9x12'  (not here)
    → Worker 2: SELECT ... WHERE suffix = 'ab3k9x12'  (FOUND!)
    → Worker 3: SELECT ... WHERE suffix = 'ab3k9x12'  (not here)
    → ... (7 more empty responses)

  Latency: max(all shard responses) ≈ slowest shard ≈ ~15ms
  Load: 10x amplification (every redirect = 10 queries)

  At 15,000 redirects/sec: 150,000 queries/sec across shards
  vs. 15,000 queries/sec with shard-by-suffix

  → 10x more database load for the hot path = unacceptable
```

### Strategy 3: Hybrid — Suffix Sharding with Secondary Index

Best of both worlds: shard by suffix for the hot path, maintain a separate index for the cold path.

```
Primary table: url_mappings (sharded by suffix)
  → Redirect queries: 1 shard (fast)
  → Create: 1 shard (fast)
  → Delete by suffix: 1 shard (fast)

Secondary index table: user_urls (sharded by creator_id)
  → "List user's URLs" queries: 1 shard (fast)

┌─────────────────────────────────────────────────────┐
│  user_urls table (sharded by creator_id)            │
│                                                     │
│  CREATE TABLE user_urls (                           │
│    creator_id  BIGINT NOT NULL,                     │
│    suffix      VARCHAR(12) NOT NULL,                │
│    created_at  TIMESTAMPTZ NOT NULL,                │
│    long_url    TEXT NOT NULL,                        │
│    PRIMARY KEY (creator_id, created_at, suffix)     │
│  );                                                 │
│  SELECT create_distributed_table('user_urls',       │
│                                  'creator_id');     │
│                                                     │
│  Denormalized: stores long_url so dashboard query   │
│  doesn't need to join back to url_mappings.         │
└─────────────────────────────────────────────────────┘
```

**Cost:** Double write on URL creation (write to both tables). Acceptable because:
- Creation is 1,000/sec (low volume)
- Consistency: both writes in the same transaction (Citus supports distributed transactions)
- Dashboard is a low-frequency cold path — worth the write amplification

### Rebalancing When Adding Shards

```
Current: 5 workers, 32 shards (Citus default)
  Worker 0: shards 0-6   (7 shards)
  Worker 1: shards 7-12  (6 shards)
  Worker 2: shards 13-18 (6 shards)
  Worker 3: shards 19-25 (7 shards)
  Worker 4: shards 26-31 (6 shards)

Adding Worker 5:
  Step 1: Add new worker node
    SELECT citus_add_node('worker-5.internal', 5432);

  Step 2: Rebalance shards
    SELECT citus_rebalance_start();

  What happens:
    - Citus identifies over-loaded workers (by shard count or data size)
    - Moves shards from over-loaded to new worker using logical replication
    - Reads and writes CONTINUE during rebalancing (online operation)
    - Once a shard is fully copied, Citus atomically updates the routing table
    - Old shard copy is deleted

  After rebalancing:
    Worker 0: shards 0-4    (5 shards)
    Worker 1: shards 5-9    (5 shards)
    Worker 2: shards 10-15  (6 shards)
    Worker 3: shards 16-20  (5 shards)
    Worker 4: shards 21-25  (5 shards)
    Worker 5: shards 26-31  (6 shards)

  Duration: depends on data size. ~1TB per shard at year 50 →
            ~30 minutes per shard move at ~500MB/sec.
```

### Shard Count Planning

```
┌──────────────────────────────────────────────────────────────────────┐
│  Shard Count Strategy                                                │
│                                                                      │
│  Start with 32 shards (Citus default):                               │
│    - Even with 1 worker, 32 shards = 32 separate tables              │
│    - Each shard is smaller → faster vacuum, backup, index rebuild     │
│    - Can spread across up to 32 workers without re-sharding          │
│                                                                      │
│  Year 1:   32 shards on 1 worker   → ~17 GB per shard               │
│  Year 5:   32 shards on 2 workers  → ~84 GB per shard               │
│  Year 10:  32 shards on 4 workers  → ~166 GB per shard              │
│  Year 50:  32 shards on 8 workers  → ~830 GB per shard              │
│  Year 100: 64 shards on 16 workers → ~750 GB per shard*             │
│                                                                      │
│  * At year ~60, split from 32 to 64 shards (Citus supports online   │
│    shard splitting: SELECT citus_split_shard_by_split_points(...))   │
│                                                                      │
│  Rule of thumb: keep each shard under 1 TB for operational ease.     │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 7. Multi-Region Deployment Topology

### Full Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                      DNS (Route53 Latency-Based Routing)                    │
│                                                                             │
│      US users → US-EAST        EU users → EU-WEST       AP users → AP-EAST │
│                                                                             │
└──────────┬────────────────────────────┬───────────────────────┬─────────────┘
           │                            │                       │
           ▼                            ▼                       ▼
┌─────────────────────┐     ┌─────────────────────┐    ┌─────────────────────┐
│     US-EAST         │     │     EU-WEST         │    │     AP-EAST         │
│     (LEADER)        │     │     (FOLLOWER)      │    │     (FOLLOWER)      │
│                     │     │                     │    │                     │
│  ┌───────────────┐  │     │  ┌───────────────┐  │    │  ┌───────────────┐  │
│  │ CDN Edge POPs │  │     │  │ CDN Edge POPs │  │    │  │ CDN Edge POPs │  │
│  └───────┬───────┘  │     │  └───────┬───────┘  │    │  └───────┬───────┘  │
│          │          │     │          │          │    │          │          │
│  ┌───────▼───────┐  │     │  ┌───────▼───────┐  │    │  ┌───────▼───────┐  │
│  │ App Servers   │  │     │  │ App Servers   │  │    │  │ App Servers   │  │
│  │ (2 servers)   │  │     │  │ (2 servers)   │  │    │  │ (2 servers)   │  │
│  └───┬───────┬───┘  │     │  └───┬───────┬───┘  │    │  └───┬───────┬───┘  │
│      │       │      │     │      │       │      │    │      │       │      │
│  ┌───▼───┐ ┌─▼───┐  │     │  ┌───▼───┐ ┌─▼───┐  │    │  ┌───▼───┐ ┌─▼───┐  │
│  │ Redis │ │Redis│  │     │  │ Redis │ │Redis│  │    │  │ Redis │ │Redis│  │
│  │ 3P+3R │ │     │  │     │  │ 3P+3R │ │     │  │    │  │ 3P+3R │ │     │  │
│  └───┬───┘ └──┬──┘  │     │  └───┬───┘ └──┬──┘  │    │  └───┬───┘ └──┬──┘  │
│      │        │     │     │      │        │     │    │      │        │     │
│  ┌───▼────────▼──┐  │     │  ┌───▼────────▼──┐  │    │  ┌───▼────────▼──┐  │
│  │ PostgreSQL    │  │     │  │ PostgreSQL    │  │    │  │ PostgreSQL    │  │
│  │ LEADER        │──────async──▶ FOLLOWER    │──────async──▶ FOLLOWER    │  │
│  │ (read+write)  │  │repl │  │ (read only)  │  │repl│  │ (read only)  │  │
│  │ + 1 replica   │  │     │  │ + 1 replica   │  │    │  │ + 1 replica   │  │
│  └───────────────┘  │     │  └───────────────┘  │    │  └───────────────┘  │
│                     │     │                     │    │                     │
└─────────────────────┘     └─────────────────────┘    └─────────────────────┘
```

### Write Routing Strategy

```
┌──────────────────────────────────────────────────────────────────────┐
│                                                                      │
│  ALL writes go to US-EAST (leader region)                            │
│                                                                      │
│  Why single-leader for writes:                                       │
│    1. Key generation uses FOR UPDATE SKIP LOCKED on pre-generated    │
│       key pool — requires serializable access to avoid duplicates    │
│    2. Writes are only 1,000/sec peak — one leader handles this      │
│    3. Multi-master writes would require conflict resolution for      │
│       suffix uniqueness — complex and error-prone                    │
│                                                                      │
│  Latency impact:                                                     │
│    US users creating URLs:  ~10ms  (local)                           │
│    EU users creating URLs: ~100ms  (cross-Atlantic)                  │
│    AP users creating URLs: ~150ms  (cross-Pacific)                   │
│                                                                      │
│  Is 100-150ms acceptable for URL creation?                           │
│    YES — URL creation is a background action (user fills a form,     │
│    clicks "create", waits for response). 150ms is imperceptible.     │
│    Compare to redirect: 150ms would be noticeable (user clicks a     │
│    link and expects instant navigation).                             │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

### Read Routing Strategy

```
Reads are served from the NEAREST region:

US user clicks tinyurl.com/ab3k9x12:
  1. DNS (Route53) → US-EAST
  2. CDN POP in US → cache hit? Return 302 (5ms)
  3. CDN miss → App server in US-EAST
  4. Redis in US-EAST → cache hit? Return 302 (2ms)
  5. Redis miss → PostgreSQL read replica in US-EAST (10ms)
  → Total: 2-15ms

EU user clicks the same URL:
  1. DNS (Route53) → EU-WEST
  2. CDN POP in EU → cache hit? Return 302 (5ms)
  3. CDN miss → App server in EU-WEST
  4. Redis in EU-WEST → cache hit? Return 302 (2ms)
  5. Redis miss → PostgreSQL follower in EU-WEST (10ms)
  → Total: 2-15ms (same! reads are always local)

Key insight: reads NEVER cross regions. Each region has a complete
read path: CDN → Redis → DB replica.
```

### Replication Lag and Consistency

```
Async replication lag: US-EAST → EU-WEST / AP-EAST

Typical lag:
  US → EU:  50-200ms  (transatlantic cable)
  US → AP: 100-300ms  (transpacific cable)

Worst case (network congestion, large write batch):
  US → EU:  500ms
  US → AP:  1,000ms (1 second)

Impact analysis:
───────────────

Scenario: User in US creates tinyurl.com/newlink, shares it with colleague in EU

Timeline:
  T=0ms:     User clicks "Create" → request goes to US-EAST leader
  T=10ms:    URL written to US-EAST PostgreSQL
  T=10ms:    Redis in US-EAST warmed (write-through)
  T=50ms:    User sees "URL created: tinyurl.com/newlink"
  T=50ms:    User copies link, pastes in Slack/email to EU colleague
  T=2000ms+: EU colleague sees the message and clicks the link (minimum human latency)
  T=110ms:   Meanwhile, async replication already delivered to EU-WEST (at T=60-210ms)
  T=2010ms:  EU colleague's request hits EU-WEST → URL exists in DB → redirect works

The human latency of sharing a link (typing, sending, reading, clicking) is
ALWAYS longer than replication lag. Replication lag is invisible to users.

Exception: Automated sharing (API creates URL and immediately posts to webhook)
  - Could theoretically fail if webhook recipient is in different region
  - AND they click within 200ms of creation
  - Mitigation: on 404 for a recently-created URL, retry once from leader region
```

### Cross-Region Cache Consistency

```
Each region has its own independent Redis cache.
They are NOT synchronized with each other.

Why not synchronize Redis across regions?
  1. Cross-region Redis replication adds latency and complexity
  2. Cache is populated on-demand (cache-aside): each region warms its
     own cache based on its traffic pattern
  3. EU's hot URLs are different from AP's hot URLs — independent caches
     are more efficient
  4. Cache data is derived from DB — DB replication is the source of truth

What happens on cache miss in EU?
  1. Redis miss in EU → query PostgreSQL follower in EU
  2. EU follower has the data (via async replication from US leader)
  3. Populate EU Redis from EU PostgreSQL
  4. Future EU requests for this URL hit EU Redis

The only edge case: URL created in US, EU user accesses within replication lag window
  → EU PostgreSQL doesn't have it yet
  → Return 404
  → Client can retry (or we can query US-EAST as fallback — see below)
```

### Fallback for Replication Lag (Optional Enhancement)

```python
function redirect_with_fallback(suffix, region):
    # Standard flow: check local region
    result = resolve_locally(suffix)  # Redis → local DB

    if result != null:
        return result

    # URL not found locally. Two possibilities:
    # 1. URL genuinely doesn't exist (404)
    # 2. URL was JUST created and replication hasn't caught up

    if region != "US-EAST":
        # Check if this might be a replication lag issue
        # Only do this for very recent URLs (optimization: check creation timestamp)
        leader_result = query_leader_region(suffix)  # Cross-region call ~100-150ms

        if leader_result != null:
            # Replication lag confirmed — URL exists in leader
            # Populate local cache and return
            populate_local_redis(suffix, leader_result)
            return leader_result

    # URL genuinely doesn't exist
    return HTTP_404_NOT_FOUND
```

**Note:** This fallback adds 100-150ms for true 404s in non-leader regions (because we always check the leader). To avoid this penalty, only enable the fallback for the first N minutes after a URL could have been created (e.g., if the request comes with a `Referer` header from the TinyURL creation page).

### Active-Active Alternative (Advanced)

For organizations that need zero cross-region write latency:

```
┌──────────────────────────────────────────────────────────────────────┐
│  Active-Active: Each Region Has Its Own Leader                       │
│                                                                      │
│  Key space partitioning:                                             │
│    US-EAST:  generates suffixes starting with [a-i]  (9/26 = 35%)   │
│    EU-WEST:  generates suffixes starting with [j-r]  (9/26 = 35%)   │
│    AP-EAST:  generates suffixes starting with [s-z]  (8/26 = 30%)   │
│                                                                      │
│  How it works:                                                       │
│    - Each region has its own pre-generated key pool (region-prefixed) │
│    - US users create URLs with US-prefixed suffixes → local write     │
│    - EU users create URLs with EU-prefixed suffixes → local write     │
│    - No cross-region coordination needed for writes                  │
│    - Async replication for reads: all regions replicate to each other │
│                                                                      │
│  Advantages:                                                         │
│    - Zero cross-region write latency                                 │
│    - No single point of failure for writes                           │
│    - Each region is fully independent                                │
│                                                                      │
│  Disadvantages:                                                      │
│    - More complex key generation (region-aware)                      │
│    - Replication topology is more complex (mesh instead of star)     │
│    - Conflict resolution needed if regions share key space           │
│    - Harder to reason about consistency                              │
│                                                                      │
│  Verdict: Not worth the complexity for TinyURL.                      │
│    - Write latency of 100-150ms is acceptable for URL creation       │
│    - Single-leader is simpler to reason about                        │
│    - Use active-active only if write latency is truly critical       │
│      (e.g., gaming, financial trading — not URL shortening)          │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 8. Capacity Planning

### Per-Component Sizing (Single Region)

| Component | Instance Type | Specs | Count per Region | Purpose |
|---|---|---|---|---|
| App Server | c5.xlarge | 4 vCPU, 8 GB RAM | 2-4 | HTTP request handling, routing |
| Redis Primary | r6g.xlarge | 4 vCPU, 32 GB RAM | 3 | Hot URL cache (primary shards) |
| Redis Replica | r6g.xlarge | 4 vCPU, 32 GB RAM | 3 | Cache HA + read distribution |
| DB Leader | r5.4xlarge | 16 vCPU, 128 GB RAM | 1 (US-EAST only) | Writes + key generation |
| DB Read Replica | r5.2xlarge | 8 vCPU, 64 GB RAM | 1-2 | Read queries on cache miss |
| DB Follower | r5.2xlarge | 8 vCPU, 64 GB RAM | 1 (EU/AP) | Cross-region async replica |
| Load Balancer | ALB | Managed | 1 | Request distribution |
| CDN | CloudFront | Managed (400+ POPs) | N/A | Edge caching |

### Why These Instance Types?

```
App Servers: c5.xlarge (compute-optimized)
  - CPU-bound: JSON parsing, hash computation, HTTP handling
  - 8 GB RAM is enough for JVM + local Caffeine cache
  - At 2,000 req/sec per server, 4 servers handle 8,000 req/sec (more than CDN-miss traffic)

Redis: r6g.xlarge (memory-optimized, ARM/Graviton)
  - Memory-bound: 32 GB RAM per node, ~10 GB used
  - ARM (Graviton2): 20% cheaper than x86 for same specs
  - r6g provides best price/performance for Redis workloads

DB Leader: r5.4xlarge (memory-optimized, larger)
  - Needs more RAM for PostgreSQL shared_buffers (32 GB recommended for large DB)
  - 16 vCPU for handling writes + background processes (vacuum, replication)
  - 128 GB RAM for OS cache + PostgreSQL buffers

DB Replicas: r5.2xlarge (memory-optimized, smaller)
  - Less CPU needed (read-only, no write overhead)
  - 64 GB RAM still provides good caching of hot data pages
```

### Storage Growth Projection

| Timeframe | URLs Created | Cumulative Storage | Shard Count | Workers Needed |
|---|---|---|---|---|
| Year 1 | 7.2 billion | ~533 GB | 32 (1 worker) | 1 |
| Year 2 | 14.4 billion | ~1.1 TB | 32 (1 worker) | 1 |
| Year 5 | 36 billion | ~2.7 TB | 32 (2 workers) | 2 |
| Year 10 | 72 billion | ~5.3 TB | 32 (4 workers) | 4 |
| Year 20 | 144 billion | ~10.7 TB | 32 (4 workers) | 4 |
| Year 50 | 360 billion | ~26.6 TB | 32 (8 workers) | 8 |
| Year 100 | 720 billion | ~48 TB | 64 (16 workers) | 16 |

**Storage per URL:**
```
url_mappings table:
  suffix (8 bytes) + long_url (50 bytes avg) + creator_id (8 bytes) +
  created_at (8 bytes) + expiry_time (8 bytes) + permanent (1 byte) +
  access_count (8 bytes) + row overhead (24 bytes) = ~115 bytes per row

With index (B-tree on suffix):
  ~50 bytes per index entry

Total per URL: ~165 bytes → round to ~170 bytes

Verification: 720B URLs x 170 bytes = ~122 TB raw
  But: many URLs expire and are cleaned up
  With 30% still active at any time: ~36.6 TB
  With index: ~48 TB (matches our estimate)

  Note: the 48 TB estimate accounts for indexes, TOAST storage for long URLs,
  and PostgreSQL page overhead (each 8KB page has ~24 bytes of header).
```

### Network Bandwidth Requirements

```
Per redirect request:
  Incoming:  ~200 bytes (HTTP GET /ab3k9x12)
  Outgoing:  ~300 bytes (HTTP 302 + Location header + Cache-Control)
  Total:     ~500 bytes per redirect

At 15,000 req/sec peak:
  Bandwidth: 15,000 x 500 bytes = 7.5 MB/sec = 60 Mbps

Per app server (4 servers, 3,750 req/sec each):
  Bandwidth: 3,750 x 500 = 1.875 MB/sec = 15 Mbps per server
  → c5.xlarge has up to 10 Gbps network → no bottleneck

Redis network (12,000 req/sec to Redis, ~100 bytes per GET response):
  Bandwidth: 12,000 x 100 = 1.2 MB/sec = 9.6 Mbps
  → r6g.xlarge has up to 10 Gbps → no bottleneck

Conclusion: Network is NOT a bottleneck at our scale.
Even at 10x growth (150K req/sec), network is fine.
```

### Cost Estimate (Monthly)

#### US-EAST Region (Leader)

| Component | Count | Instance Type | Unit Cost (On-Demand) | Monthly Cost |
|---|---|---|---|---|
| App Servers | 4 | c5.xlarge | $124/mo | $496 |
| Redis Primary | 3 | r6g.xlarge | $197/mo | $591 |
| Redis Replica | 3 | r6g.xlarge | $197/mo | $591 |
| DB Leader | 1 | r5.4xlarge | $1,459/mo | $1,459 |
| DB Read Replica | 2 | r5.2xlarge | $730/mo | $1,460 |
| Storage (gp3) | 1 TB | gp3 SSD | $80/TB/mo | $80 |
| ALB | 1 | Managed | ~$50/mo | $50 |
| CloudFront | ~1B req/mo | Managed | $0.0075/10K req | $750 |
| Route53 | 1 hosted zone | Managed | $0.50/zone + queries | $25 |
| **Subtotal (US-EAST)** | | | | **$5,502/mo** |

#### EU-WEST and AP-EAST (Follower Regions)

| Component | Count | Instance Type | Unit Cost | Monthly Cost |
|---|---|---|---|---|
| App Servers | 2 | c5.xlarge | $124/mo | $248 |
| Redis Primary | 3 | r6g.xlarge | $197/mo | $591 |
| Redis Replica | 3 | r6g.xlarge | $197/mo | $591 |
| DB Follower | 1 | r5.2xlarge | $730/mo | $730 |
| DB Read Replica | 1 | r5.2xlarge | $730/mo | $730 |
| Storage (gp3) | 1 TB | gp3 SSD | $80/TB/mo | $80 |
| ALB | 1 | Managed | ~$50/mo | $50 |
| CloudFront | ~500M req/mo | Managed | $0.0075/10K req | $375 |
| **Subtotal (per follower)** | | | | **$3,395/mo** |

#### Total Cost Summary

| Region | Monthly Cost |
|---|---|
| US-EAST (leader) | $5,502 |
| EU-WEST (follower) | $3,395 |
| AP-EAST (follower) | $3,395 |
| Cross-region data transfer | ~$500 |
| Monitoring (CloudWatch, Datadog) | ~$300 |
| **Total (all regions)** | **~$13,092/mo** |

#### Cost with Reserved Instances (1-year, no upfront)

| | On-Demand | Reserved (1yr) | Savings |
|---|---|---|---|
| Compute + Redis + DB | $11,000/mo | $7,700/mo | 30% |
| Total with managed services | $13,092/mo | ~$9,800/mo | 25% |

#### Cost per Request

```
Monthly requests: ~10 billion (reads + writes)
Monthly cost: ~$13,000

Cost per 1 million requests: $1.30
Cost per single request: $0.0000013 (~0.00013 cents)

For comparison:
  - A single redirect costs 0.13 microcents
  - A viral URL with 10M clicks costs ~$13
  - The infrastructure pays for itself with minimal monetization
```

---

## 9. Load Testing and Performance Benchmarks

### Benchmark Methodology

```
Tool: k6 (Grafana k6) or wrk2
Environment: Staging cluster (identical config to production)
Duration: 30 minutes sustained load per test
Metrics: p50, p95, p99, p99.9 latency; throughput; error rate

Test Types:
  1. Redirect throughput (read path)
  2. Create throughput (write path)
  3. Mixed workload (17:1 read:write ratio)
  4. Hot key stress test
  5. Cache failure test (Redis down)
  6. CDN bypass test (all traffic hits origin)
```

### Expected Benchmark Results

#### Redirect Latency (Read Path)

```
Conditions: 15,000 req/sec sustained, Zipf distribution URL selection

                     p50        p95        p99        p99.9
                     ───        ───        ───        ─────
CDN hit:             3ms        5ms        8ms        15ms
Redis hit:           2ms        4ms        6ms        12ms
DB hit (cache miss): 8ms       15ms       25ms        50ms
Weighted average:    3ms        6ms       12ms        30ms

Throughput:
  Target:    15,000 req/sec
  Achieved:  18,000 req/sec (headroom before saturation)
  Error rate: 0.001% (transient network errors)
```

#### Create Latency (Write Path)

```
Conditions: 1,000 req/sec sustained

                     p50        p95        p99        p99.9
                     ───        ───        ───        ─────
Key claim + insert:  8ms       15ms       25ms        60ms
+ Redis warm:        9ms       17ms       28ms        65ms

Throughput:
  Target:    1,000 writes/sec
  Achieved:  3,000 writes/sec (headroom)
  Error rate: 0.01% (mostly key contention retries)
```

#### Hot Key Stress Test

```
Conditions: 10,000 req/sec to ONE URL + 5,000 req/sec normal traffic

Without local cache:
  Hot key latency:   p99 = 45ms (Redis primary saturated)
  Normal key latency: p99 = 35ms (elevated due to shared Redis primary)

With local cache (Caffeine, 10s TTL):
  Hot key latency:   p99 = 0.5ms (served from local JVM memory)
  Normal key latency: p99 = 6ms (Redis not affected)

Conclusion: Local cache reduces hot key p99 by 90x.
```

#### Cache Failure Test (Redis Cluster Down)

```
Conditions: Redis cluster fully unavailable, 15,000 req/sec sustained

Without circuit breaker:
  Every request times out waiting for Redis (3 second timeout)
  Then falls through to DB
  Latency: p50 = 3,010ms (3s timeout + 10ms DB) ← TERRIBLE
  DB receives 15,000 req/sec → overloaded → cascading failure

With circuit breaker (Resilience4j / Hystrix):
  After 5 consecutive Redis failures: circuit OPENS
  All requests bypass Redis, go directly to DB
  Latency: p50 = 10ms (just DB)
  DB receives 15,000 req/sec → need 3 replicas (same as Iteration 1)

  Circuit breaker config:
    failureRateThreshold: 50
    slidingWindowSize: 10
    waitDurationInOpenState: 30s
    permittedNumberOfCallsInHalfOpenState: 3

  Recovery: circuit half-opens after 30s, tests Redis with 3 probe requests
            if Redis is back, circuit closes and normal flow resumes
```

### Benchmark Setup Script (k6)

```javascript
// k6 load test script for redirect endpoint
import http from 'k6/http';
import { check, sleep } from 'k6';
import { Rate, Trend } from 'k6/metrics';

// Custom metrics
const redirectLatency = new Trend('redirect_latency', true);
const errorRate = new Rate('error_rate');

// Test configuration
export const options = {
    scenarios: {
        redirect_load: {
            executor: 'constant-arrival-rate',
            rate: 15000,         // 15K req/sec
            timeUnit: '1s',
            duration: '30m',
            preAllocatedVUs: 500,
            maxVUs: 1000,
        },
    },
    thresholds: {
        'redirect_latency': ['p(95)<10', 'p(99)<25'],  // ms
        'error_rate': ['rate<0.001'],                    // 0.1%
    },
};

// Pool of short URL suffixes (pre-populated, Zipf-weighted)
const suffixes = open('./suffixes.txt').split('\n');

export default function() {
    // Select suffix with Zipf-like distribution
    const idx = Math.floor(Math.pow(Math.random(), 2) * suffixes.length);
    const suffix = suffixes[idx];

    const res = http.get(`https://tinyurl.example.com/${suffix}`, {
        redirects: 0,  // Don't follow redirect — just measure the 302
    });

    redirectLatency.add(res.timings.duration);

    check(res, {
        'is 302': (r) => r.status === 302,
        'has Location header': (r) => r.headers['Location'] !== undefined,
        'latency < 50ms': (r) => r.timings.duration < 50,
    });

    errorRate.add(res.status !== 302 && res.status !== 301);
}
```

---

## 10. Failure Modes and Resilience

### Failure Scenario Matrix

| Failure | Impact | Detection | Mitigation | Recovery Time |
|---|---|---|---|---|
| Single Redis node down | Cluster auto-failover to replica | Redis Cluster heartbeat (1s) | Replica promoted automatically | ~5-15 seconds |
| Entire Redis cluster down | All reads hit DB (15K/sec) | Health check failure | Circuit breaker bypasses Redis; DB absorbs load | ~30 seconds (circuit breaker) |
| DB read replica down | Remaining replicas absorb load | PostgreSQL streaming replication lag alert | ALB health check removes dead replica | ~30 seconds |
| DB leader down | No writes possible | PostgreSQL pg_isready health check | Promote replica to leader (manual or Patroni auto) | ~30-60 seconds (auto), ~5 min (manual) |
| CDN outage | All traffic hits origin (2x load) | Synthetic monitoring from multiple regions | Origin auto-scales app servers; Redis absorbs most reads | ~1-5 minutes (auto-scale) |
| US-EAST region down | No writes globally; EU/AP reads continue | Route53 health check | Route53 fails writes to EU (if active-active) or queue writes | ~60 seconds (DNS failover) |
| Network partition (US-EU) | EU reads continue from local replica; EU writes fail | Cross-region replication lag alarm | EU users see stale data (bounded by last replication); writes queued | Variable (depends on partition duration) |

### Graceful Degradation Strategy

```
Priority 1 (MUST work): Redirects for existing URLs
  → Served from CDN, Redis, or DB replicas
  → Multiple layers of redundancy
  → Even if US-EAST is down, EU/AP serve reads from local data

Priority 2 (SHOULD work): URL creation
  → Requires leader (US-EAST)
  → If leader is down: queue writes and retry, or return 503 with retry-after
  → Writes are 17x less frequent than reads — acceptable to degrade

Priority 3 (NICE to have): Analytics, dashboard, user management
  → Can be completely unavailable during outages
  → Non-critical for core functionality
```

### Circuit Breaker Configuration

```
┌──────────────────────────────────────────────────────────────────────┐
│  Circuit Breaker: Redis                                              │
│                                                                      │
│  CLOSED (normal):                                                    │
│    All requests go to Redis                                          │
│    If 5 of last 10 requests fail → OPEN                              │
│                                                                      │
│  OPEN (Redis assumed down):                                          │
│    All requests bypass Redis → go directly to DB                     │
│    After 30 seconds → HALF-OPEN                                      │
│                                                                      │
│  HALF-OPEN (testing):                                                │
│    Allow 3 probe requests to Redis                                   │
│    If all 3 succeed → CLOSED (resume normal)                         │
│    If any fail → OPEN (wait another 30 seconds)                      │
│                                                                      │
│        ┌─────────┐    5/10 fail    ┌──────────┐                      │
│        │ CLOSED  │────────────────▶│  OPEN    │                      │
│        │         │◀────────────────│          │                      │
│        └─────────┘   3/3 succeed   └────┬─────┘                      │
│             ▲                            │ 30s                        │
│             │         ┌─────────────┐    │                            │
│             └─────────│ HALF-OPEN   │◀───┘                            │
│            3/3 succeed│ (3 probes)  │                                 │
│                       └─────────────┘                                 │
│                            │ any fail                                 │
│                            └──────▶ back to OPEN                     │
└──────────────────────────────────────────────────────────────────────┘
```

### Database Connection Pooling

```
┌──────────────────────────────────────────────────────────────────────┐
│  PgBouncer Configuration (per region)                                │
│                                                                      │
│  Pool mode: transaction (release connection after each transaction)   │
│  Max client connections: 10,000 (from all app servers)               │
│  Max server connections: 100 (to PostgreSQL)                         │
│  Default pool size: 20 per database                                  │
│  Reserve pool size: 5                                                │
│  Reserve pool timeout: 3s                                            │
│                                                                      │
│  Why PgBouncer is critical:                                          │
│    - PostgreSQL max_connections default: 100                          │
│    - Each connection uses ~10 MB of RAM                              │
│    - 4 app servers × 50 connections each = 200 connections           │
│    - Without pooling: need 200+ PostgreSQL connections               │
│    - With PgBouncer: 200 client connections multiplexed to 20 server │
│      connections (10:1 multiplexing ratio)                            │
│                                                                      │
│  This matters for the "Redis down" scenario:                         │
│    - All 15,000 req/sec hit DB directly                              │
│    - Without PgBouncer: connection exhaustion in seconds             │
│    - With PgBouncer: connections are multiplexed, DB handles the load│
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

### Health Check Endpoints

```
App Server health checks:

GET /health/ready
  Returns 200 if:
    - App server is accepting requests
    - Redis is reachable (or circuit breaker is handling it)
    - At least one DB read replica is reachable
  Used by: ALB to route traffic

GET /health/live
  Returns 200 if:
    - App server process is running
    - JVM is not in GC death spiral (< 90% GC overhead)
  Used by: Kubernetes liveness probe (restart if unhealthy)

GET /health/detailed
  Returns JSON with component status:
  {
    "status": "degraded",
    "components": {
      "redis": {"status": "down", "circuit_breaker": "open"},
      "db_leader": {"status": "up", "connections": 18},
      "db_replica": {"status": "up", "replication_lag_ms": 50},
      "cdn": {"status": "up"}
    },
    "uptime_seconds": 86400
  }
  Used by: Monitoring dashboards, PagerDuty alerts
```

---

## Appendix: Key Formulas and Numbers

Quick reference for system design interview discussions:

```
Cache Hit Rates (Zipf, s=1.0):
  Cache 1% of URLs   → 50% hit rate
  Cache 10% of URLs  → 80% hit rate
  Cache 20% of URLs  → 90% hit rate

Traffic Reduction per Layer:
  Browser cache:  30% absorbed (depends on TTL and repeat visit rate)
  CDN:            50% of remaining (depends on TTL and Zipf distribution)
  Redis:          80% of remaining (depends on cache size and working set)

  Combined: 1 - (0.70 × 0.50 × 0.20) = 93% before DB

Latency by Layer:
  Browser cache:  0ms (local)
  CDN edge:       3-10ms (geographic proximity)
  Redis:          1-3ms (in-region network hop)
  PostgreSQL:     5-15ms (indexed lookup including network)

Single PostgreSQL Node Limits:
  Simple SELECTs by PK:  ~5,000-50,000/sec (depends on data in buffer cache)
  Simple INSERTs:        ~5,000-10,000/sec (depends on WAL and fsync settings)
  Storage:               ~5-10 TB practical limit

Single Redis Node Limits:
  GET/SET:               ~100,000-200,000 ops/sec
  Memory:                ~25 GB usable (of 32 GB instance)
  Network:               ~1 GB/sec

Cost per Request (at our scale):
  ~$0.0000013 per request ($1.30 per million requests)
```

---

*This document complements the [Interview Simulation](interview-simulation.md). For database design decisions, see [SQL vs NoSQL Tradeoffs](sql-vs-nosql-tradeoffs.md). For detailed system flows, see [Flow](flow.md). For the core key generation algorithm, see [Key Generation Deep Dive](key-generation-deep-dive.md).*
