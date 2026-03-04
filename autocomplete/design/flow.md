# Autocomplete System — System Flows

> This document depicts and explains all the major flows in the autocomplete system. Each flow includes a sequence diagram, step-by-step breakdown, and discussion of edge cases.

---

## Table of Contents

1. [Autocomplete Query Flow (CDN Hit)](#1-autocomplete-query-flow-cdn-hit)
2. [Autocomplete Query Flow (Full Path — Cache Miss)](#2-autocomplete-query-flow-full-path--cache-miss)
3. [Trie Build Flow (Batch)](#3-trie-build-flow-batch)
4. [Real-Time Trending Injection Flow](#4-real-time-trending-injection-flow)
5. [Cache Warm-Up Flow](#5-cache-warm-up-flow)
6. [Trie Deployment Flow (Blue-Green)](#6-trie-deployment-flow-blue-green)
7. [Content Filtering Flow](#7-content-filtering-flow)
8. [Personalization Overlay Flow](#8-personalization-overlay-flow)
9. [Failure / Fallback Flow](#9-failure--fallback-flow)
10. [Trie Shard Rebalancing Flow](#10-trie-shard-rebalancing-flow)

---

## 1. Autocomplete Query Flow (CDN Hit)

### Happy Path — Prefix Cached at CDN Edge

```
User Browser              CDN (CloudFront)          API Gateway       Autocomplete Svc      Trie Server
    │                          │                         │                   │                   │
    │  1. User types "am"      │                         │                   │                   │
    │     (debounce 150ms)     │                         │                   │                   │
    │                          │                         │                   │                   │
    │  2. GET /v1/suggestions  │                         │                   │                   │
    │     ?prefix=am&limit=10  │                         │                   │                   │
    │─────────────────────────▶│                         │                   │                   │
    │                          │                         │                   │                   │
    │                          │  3. Cache lookup:       │                   │                   │
    │                          │     key="am:en"         │                   │                   │
    │                          │     CACHE HIT!          │                   │                   │
    │                          │                         │                   │                   │
    │  4. 200 OK               │                         │                   │                   │
    │  Cache-Control:          │                         │                   │                   │
    │    max-age=300           │                         │                   │                   │
    │  X-Served-From: cdn      │                         │                   │                   │
    │                          │                         │                   │                   │
    │  {suggestions: [         │                         │                   │                   │
    │    "amazon prime",       │                         │                   │                   │
    │    "amazon kindle",      │                         │                   │                   │
    │    "amazon music", ...   │                         │                   │                   │
    │  ]}                      │                         │                   │                   │
    │◀─────────────────────────│                         │                   │                   │
    │                          │                         │                   │                   │
    │  5. Render suggestions   │                         │                   │                   │
    │     in dropdown          │                         │                   │                   │
    │                          │                         │                   │                   │
```

### Step-by-Step Breakdown

| Step | Component | Action | Latency |
|---|---|---|---|
| 1 | Browser | User types "am". Previous keystroke "a" was debounced (150ms timer reset). Timer fires for "am". | 150ms (debounce) |
| 2 | Browser | Sends GET request to CDN edge POP. Browser cache miss (first time or TTL expired). | ~0.1ms |
| 3 | CDN | Looks up cache key "am:en" (prefix + language). Found in edge cache (populated by previous user). | ~0.5ms |
| 4 | CDN | Returns cached response with `Cache-Control: max-age=300` and `X-Served-From: cdn`. | ~0.1ms |
| 5 | Browser | Renders 10 suggestions in the search dropdown. Caches response locally (max-age=60). | ~1ms (render) |

**Total user-perceived latency: ~5-10ms** (dominated by network RTT to nearest CDN POP)

### Edge Cases

- **Empty prefix** (user focuses search box without typing): Return global top-10. Highly cacheable at CDN.
- **Very long prefix** (50+ characters): Unlikely to be cached. Falls through to trie server. Still fast (O(L) lookup).
- **Special characters in prefix**: URL-encode. CDN caches the encoded form. Trie handles normalized input.

---

## 2. Autocomplete Query Flow (Full Path — Cache Miss)

### All Cache Layers Miss — Request Reaches Trie Server

```
User Browser         CDN            API Gateway      Autocomplete Svc      Redis Cache       Trie Server
    │                 │                  │                  │                   │                 │
    │ 1. GET          │                  │                  │                   │                 │
    │  ?prefix=amazo  │                  │                  │                   │                 │
    │  &limit=10      │                  │                  │                   │                 │
    │────────────────▶│                  │                  │                   │                 │
    │                 │                  │                  │                   │                 │
    │                 │ 2. Cache MISS    │                  │                   │                 │
    │                 │    (long-tail    │                  │                   │                 │
    │                 │     prefix)      │                  │                   │                 │
    │                 │─────────────────▶│                  │                   │                 │
    │                 │                  │                  │                   │                 │
    │                 │                  │ 3. Auth +        │                   │                 │
    │                 │                  │    rate limit    │                   │                 │
    │                 │                  │    check         │                   │                 │
    │                 │                  │─────────────────▶│                   │                 │
    │                 │                  │                  │                   │                 │
    │                 │                  │                  │ 4. Redis lookup   │                 │
    │                 │                  │                  │    key: suggest   │                 │
    │                 │                  │                  │    ions:v42:amazo │                 │
    │                 │                  │                  │──────────────────▶│                 │
    │                 │                  │                  │                   │                 │
    │                 │                  │                  │ 5. MISS           │                 │
    │                 │                  │                  │◀──────────────────│                 │
    │                 │                  │                  │                   │                 │
    │                 │                  │                  │ 6. Trie lookup    │                 │
    │                 │                  │                  │    search("amazo")│                 │
    │                 │                  │                  │──────────────────────────────────── ▶│
    │                 │                  │                  │                   │                 │
    │                 │                  │                  │                   │   7. Traverse   │
    │                 │                  │                  │                   │   trie:         │
    │                 │                  │                  │                   │   root→"am"→    │
    │                 │                  │                  │                   │   "azon"→       │
    │                 │                  │                  │                   │   return top_k  │
    │                 │                  │                  │                   │                 │
    │                 │                  │                  │ 8. Results:       │                 │
    │                 │                  │                  │ [amazon prime,    │                 │
    │                 │                  │                  │  amazon kindle...]│                 │
    │                 │                  │                  │◀─────────────────────────────────── │
    │                 │                  │                  │                   │                 │
    │                 │                  │                  │ 9. Apply content  │                 │
    │                 │                  │                  │    filter         │                 │
    │                 │                  │                  │                   │                 │
    │                 │                  │                  │ 10. Cache in      │                 │
    │                 │                  │                  │     Redis (async) │                 │
    │                 │                  │                  │──────────────────▶│                 │
    │                 │                  │                  │                   │                 │
    │                 │                  │ 11. Return       │                   │                 │
    │                 │                  │◀─────────────────│                   │                 │
    │                 │                  │                  │                   │                 │
    │ 12. 200 OK      │                  │                  │                   │                 │
    │  X-Served-From: │                  │                  │                   │                 │
    │    trie          │                  │                  │                   │                 │
    │◀────────────────│◀─────────────────│                  │                   │                 │
    │                 │                  │                  │                   │                 │
    │                 │ 13. CDN caches   │                  │                   │                 │
    │                 │     response     │                  │                   │                 │
    │                 │     (TTL: 300s)  │                  │                   │                 │
    │                 │                  │                  │                   │                 │
```

### Step-by-Step Breakdown

| Step | Component | Action | Latency |
|---|---|---|---|
| 1 | Browser | Sends GET request after debounce fires. Browser cache miss. | ~0.1ms |
| 2 | CDN | Cache miss — "amazo" is a long-tail prefix, not cached at this edge POP. Forwards to origin. | ~1ms (cache lookup) |
| 3 | API Gateway | Validates API key, checks rate limit (token bucket: 100 req/sec per user). Passes. | ~1ms |
| 4 | Autocomplete Svc | Looks up Redis: `suggestions:v42:amazo`. | ~0.1ms (send) |
| 5 | Redis | Cache miss — this prefix hasn't been queried since the last trie deploy. | ~2ms (RTT) |
| 6 | Autocomplete Svc | Forwards to trie server (selected by load balancer, round-robin). | ~0.1ms (send) |
| 7 | Trie Server | Traverses compressed trie: root → "am" edge → "azon" edge (partial match at "amazo"). Returns precomputed top_k from the "amazon" node. | ~0.1ms |
| 8 | Trie Server | Returns 10 ScoredSuggestion objects. | ~0.1ms |
| 9 | Autocomplete Svc | Checks results against Redis blocklist. All clean. | ~0.5ms |
| 10 | Autocomplete Svc | Async: writes results to Redis with TTL=600s. Fire-and-forget. | ~0ms (async) |
| 11 | Autocomplete Svc | Returns JSON response to API Gateway. | ~0.1ms |
| 12 | CDN | Passes response to browser. CDN caches with TTL=300s for future requests. | ~1ms |

**Total user-perceived latency: ~30-50ms** (dominated by network RTTs: browser→CDN→origin→Redis→trie)

### Latency Budget Breakdown

```
┌────────────────────────────────────────────────────────────────┐
│                    50ms TOTAL BUDGET                            │
│                                                                │
│  ┌──────┐ ┌──────┐ ┌──────┐ ┌────┐ ┌────┐ ┌──────┐ ┌──────┐ │
│  │ Net  │ │ CDN  │ │ API  │ │Red │ │Trie│ │Filter│ │ Net  │ │
│  │ RTT  │ │ miss │ │ GW   │ │miss│ │serv│ │      │ │ RTT  │ │
│  │ 15ms │ │ 1ms  │ │ 1ms  │ │2ms │ │1ms │ │ 1ms  │ │ 15ms │ │
│  └──────┘ └──────┘ └──────┘ └────┘ └────┘ └──────┘ └──────┘ │
│  ◄─────── request ──────────────────────────── response ─────▶│
└────────────────────────────────────────────────────────────────┘

Network RTTs dominate. Server processing is < 5ms total.
```

---

## 3. Trie Build Flow (Batch)

### End-to-End Pipeline: Logs → Trie → Deploy

```
Scheduler         S3 (Logs)      Spark Cluster     Content Filter    Trie Builder      S3 (Artifacts)     Trie Servers
    │                │                │                  │                │                  │                 │
    │ 1. Hourly      │                │                  │                │                  │                 │
    │    cron trigger│                │                  │                │                  │                 │
    │                │                │                  │                │                  │                 │
    │ 2. Submit      │                │                  │                │                  │                 │
    │    Spark job   │                │                  │                │                  │                 │
    │───────────────────────────────▶│                  │                │                  │                 │
    │                │                │                  │                │                  │                 │
    │                │  3. Read last  │                  │                │                  │                 │
    │                │     30 days    │                  │                │                  │                 │
    │                │◀───────────────│                  │                │                  │                 │
    │                │  raw logs      │                  │                │                  │                 │
    │                │───────────────▶│                  │                │                  │                 │
    │                │                │                  │                │                  │                 │
    │                │                │ 4. Normalize     │                │                  │                 │
    │                │                │    GroupBy query  │                │                  │                 │
    │                │                │    Compute time-  │                │                  │                 │
    │                │                │    decay scores   │                │                  │                 │
    │                │                │    (~20 min)      │                │                  │                 │
    │                │                │                  │                │                  │                 │
    │                │                │ 5. Scored query  │                │                  │                 │
    │                │                │    list           │                │                  │                 │
    │                │                │─────────────────▶│                │                  │                 │
    │                │                │                  │                │                  │                 │
    │                │                │                  │ 6. Filter      │                  │                 │
    │                │                │                  │    blocked     │                  │                 │
    │                │                │                  │    terms       │                  │                 │
    │                │                │                  │    (~2 min)    │                  │                 │
    │                │                │                  │                │                  │                 │
    │                │                │                  │ 7. Filtered    │                  │                 │
    │                │                │                  │    query list  │                  │                 │
    │                │                │                  │───────────────▶│                  │                 │
    │                │                │                  │                │                  │                 │
    │                │                │                  │                │ 8. Sort alpha    │                 │
    │                │                │                  │                │ 9. Build trie    │                 │
    │                │                │                  │                │ 10. Propagate    │                 │
    │                │                │                  │                │     top-K        │                 │
    │                │                │                  │                │ 11. Serialize    │                 │
    │                │                │                  │                │     (~15 min)    │                 │
    │                │                │                  │                │                  │                 │
    │                │                │                  │                │ 12. Upload       │                 │
    │                │                │                  │                │     trie binary  │                 │
    │                │                │                  │                │─────────────────▶│                 │
    │                │                │                  │                │                  │                 │
    │                │                │                  │                │ 13. Validate     │                 │
    │                │                │                  │                │     (size,       │                 │
    │                │                │                  │                │     count,       │                 │
    │                │                │                  │                │     sample)      │                 │
    │                │                │                  │                │                  │                 │
    │                │                │                  │                │                  │ 14. Trie servers│
    │                │                │                  │                │                  │     pull v42    │
    │                │                │                  │                  │                 │◀────────────────│
    │                │                │                  │                │                  │                 │
    │                │                │                  │                │                  │  15. mmap +     │
    │                │                │                  │                │                  │      pre-touch  │
    │                │                │                  │                │                  │      (~40s)     │
    │                │                │                  │                │                  │                 │
    │                │                │                  │                │                  │  16. Health     │
    │                │                │                  │                │                  │      check OK   │
    │                │                │                  │                │                  │                 │
    │                │                │                  │                │                  │  17. LB switch  │
    │                │                │                  │                │                  │      traffic    │
    │                │                │                  │                │                  │      → v42      │
    │                │                │                  │                │                  │                 │
```

### Timing Breakdown

| Phase | Steps | Duration | Cumulative |
|---|---|---|---|
| Spark aggregation | 3-4 | ~20 min | 20 min |
| Content filtering | 5-6 | ~2 min | 22 min |
| Trie construction | 8-11 | ~15 min | 37 min |
| Upload + validate | 12-13 | ~1 min | 38 min |
| Server deploy | 14-17 | ~2 min | **40 min** |

**Total: ~40 minutes** on an hourly schedule → 20 min of buffer.

### Failure Points and Mitigations

| Step | Failure Mode | Mitigation |
|---|---|---|
| 3 | S3 read timeout | Retry with exponential backoff. 3 attempts. |
| 4 | Spark executor OOM | Increase executor memory. Repartition data. |
| 6 | Content filter service down | **HALT BUILD.** Never build without filtering. |
| 12 | S3 upload failure | Retry. If persistent, alert. Serve previous trie. |
| 13 | Validation failure | Abort deploy. Keep serving previous trie. |
| 15 | Server OOM during load | Server fails health check. LB routes around it. |

---

## 4. Real-Time Trending Injection Flow

### Kafka → Flink → Redis Trending Store

```
Search Events       Kafka              Flink                  DynamoDB              Redis
(from app)          (stream)           (processing)           (baseline)            (trending store)
    │                  │                    │                      │                     │
    │ 1. Query event   │                    │                      │                     │
    │ "super bowl"     │                    │                      │                     │
    │─────────────────▶│                    │                      │                     │
    │                  │                    │                      │                     │
    │                  │ 2. Consume event   │                      │                     │
    │                  │───────────────────▶│                      │                     │
    │                  │                    │                      │                     │
    │                  │                    │ 3. Add to 5-min      │                     │
    │                  │                    │    tumbling window    │                     │
    │                  │                    │    counter for        │                     │
    │                  │                    │    "super bowl"       │                     │
    │                  │                    │                      │                     │
    │  ... 5 minutes pass, window closes...│                      │                     │
    │                  │                    │                      │                     │
    │                  │                    │ 4. Window result:     │                     │
    │                  │                    │    "super bowl"       │                     │
    │                  │                    │    = 15,000 in 5 min  │                     │
    │                  │                    │                      │                     │
    │                  │                    │ 5. Fetch baseline     │                     │
    │                  │                    │    for this query     │                     │
    │                  │                    │    at this time       │                     │
    │                  │                    │───────────────────── ▶│                     │
    │                  │                    │                      │                     │
    │                  │                    │ 6. Baseline:          │                     │
    │                  │                    │    avg = 200 / 5 min  │                     │
    │                  │                    │◀──────────────────── │                     │
    │                  │                    │                      │                     │
    │                  │                    │ 7. Spike detection:   │                     │
    │                  │                    │    ratio = 15000/200  │                     │
    │                  │                    │    = 75x              │                     │
    │                  │                    │    > threshold (5x)   │                     │
    │                  │                    │    → TRENDING!        │                     │
    │                  │                    │    boost = min(75,10) │                     │
    │                  │                    │    = 10.0             │                     │
    │                  │                    │                      │                     │
    │                  │                    │ 8. Write to Redis     │                     │
    │                  │                    │    trending store     │                     │
    │                  │                    │──────────────────────────────────────────── ▶│
    │                  │                    │                      │                     │
    │                  │                    │                      │  ZADD trending:     │
    │                  │                    │                      │  queries            │
    │                  │                    │                      │  10.0               │
    │                  │                    │                      │  "super bowl"       │
    │                  │                    │                      │  TTL: 30 min        │
    │                  │                    │                      │                     │
```

### How Trending Results Are Served

When the autocomplete service handles a query, it merges trie results with trending:

```
Autocomplete Service    Trie Server      Redis (trending)     Redis (blocklist)
       │                    │                  │                     │
       │ 1. search("su")    │                  │                     │
       │───────────────────▶│                  │                     │
       │                    │                  │                     │
       │ 2. trie results:   │                  │                     │
       │    [summer dresses, │                  │                     │
       │     sunglasses,    │                  │                     │
       │     sunscreen, ...] │                  │                     │
       │◀───────────────────│                  │                     │
       │                    │                  │                     │
       │ 3. ZRANGEBYSCORE   │                  │                     │
       │    trending:queries│                  │                     │
       │    filter by       │                  │                     │
       │    prefix "su"     │                  │                     │
       │──────────────────────────────────────▶│                     │
       │                    │                  │                     │
       │ 4. trending match: │                  │                     │
       │    "super bowl"    │                  │                     │
       │    score: 10.0     │                  │                     │
       │◀──────────────────────────────────── │                     │
       │                    │                  │                     │
       │ 5. Merge results:  │                  │                     │
       │    "super bowl" gets│                 │                     │
       │    trending boost   │                 │                     │
       │    → insert at top  │                 │                     │
       │                    │                  │                     │
       │ 6. Content filter  │                  │                     │
       │    check           │                  │                     │
       │────────────────────────────────────────────────────────────▶│
       │                    │                  │                     │
       │ 7. All clean       │                  │                     │
       │◀───────────────────────────────────────────────────────────│
       │                    │                  │                     │
       │ 8. Return:         │                  │                     │
       │    [super bowl,    │                  │                     │
       │     summer dresses,│                  │                     │
       │     sunglasses,...] │                  │                     │
       │                    │                  │                     │
```

**Total additional latency from trending overlay: ~2-3ms** (one Redis ZRANGEBYSCORE call).

---

## 5. Cache Warm-Up Flow

### Post-Deployment Redis Cache Population

```
Deployment Svc     Trie Server (v42)    Redis              Popular Prefixes DB
      │                   │                │                       │
      │ 1. Trie v42       │                │                       │
      │    deployed OK     │                │                       │
      │                   │                │                       │
      │ 2. Fetch top      │                │                       │
      │    100K prefixes  │                │                       │
      │───────────────────────────────────────────────────────────▶│
      │                   │                │                       │
      │ 3. Popular        │                │                       │
      │    prefixes list  │                │                       │
      │◀──────────────────────────────────────────────────────────│
      │                   │                │                       │
      │ 4. Batch search   │                │                       │
      │    (1000 at a time)│               │                       │
      │──────────────────▶│                │                       │
      │                   │                │                       │
      │ 5. Results for    │                │                       │
      │    batch 1        │                │                       │
      │◀──────────────────│                │                       │
      │                   │                │                       │
      │ 6. Cache results  │                │                       │
      │    in Redis       │                │                       │
      │───────────────────────────────────▶│                       │
      │                   │                │                       │
      │                   │                │  MSET                 │
      │                   │                │  suggestions:v42:a    │
      │                   │                │  suggestions:v42:am   │
      │                   │                │  suggestions:v42:ama  │
      │                   │                │  suggestions:v42:i    │
      │                   │                │  ...                  │
      │                   │                │                       │
      │ 7. Repeat for     │                │                       │
      │    all batches    │                │                       │
      │    (100 batches,  │                │                       │
      │     100ms between) │               │                       │
      │                   │                │                       │
      │ Total: ~10 sec    │                │                       │
      │                   │                │                       │
```

### Timing

| Phase | Duration |
|---|---|
| Fetch popular prefixes | ~0.1s |
| 100 batch lookups (1000 each, 100ms spacing) | ~10s |
| Redis writes (pipelined) | Overlapped with lookups |
| **Total warm-up time** | **~10 seconds** |

After warm-up, ~80% of Redis cache hits are pre-populated, preventing a burst of traffic to trie servers.

---

## 6. Trie Deployment Flow (Blue-Green)

### Zero-Downtime Trie Version Swap

```
Deploy Orchestrator    LB (ALB)         BLUE Pool (v41)     GREEN Pool (idle)     S3
      │                   │                  │                    │                 │
      │ 1. New trie v42   │                  │                    │                 │
      │    ready in S3    │                  │                    │                 │
      │                   │                  │                    │                 │
      │ 2. Signal GREEN   │                  │                    │                 │
      │    pool to load v42│                 │                    │                 │
      │──────────────────────────────────────────────────────────▶│                 │
      │                   │                  │                    │                 │
      │                   │  Traffic ──────▶│                    │ 3. Download     │
      │                   │  (all to BLUE)   │                    │    trie_v42.bin │
      │                   │                  │                    │    from S3      │
      │                   │                  │                    │◀────────────────│
      │                   │                  │                    │                 │
      │                   │                  │                    │ 4. mmap + pre-  │
      │                   │                  │                    │    touch pages  │
      │                   │                  │                    │    (~40 seconds)│
      │                   │                  │                    │                 │
      │ 5. GREEN health   │                  │                    │                 │
      │    check          │                  │                    │                 │
      │──────────────────────────────────────────────────────────▶│                 │
      │                   │                  │                    │                 │
      │ 6. Health: OK     │                  │                    │                 │
      │    trie_version:  │                  │                    │                 │
      │    v42, loaded,   │                  │                    │                 │
      │    pre-faulted    │                  │                    │                 │
      │◀──────────────────────────────────────────────────────── │                 │
      │                   │                  │                    │                 │
      │ 7. Run warm-up    │                  │                    │                 │
      │    (cache Redis)  │                  │                    │                 │
      │    (~10 seconds)  │                  │                    │                 │
      │                   │                  │                    │                 │
      │ 8. Switch traffic │                  │                    │                 │
      │    BLUE → GREEN   │                  │                    │                 │
      │──────────────────▶│                  │                    │                 │
      │                   │                  │                    │                 │
      │                   │  Traffic ──────────────────────────▶│                 │
      │                   │  (all to GREEN)  │                    │                 │
      │                   │                  │                    │                 │
      │ 9. Drain BLUE     │                  │                    │                 │
      │    (wait for      │                  │                    │                 │
      │     in-flight     │  ◀── drain ───── │                    │                 │
      │     requests)     │                  │                    │                 │
      │                   │                  │                    │                 │
      │ 10. BLUE becomes  │                  │                    │                 │
      │     next GREEN    │                  │                    │                 │
      │     (idle, ready  │                  │                    │                 │
      │      for v43)     │                  │                    │                 │
      │                   │                  │                    │                 │
```

### Rollback Flow

If quality metrics degrade after switching to v42:

```
Deploy Orchestrator    LB              GREEN (v42 — active)    BLUE (v41 — standby)
      │                 │                    │                       │
      │ 1. Alert:       │                    │                       │
      │    CTR dropped  │                    │                       │
      │    22% in last  │                    │                       │
      │    10 minutes   │                    │                       │
      │                 │                    │                       │
      │ 2. ROLLBACK!    │                    │                       │
      │    Switch to    │                    │                       │
      │    BLUE (v41)   │                    │                       │
      │────────────────▶│                    │                       │
      │                 │                    │                       │
      │                 │  Traffic ─────────────────────────────────▶│
      │                 │  (back to BLUE)    │                       │
      │                 │                    │                       │
      │ 3. Investigate  │                    │                       │
      │    v42 issues   │                    │                       │
      │                 │                    │                       │

Rollback time: < 30 seconds (just a load balancer config change)
```

---

## 7. Content Filtering Flow

### Three-Level Defense

```
LEVEL 1: BUILD-TIME          LEVEL 2: SERVE-TIME           LEVEL 3: EMERGENCY
(during trie construction)    (during query response)        (admin-triggered)

┌────────────────────┐       ┌─────────────────────┐       ┌─────────────────────┐
│                    │       │                     │       │                     │
│ Query: "bad term"  │       │ Result includes     │       │ New offensive term  │
│                    │       │ "bad term"          │       │ discovered          │
│ 1. Check against   │       │                     │       │                     │
│    master blocklist│       │ 1. Check each result│       │ 1. Admin POSTs to   │
│    (500K terms)    │       │    against Redis    │       │    /admin/blocklist  │
│                    │       │    blocklist        │       │                     │
│ 2. Run ML          │       │    (fast: O(1))     │       │ 2. Term added to    │
│    classifier      │       │                     │       │    Redis blocklist   │
│    (score > 0.9    │       │ 2. Filter out       │       │    immediately       │
│     → block)       │       │    any matches      │       │                     │
│                    │       │                     │       │ 3. Next query for    │
│ 3. Excluded from   │       │ 3. Return clean     │       │    this prefix       │
│    trie entirely   │       │    results          │       │    → filtered        │
│                    │       │                     │       │                     │
│ Latency impact:    │       │ Latency impact:     │       │ Latency impact:     │
│ None (offline)     │       │ +0.5ms (Redis call) │       │ None (Redis update) │
│                    │       │                     │       │                     │
│ Coverage: 99%+     │       │ Coverage: catches   │       │ Time to effective:  │
│ of known bad terms │       │ any slip-throughs   │       │ < 30 seconds        │
└────────────────────┘       └─────────────────────┘       └─────────────────────┘
```

### Emergency Blocking Sequence

```
Trust & Safety       Admin API          Redis               Autocomplete Svc
      │                  │                 │                       │
      │ 1. Report:       │                 │                       │
      │    offensive     │                 │                       │
      │    term found    │                 │                       │
      │    in suggestions│                 │                       │
      │                  │                 │                       │
      │ 2. POST          │                 │                       │
      │    /admin/       │                 │                       │
      │    blocklist     │                 │                       │
      │    {action: ADD, │                 │                       │
      │     terms: [...]}│                 │                       │
      │─────────────────▶│                 │                       │
      │                  │                 │                       │
      │                  │ 3. SADD         │                       │
      │                  │    content:     │                       │
      │                  │    blocklist    │                       │
      │                  │    "bad term"   │                       │
      │                  │────────────────▶│                       │
      │                  │                 │                       │
      │ 4. 200 OK        │                 │                       │
      │    term blocked  │                 │                       │
      │◀─────────────────│                 │                       │
      │                  │                 │                       │
      │                  │                 │   (next query for     │
      │                  │                 │    matching prefix)    │
      │                  │                 │                       │
      │                  │                 │ 5. Serve-time filter  │
      │                  │                 │    catches "bad term" │
      │                  │                 │◀──────────────────────│
      │                  │                 │                       │
      │                  │                 │ 6. Term excluded from │
      │                  │                 │    response           │
      │                  │                 │───────────────────── ▶│
      │                  │                 │                       │
      │                  │   Also:         │                       │
      │                  │   Invalidate    │                       │
      │                  │   cached results│                       │
      │                  │   for affected  │                       │
      │                  │   prefixes      │                       │
      │                  │                 │                       │

Time from report to effective: < 30 seconds
Time until trie rebuilt without the term: next hourly cycle
```

---

## 8. Personalization Overlay Flow

### Merging Global Suggestions with User History

```
Autocomplete Svc       Trie Server        Redis (user profile)    Redis (blocklist)
      │                     │                    │                       │
      │ 1. Request:         │                    │                       │
      │    prefix="ip"      │                    │                       │
      │    user_id="u123"   │                    │                       │
      │                     │                    │                       │
      │ 2. Trie search      │                    │                       │
      │    (parallel with 3)│                    │                       │
      │────────────────────▶│                    │                       │
      │                     │                    │                       │
      │ 3. User profile     │                    │                       │
      │    lookup (parallel │                    │                       │
      │    with 2)          │                    │                       │
      │──────────────────────────────────────── ▶│                       │
      │                     │                    │                       │
      │ 4. Trie results:    │                    │                       │
      │    [iphone 15,      │                    │                       │
      │     ipad,           │                    │                       │
      │     iphone case,    │                    │                       │
      │     iphone charger, │                    │                       │
      │     ...]            │                    │                       │
      │◀────────────────────│                    │                       │
      │                     │                    │                       │
      │ 5. User profile:    │                    │                       │
      │    recent: [        │                    │                       │
      │      "ipad pro",    │                    │                       │
      │      "iphone 15     │                    │                       │
      │       pro max",     │                    │                       │
      │      "airpods"      │                    │                       │
      │    ]                │                    │                       │
      │    categories:      │                    │                       │
      │    [Electronics]    │                    │                       │
      │◀─────────────────────────────────────── │                       │
      │                     │                    │                       │
      │ 6. Re-rank:         │                    │                       │
      │    "iphone 15 pro   │                    │                       │
      │     max" → boost    │                    │                       │
      │    (matches user's  │                    │                       │
      │     recent search)  │                    │                       │
      │                     │                    │                       │
      │    "ipad pro" →     │                    │                       │
      │     boost (recent   │                    │                       │
      │     search match)   │                    │                       │
      │                     │                    │                       │
      │    Final ranking:   │                    │                       │
      │    [iphone 15 pro   │                    │                       │
      │     max,            │                    │                       │
      │     ipad pro,       │                    │                       │
      │     iphone 15,      │                    │                       │
      │     ipad,           │                    │                       │
      │     iphone case,...]│                    │                       │
      │                     │                    │                       │
      │ 7. Content filter   │                    │                       │
      │────────────────────────────────────────────────────────────────▶│
      │ 8. Clean            │                    │                       │
      │◀───────────────────────────────────────────────────────────────│
      │                     │                    │                       │
      │ 9. Return           │                    │                       │
      │    personalized     │                    │                       │
      │    results          │                    │                       │
      │                     │                    │                       │
```

**Key**: Steps 2 and 3 are **parallel** — trie lookup and user profile fetch happen simultaneously. This keeps latency the same as a non-personalized request (~5ms for both, whichever is slower).

### Latency Impact of Personalization

| Component | Without Personalization | With Personalization |
|---|---|---|
| Trie lookup | ~1ms | ~1ms (unchanged) |
| User profile fetch | N/A | ~2ms (Redis) — **parallel with trie** |
| Re-ranking | N/A | ~0.1ms (in-memory sort of 10 items) |
| **Total** | **~1ms** | **~2ms** (bounded by slower parallel call) |

Personalization adds only ~1ms of latency — well within budget.

---

## 9. Failure / Fallback Flow

### Cascading Fallback Strategy

```
                         NORMAL OPERATION
                    ┌─────────────────────┐
                    │ Trie Server (v42)   │◀── Primary source
                    │ + Trending overlay  │
                    │ + Personalization   │
                    └────────┬────────────┘
                             │ FAILS (health check, timeout, error)
                             ▼
                    ┌─────────────────────┐
                    │ Redis Cache         │◀── Fallback 1: stale but fast
                    │ (may be stale by    │
                    │  up to 10 min)      │
                    └────────┬────────────┘
                             │ FAILS (Redis down, cache miss)
                             ▼
                    ┌─────────────────────┐
                    │ CDN Cache           │◀── Fallback 2: staler but available
                    │ (may be stale by    │
                    │  up to 5 min)       │
                    └────────┬────────────┘
                             │ FAILS (CDN miss for this prefix)
                             ▼
                    ┌─────────────────────┐
                    │ Empty Suggestions   │◀── Fallback 3: graceful degradation
                    │ (search bar works,  │
                    │  just no dropdown)  │
                    └─────────────────────┘
```

### Circuit Breaker on Trie Server

```
Autocomplete Svc                Circuit Breaker                Trie Server
      │                              │                             │
      │ 1. Request                   │                             │
      │─────────────────────────────▶│                             │
      │                              │                             │
      │                              │ State: CLOSED (normal)      │
      │                              │─────────────────────────── ▶│
      │                              │                             │
      │                              │ 2. Timeout (50ms)           │
      │                              │◀────────── X ──────────────│
      │                              │                             │
      │                              │ failures++ (now 3/5)        │
      │                              │                             │
      │ 3. Return: fallback          │                             │
      │    (Redis cached result)     │                             │
      │◀─────────────────────────────│                             │
      │                              │                             │
      │  ... more failures ...       │                             │
      │                              │                             │
      │                              │ failures = 5/5 in 10s       │
      │                              │ State: OPEN                 │
      │                              │                             │
      │ 4. Request                   │                             │
      │─────────────────────────────▶│                             │
      │                              │                             │
      │                              │ State: OPEN                 │
      │                              │ → Don't even try trie       │
      │                              │   server                    │
      │                              │                             │
      │ 5. Return: fallback          │                             │
      │    immediately               │                             │
      │◀─────────────────────────────│                             │
      │                              │                             │
      │  ... 30 seconds pass ...     │                             │
      │                              │                             │
      │                              │ State: HALF-OPEN            │
      │                              │ (allow 1 test request)      │
      │                              │                             │
      │ 6. Request                   │                             │
      │─────────────────────────────▶│─────────────────────────── ▶│
      │                              │                             │
      │                              │ 7. Success!                 │
      │                              │◀────────────────────────── │
      │                              │                             │
      │                              │ State: CLOSED (normal)      │
      │                              │                             │
      │ 8. Return: trie result       │                             │
      │◀─────────────────────────────│                             │
      │                              │                             │
```

### Circuit Breaker Configuration

| Parameter | Value | Rationale |
|---|---|---|
| Failure threshold | 5 failures in 10 seconds | Detect sustained outage, not transient blip |
| Open duration | 30 seconds | Give trie server time to recover |
| Timeout per request | 50ms | Autocomplete must be fast or not at all |
| Fallback | Redis cache → empty results | Always return something |

---

## 10. Trie Shard Rebalancing Flow

### Adding a New Shard (Future Growth Scenario)

This flow applies when the trie grows beyond single-server memory and we need to split shards.

```
Orchestrator        Shard Registry       Old Shard (a-m)     New Shard 1 (a-f)    New Shard 2 (g-m)
      │                   │                    │                    │                    │
      │ 1. Initiate       │                    │                    │                    │
      │    shard split:   │                    │                    │                    │
      │    "a-m" → "a-f"  │                    │                    │                    │
      │    + "g-m"        │                    │                    │                    │
      │                   │                    │                    │                    │
      │ 2. Build sub-tries│                    │                    │                    │
      │    for new ranges │                    │                    │                    │
      │──────────────────────────────────────────────────────────▶│                    │
      │───────────────────────────────────────────────────────────────────────────────▶│
      │                   │                    │                    │                    │
      │                   │                    │                    │ 3. Load trie       │
      │                   │                    │                    │    (prefixes a-f)  │
      │                   │                    │                    │                    │
      │                   │                    │                    │                    │ 3. Load trie
      │                   │                    │                    │                    │    (prefixes g-m)
      │                   │                    │                    │                    │
      │ 4. Health checks  │                    │                    │                    │
      │    pass on both   │                    │                    │                    │
      │                   │                    │                    │                    │
      │ 5. Update routing │                    │                    │                    │
      │    table           │                    │                    │                    │
      │──────────────────▶│                    │                    │                    │
      │                   │                    │                    │                    │
      │                   │ prefix "a"-"f" → │                    │                    │
      │                   │   New Shard 1     │                    │                    │
      │                   │ prefix "g"-"m" → │                    │                    │
      │                   │   New Shard 2     │                    │                    │
      │                   │                    │                    │                    │
      │ 6. Switch routing │                    │                    │                    │
      │    (atomic swap)  │                    │                    │                    │
      │                   │                    │                    │                    │
      │ 7. Drain and      │                    │                    │                    │
      │    decommission   │                    │                    │                    │
      │    old shard      │  ◀── drain ──── │                    │                    │
      │                   │                    │                    │                    │
```

---

## Flow Summary Table

| # | Flow | Trigger | Sync/Async | Hot Path? | Typical Latency |
|---|---|---|---|---|---|
| 1 | Query (CDN hit) | User keystroke | Sync | ✅ Yes | ~5-10ms |
| 2 | Query (full path) | User keystroke (cache miss) | Sync | ✅ Yes | ~30-50ms |
| 3 | Trie Build | Hourly cron | Async (offline) | ❌ No | ~40 min |
| 4 | Trending Injection | Kafka events (continuous) | Async (streaming) | ❌ No | ~5 min windows |
| 5 | Cache Warm-Up | Post-trie deploy | Async (background) | ❌ No | ~10 sec |
| 6 | Blue-Green Deploy | Trie build complete | Async (operational) | ❌ No | ~2 min |
| 7 | Content Filtering | Build-time + serve-time | Mixed | ⚠️ Filter check is hot | +0.5ms (serve-time) |
| 8 | Personalization | Query time (if user_id present) | Sync (piggyback) | ✅ Yes (if enabled) | +1ms |
| 9 | Failure/Fallback | Server failure, timeout | Sync (fallback) | ⚠️ Emergency path | Varies |
| 10 | Shard Rebalancing | Capacity growth trigger | Async (operational) | ❌ No | ~30 min |

---

*This document complements the [Interview Simulation](interview-simulation.md) and references the [Trie & Ranking Deep Dive](trie-and-ranking-deep-dive.md), [Data Collection Pipeline](data-collection-pipeline.md), and [Scaling & Caching](scaling-and-caching.md) documents.*
