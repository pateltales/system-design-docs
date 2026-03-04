# Scaling & Performance — Cross-Cutting Concerns

> Every system works at small scale. The art is making it work at Instagram scale:
> 2B+ MAU, 100M+ uploads/day, 500M+ Stories users, 4B+ likes/day.
> This doc ties together scaling decisions from all deep dives.

---

## Table of Contents

1. [Scale Numbers](#1-scale-numbers)
2. [Read vs Write Asymmetry](#2-read-vs-write-asymmetry)
3. [Database Sharding Strategy](#3-database-sharding-strategy)
4. [Multi-Layer Caching](#4-multi-layer-caching)
5. [Feed Latency Budget](#5-feed-latency-budget)
6. [Handling Viral Content](#6-handling-viral-content)
7. [Multi-Datacenter / Multi-Region](#7-multi-datacenter--multi-region)
8. [Contrasts](#8-contrasts)

---

## 1. Scale Numbers

**VERIFIED** numbers are from official Meta/Instagram announcements. **INFERRED** numbers are derived from public data and engineering posts.

| Metric | Value | Confidence |
|---|---|---|
| **Monthly Active Users (MAU)** | 2B+ | HIGH (Meta official, 2023-2024) |
| **Daily Active Users (DAU)** | ~500M-700M | MEDIUM (inferred from MAU) |
| **Photos/videos uploaded per day** | ~100M+ | MEDIUM (from 2016 data, likely higher now) |
| **Stories daily active users** | 500M+ | HIGH (Instagram official, Jan 2019) |
| **Stories created per day** | ~500M+ | INFERRED (from DAU × participation rate) |
| **Likes per day** | ~4.2B | LOW-MEDIUM (third-party estimates) |
| **Comments per day** | ~hundreds of millions | INFERRED |
| **Feed loads per day** | Billions | INFERRED (multiple loads per DAU) |
| **Media storage (total)** | Hundreds of petabytes | INFERRED (from 260B images in Haystack as of 2014) |
| **Max followers (single account)** | ~650M+ (Cristiano Ronaldo) | HIGH (public data) |
| **Following limit (per user)** | 7,500 | HIGH (Instagram Help Center) |
| **Average following count** | ~200 | INFERRED |
| **Cache operations (Meta-wide)** | Trillions/day | HIGH (Meta Memcache paper, NSDI 2013) |

### Derived Numbers (Back-of-Envelope)

**Uploads per second:**
```
100M uploads/day ÷ 86,400 seconds = ~1,157 uploads/sec (sustained)
Peak: ~3-5x average = ~3,500-6,000 uploads/sec
```

**Feed fan-out writes per second:**
```
100M posts/day × avg 200 followers = 20B fan-out writes/day
20B ÷ 86,400 = ~230K fan-out writes/sec (sustained)
Peak: ~500K-1M writes/sec
```

**Image downloads per second:**
```
Assume 1B feed loads/day × 10 images per feed page = 10B image downloads/day
10B ÷ 86,400 = ~115K image downloads/sec
Peak: 300K+ downloads/sec
(Most served from CDN edge — only 5-10% reach origin)
```

**Total media storage growth per day:**
```
100M uploads × 4 resolutions × avg 100KB per variant = ~40TB new data/day
Plus video (larger, but fewer): adds ~20-40TB/day
Total: ~60-80TB new media storage per day
```

---

## 2. Read vs Write Asymmetry

Instagram is **extremely read-heavy**. A single post is written once but read thousands to millions of times.

### Read:Write Ratios by Data Type

| Data | Write | Read | Ratio | Implication |
|---|---|---|---|---|
| **Post media** | 1 upload | Viewed in feeds, profiles, Explore, search | 1:100,000+ (popular) | Aggressive CDN caching, multiple pre-generated resolutions |
| **Feed inbox** | 1 write per follower | 1 read per feed load | 1:10 to 1:100 | Fan-out on write justified — expensive write → cheap reads |
| **Social graph (follows)** | 1 follow/unfollow | Checked on every profile visit, feed assembly, recommendation | 1:1,000+ | Two-tier TAO cache absorbs reads |
| **Like/comment counts** | 1 per engagement | Displayed on every post view | 1:1,000+ | Denormalized counters, approximate counting OK |
| **User profiles** | Updated rarely | Viewed on every post, comment, follow | 1:10,000+ | Cache aggressively, short TTL for freshness |
| **Stories metadata** | 1 create | Viewed by followers (tray + playback) | 1:100 to 1:10,000 | TTL-based caching, ephemeral storage |
| **Search index** | Updated on new content | Every keystroke in search bar | 1:100+ | In-memory prefix index at edge |

### Why This Matters

The extreme read:write ratio justifies several architectural decisions:

1. **Fan-out on write**: Pay the write cost once (O(followers)) so every read is O(1)
2. **Multi-layer caching**: Cache everything — the cost of a cache miss is hit thousands of times
3. **CDN edge caching**: Hot content cached at 100+ PoPs globally
4. **Denormalized counters**: Store like_count directly on the post — don't COUNT() on every read
5. **Pre-computed rankings**: Explore top posts for hashtags pre-computed, not calculated per query

---

## 3. Database Sharding Strategy

### Sharding by User ID

Most Instagram data is sharded by userId because the dominant access pattern is **user-centric**: "Show me this user's feed," "Show me this user's profile," "Show me this user's posts."

```
┌──────────────────────────────────────────────────────────────┐
│ User-ID-Based Sharding                                       │
│                                                              │
│ Shard = hash(userId) % num_shards                            │
│                                                              │
│ ┌─────────────┐  ┌─────────────┐  ┌─────────────┐           │
│ │ Shard 0     │  │ Shard 1     │  │ Shard N     │           │
│ │ Users 0-999 │  │ Users       │  │ Users       │           │
│ │             │  │ 1000-1999   │  │ N000-N999   │           │
│ │ - Profile   │  │ - Profile   │  │ - Profile   │           │
│ │ - Posts     │  │ - Posts     │  │ - Posts     │           │
│ │ - Feed inbox│  │ - Feed inbox│  │ - Feed inbox│           │
│ │ - Following │  │ - Following │  │ - Following │           │
│ │ - Activity  │  │ - Activity  │  │ - Activity  │           │
│ └─────────────┘  └─────────────┘  └─────────────┘           │
│                                                              │
│ Single-shard queries (fast):                                 │
│   • GET /users/{userId}                                      │
│   • GET /feed (user's feed inbox)                            │
│   • GET /users/{userId}/posts                                │
│   • GET /users/{userId}/following                            │
│                                                              │
│ Cross-shard queries (expensive, avoid):                      │
│   • "Show all posts with hashtag #travel"                    │
│   • "Show trending posts globally"                           │
│   • "Compute mutual followers between users on diff shards"  │
└──────────────────────────────────────────────────────────────┘
```

### TAO Sharding (Social Graph)

TAO shards by `id1` (source object):
- `(user_42, FOLLOWS, user_99)` stored on shard for user_42
- All of user_42's outgoing relationships on one shard
- Reverse associations stored redundantly: `(user_99, FOLLOWED_BY, user_42)` on shard for user_99
- This redundancy enables both "who does X follow?" and "who follows X?" as single-shard queries

### The Hot Shard Problem

Power-law follower distribution creates hot shards:

```
Problem:
  Celebrity account (650M followers) → their FOLLOWED_BY list is enormous
  Any operation touching this data hits one shard disproportionately

Mitigations:

1. Denormalized counters
   Don't: COUNT(FOLLOWED_BY(celebrity))  → scans 650M entries
   Do:    READ follower_count(celebrity)  → single cached value

2. Paginated access only
   Never load full follower list — always LIMIT + cursor

3. Cache replication
   Multiple cache replicas for hot data (TAO L1 leaf caches)
   Many L1 caches → read load distributed across replicas

4. Virtual sharding
   Sub-shard extremely hot data within a physical shard
   Celebrity's data gets its own sub-partition

5. Fan-out on read for celebrities
   Avoid writing to 650M feed inboxes (would create hot write shard)
   Instead, merge celebrity posts at read time
```

### Post-Based Sharding (Secondary)

Post metadata can also be sharded by postId for direct lookups:
- `GET /posts/{postId}` → single shard lookup by hash(postId)
- But `GET /users/{userId}/posts` requires a cross-shard scatter-gather unless an index of `userId → [postIds]` is maintained on the user's shard

**Solution:** Store a post index on the user's shard (userId → list of postIds) and full post data on the post's shard. Two lookups: user shard for list → post shards for data. The second step is parallelized.

---

## 4. Multi-Layer Caching

Instagram's caching strategy has four layers, each serving a different purpose.

```
┌────────────────────────────────────────────────────────────────────┐
│ Layer 1: Client-Side Cache (On-Device)                             │
│                                                                    │
│ What: Feed data, profile data, image data                          │
│ Storage: App memory + device disk (iOS/Android)                    │
│ Hit rate: Very high for recently viewed content                    │
│ Eviction: LRU based on device storage limits                       │
│ Benefit: Zero network calls on app re-open (show stale, refresh    │
│          in background)                                            │
│ Size: ~50-200MB per device                                         │
├────────────────────────────────────────────────────────────────────┤
│ Layer 2: CDN Edge Cache (L1 PoPs)                                  │
│                                                                    │
│ What: Media files (images, video segments, thumbnails)             │
│ Storage: SSD/memory at edge PoPs worldwide                         │
│ Hit rate: >90% for hot content (recent posts from popular accounts)│
│ TTL: Hours to days depending on content popularity                 │
│ Benefit: Sub-50ms media delivery to nearby users                   │
│ Size: Many TBs per PoP                                             │
├────────────────────────────────────────────────────────────────────┤
│ Layer 3: Application Cache (Memcache + Redis)                      │
│                                                                    │
│ What:                                                              │
│   Memcache: User profiles, post metadata, social graph edges,     │
│             feature flags, configuration                           │
│   Redis: Feed inboxes (sorted sets), real-time counters,          │
│          rate limiting, Stories seen-state                          │
│                                                                    │
│ Hit rate: ~99% for hot data (TAO's L1 cache)                       │
│ Latency: <1ms for cache hit                                        │
│ Scale: Thousands of cache servers, trillions of ops/day            │
│ Benefit: Absorbs 99%+ of read load before hitting databases        │
│                                                                    │
│ Memory budget (feed inboxes):                                      │
│   700M DAU × 500 posts × 40 bytes per entry = ~14TB Redis         │
│   (spread across thousands of Redis instances)                     │
├────────────────────────────────────────────────────────────────────┤
│ Layer 4: Database (MySQL/TAO, Cassandra)                           │
│                                                                    │
│ What: Source of truth — all persistent data                        │
│ Hit rate: Only ~1-5% of requests reach this layer                  │
│ Latency: 5-50ms depending on query                                │
│ Benefit: Durability, consistency, recovery                         │
│ Scale: Thousands of sharded database instances                     │
└────────────────────────────────────────────────────────────────────┘
```

### Memcache at Meta Scale

**VERIFIED — from "Scaling Memcache at Facebook" USENIX NSDI 2013**

Key techniques for operating Memcache at trillions of ops/day:

| Technique | Purpose |
|---|---|
| **Lease mechanism** | Prevents thundering herd on cache miss — only one request fetches from DB, others wait for the lease holder to populate cache |
| **mcrouter** | Proxy layer that handles routing, connection pooling, and failover across Memcache clusters |
| **Gutter servers** | Dedicated pool that absorbs traffic when a Memcache server fails — prevents cascading to database |
| **Invalidation pipeline (mcsqueal)** | Tails MySQL binlog → publishes invalidation events → Memcache deletes stale entries. Ensures cache consistency on writes |
| **Regional pools** | Replicate hot data across regional Memcache pools — reads stay local, writes propagate async |
| **UDP for gets** | Use UDP for cache reads (stateless, lower overhead) and TCP for writes (need reliability) |

### Redis for Feed Inboxes

```
Feed inbox structure:
  Key:    feed:{userId}
  Type:   Sorted Set
  Members: postId (string)
  Score:   timestamp (float — seconds since epoch)

Operations:
  ZADD   feed:{userId} {timestamp} {postId}   → O(log N) — fan-out write
  ZREVRANGE feed:{userId} 0 19                 → O(log N + 20) — fetch top 20
  ZREM   feed:{userId} {postId}                → O(log N) — delete on unfollow/post deletion
  ZCARD  feed:{userId}                         → O(1) — inbox size

  Trim: ZREMRANGEBYRANK feed:{userId} 0 -(MAX_SIZE+1)
        → Keep only the latest MAX_SIZE posts (e.g., 500)
```

---

## 5. Feed Latency Budget

Target: **user opens app → feed rendered in <500ms**

```
Component Breakdown:
                                                        Cumulative
DNS resolution                                    ~20ms      20ms
TLS handshake (reuse connection)                   ~0ms      20ms
  (MQTT persistent connection already established)

API request: GET /feed                            ~50ms      70ms
  ├── Parse request, auth check                    ~5ms
  ├── Fetch feed inbox from Redis                 ~10ms
  │   (ZREVRANGE, ~500 candidate postIds)
  ├── Fetch celebrity posts (fan-out on read)      ~15ms
  │   (parallel lookups for ~5-20 celebrity accounts)
  ├── ML ranking (score ~500 candidates)           ~10ms
  │   (lightweight model, GPU-accelerated)
  └── Assemble response (top 20 posts + metadata)  ~10ms
      (parallel Memcache lookups for post metadata,
       author profiles, engagement counts)

Client receives feed JSON                         ~10ms      80ms
  (compressed, ~50-100KB for 20 posts)

First image download from CDN                    ~100ms     180ms
  (edge PoP, <50ms network + 50ms TLS/transfer)
  (blurhash placeholder shown immediately)

Remaining images download (parallel)             ~200ms     380ms
  (browser/app downloads 3-5 images in parallel)

Render and display                                ~50ms     430ms

Total: ~430ms (within 500ms budget)
```

### Latency Optimization Techniques

| Technique | Savings | Description |
|---|---|---|
| **Persistent connections** | ~100ms | MQTT connection already established — no new TCP/TLS handshake per request |
| **Feed prefetching** | ~200ms | App prefetches next page of feed while user views current page |
| **Blurhash placeholders** | Perceived ~300ms | Show colored blur immediately — user perceives the feed as "loaded" before images finish downloading |
| **Parallel image downloads** | ~60% of serial | Download multiple images simultaneously (HTTP/2 multiplexing) |
| **Cache-first rendering** | ~100ms | Show cached feed on app open, refresh in background |
| **Compressed responses** | ~40% bandwidth | gzip/brotli API response compression |
| **Regional routing** | ~50ms | DNS routes to nearest API server (Meta edge PoP) |

---

## 6. Handling Viral Content

When content goes viral, three systems experience extreme load simultaneously:

### 1. CDN Thundering Herd

```
Problem:
  Viral post → millions of users view the same image/video within minutes
  If content isn't cached at a CDN edge → all requests hit origin simultaneously

Solution:
  ┌───────────────────────────────────────────────────────┐
  │ Request Coalescing at CDN Edge                         │
  │                                                        │
  │ When 1000 requests arrive for the same uncached URL:   │
  │ • First request: fetch from origin (cache miss)        │
  │ • Requests 2-1000: hold/queue (don't forward to origin)│
  │ • When first request returns: serve all 1000 from cache │
  │                                                        │
  │ Result: 1 origin request instead of 1000               │
  └───────────────────────────────────────────────────────┘

Additional mitigations:
  • Popular content detected early → proactively warm CDN edge caches
  • Multi-tier cache (L1 edge → L2 regional) — even L1 misses hit L2 before origin
  • Video: HLS segments are individually cacheable — first segment cached quickly,
    subsequent segments cache as they're requested
```

### 2. Notification Storm

```
Problem:
  Celebrity post → 1M likes in 5 minutes → 1M individual notifications → phone explodes

Solution (Aggregation — see 10-notifications-and-real-time.md):
  • Time-window batching (30-second windows)
  • "user1, user2, and 999,998 others liked your post"
  • Max 1 push notification per post per 30-second window
  • In-app counter update via MQTT (lightweight, no push)
```

### 3. Counter Hotspot

```
Problem:
  Like count for a viral post receiving 10,000 likes/second
  Naive: UPDATE posts SET like_count = like_count + 1 WHERE id = ?
  → Row-level lock contention → database bottleneck

Solution: Approximate Counting
  ┌───────────────────────────────────────────────────────┐
  │ Write Path (high throughput):                          │
  │                                                        │
  │ 1. Like event → Redis INCR on sharded counter         │
  │    Key: like_count:{postId}:{shard_N}                  │
  │    (multiple shards to avoid single-key contention)    │
  │                                                        │
  │ 2. Periodic flush: every 5 seconds, sum all shards    │
  │    and update the authoritative count in database      │
  │                                                        │
  │ Read Path (approximate, fast):                         │
  │                                                        │
  │ 1. Read from Memcache (denormalized like_count)        │
  │ 2. May lag behind by up to 5 seconds                   │
  │ 3. For display: "1,234,567 likes" — user can't tell    │
  │    if it's 1,234,567 or 1,234,589                      │
  └───────────────────────────────────────────────────────┘

  Why this works:
  • A like count off by 0.1% is imperceptible to humans
  • "1.2M likes" vs "1,200,001 likes" — same user experience
  • Sharded counters handle 100K+ increments/sec
  • No serialization bottleneck
```

### View Counts (HyperLogLog)

For content where unique view counts matter (Reels, Stories):

```
Problem:
  Count unique viewers of a Reel that 10M people watch
  Storing 10M userIds in a set = ~80MB per Reel
  Not feasible for millions of Reels

Solution: HyperLogLog (probabilistic unique counting)
  • 12KB of memory per counter (regardless of count magnitude)
  • ~0.81% standard error
  • PFADD viewcount:{reelId} {userId}  → O(1)
  • PFCOUNT viewcount:{reelId}         → O(1), returns approximate count
  • For "1.2M views" — ±0.81% = ±9,720 — completely invisible to users
```

---

## 7. Multi-Datacenter / Multi-Region

**VERIFIED — Meta operates multiple data centers across regions. TAO paper and Memcache paper describe multi-region architecture.**

Instagram runs in an active-active multi-datacenter configuration.

### Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                          Global DNS                               │
│                                                                   │
│  Routes users to the nearest data center based on latency         │
│  Failover: if DC1 is down, DNS reroutes to DC2                   │
└──────────┬───────────────────────────┬───────────────────────────┘
           │                           │
    ┌──────▼──────┐             ┌──────▼──────┐
    │  US-East DC │             │  US-West DC │
    │  (Leader)   │             │  (Follower) │
    │             │             │             │
    │ ┌─────────┐ │             │ ┌─────────┐ │
    │ │ App     │ │             │ │ App     │ │
    │ │ Servers │ │             │ │ Servers │ │
    │ └────┬────┘ │             │ └────┬────┘ │
    │      │      │             │      │      │
    │ ┌────▼────┐ │             │ ┌────▼────┐ │
    │ │ Cache   │ │             │ │ Cache   │ │
    │ │ (L1+L2) │ │             │ │ (L1+L2) │ │
    │ └────┬────┘ │             │ └────┬────┘ │
    │      │      │             │      │      │
    │ ┌────▼────┐ │  async      │ ┌────▼────┐ │
    │ │ MySQL   │ │──repl──────>│ │ MySQL   │ │
    │ │ (Master)│ │             │ │ (Replica)│ │
    │ └─────────┘ │             │ └─────────┘ │
    │             │             │             │
    │ ┌─────────┐ │  async      │ ┌─────────┐ │
    │ │Cassandra│ │──repl──────>│ │Cassandra│ │
    │ │ (Ring)  │ │             │ │ (Ring)  │ │
    │ └─────────┘ │             │ └─────────┘ │
    └─────────────┘             └─────────────┘
```

### Replication per Storage System

| Storage | Replication Model | Cross-DC Behavior |
|---|---|---|
| **TAO (MySQL)** | Leader-follower | One region is leader (MySQL master). Follower regions have read replicas. Writes forwarded to leader. After commit, leader sends cache invalidation to followers. |
| **Cassandra** | Multi-master | All DCs can write. Quorum-based consistency (LOCAL_QUORUM for fast local reads, EACH_QUORUM for cross-DC consistency when needed). |
| **Redis** | Primary-replica | Per-DC Redis clusters. Feed inboxes replicated asynchronously. Fan-out writes happen in each DC independently. |
| **Memcache** | Regional pools with invalidation | Each DC has independent Memcache pools. Invalidation events propagated cross-DC via mcsqueal pipeline. |
| **Haystack/f4** | Replicated blobs | Media stored in 3+ replicas across DCs. Read from nearest DC with available replica. |

### Consistency Trade-offs

```
Strong consistency required for:
  • Follow/unfollow (graph mutation — must not lose edges)
  • Account creation/deletion
  • Post creation/deletion (must not show deleted posts)
  • Payment/monetization data

  Implementation: Write to leader DC → acknowledge → async replicate

Eventual consistency acceptable for:
  • Feed inboxes (a post appearing 1-2s late is fine)
  • Like/comment counts (off by a few is imperceptible)
  • Stories seen-state (gray ring delayed by seconds is OK)
  • Search index (new post searchable within minutes is fine)
  • Notification delivery (1-5s delay is acceptable)

  Implementation: Write locally → async replicate → converge within seconds
```

### Failure Handling

```
Scenario: US-East DC fails

1. Global DNS detects failure (health checks fail)
   → Reroutes all traffic to US-West

2. US-West has:
   • Full Cassandra ring (independent multi-master)
   • MySQL read replicas → promoted to master
   • Memcache warmed with recent data
   • CDN edge PoPs unaffected (they're at IXPs, not in DCs)

3. Impact:
   • Very recent writes (last few seconds) on US-East may be lost
     (async replication lag window)
   • Cache hit rate temporarily drops (US-West cache doesn't have
     all US-East users' hot data)
   • Cache warms up within minutes as traffic drives fills

4. Recovery:
   • When US-East comes back, resync from US-West
   • Gradually shift traffic back (DNS weight change)
```

---

## 8. Contrasts

### Instagram vs Netflix — Scaling Challenges

| Dimension | Instagram | Netflix |
|---|---|---|
| **Primary bottleneck** | Fan-out writes, metadata lookups, small media serves at extreme QPS | CDN bandwidth (long video streaming is bandwidth-heavy) |
| **QPS shape** | Billions of small requests (API calls, image downloads) | Millions of large, long-running streams |
| **Per-request cost** | Low (50-300KB image, 1-5KB API response) | High (GB-scale video streams per session) |
| **Content catalog** | Billions of items (every photo ever posted) | Tens of thousands (curated) |
| **Scaling dimension** | Metadata throughput + cache efficiency | Network bandwidth + storage volume |
| **CDN strategy** | Reactive caching (UGC volume too large to push proactively) | Proactive push (curated catalog fits on edge appliances) |
| **Database challenge** | Social graph at 100B+ edges, feed assembly for 2B users | Viewing history, recommendation model serving |
| **Write amplification** | Extreme (fan-out: 1 post → N follower writes) | Low (1 upload → 1 store, encoding, CDN push) |

**Key insight:** Netflix's scaling challenge is in the bandwidth plane — delivering HD/4K video streams to millions of concurrent viewers. Instagram's scaling challenge is in the metadata plane — assembling personalized feeds from a massive social graph, with high fan-out write amplification. Both are hard, but they stress different system components.

### Instagram vs Twitter — Scaling Challenges

| Dimension | Instagram | Twitter |
|---|---|---|
| **Fan-out problem** | Same (hybrid fan-out for celebrities) | Same (hybrid fan-out for celebrities) |
| **Media payload** | Heavy (50-300KB images per feed item) | Light (tweets are mostly text, <1KB) |
| **CDN load** | Dominant (images/videos are the product) | Secondary (media is optional, most tweets are text) |
| **Feed complexity** | Social-graph feed + recommendation feeds (Reels, Explore) | Social-graph feed + algorithmic For You feed |
| **Content types** | Photos, videos, Stories, Reels, carousels | Tweets, threads, Spaces (audio) |
| **Processing pipeline** | Complex (resize, transcode, filters, thumbnails) | Simple (mostly text validation, optional image resize) |
| **Write amplification** | Higher per post (media processing + fan-out) | Lower per tweet (text + fan-out) |

**Key insight:** Twitter and Instagram face the same fan-out challenge but differ in payload weight. Twitter's bottleneck is timeline assembly speed (text is cheap to store/transfer but fan-out is expensive). Instagram's bottleneck is both fan-out AND media delivery (images/videos are expensive to store, process, and serve). Instagram needs to solve two hard problems simultaneously; Twitter primarily needs to solve one.

### Instagram vs TikTok — Scaling Challenges

| Dimension | Instagram | TikTok |
|---|---|---|
| **Fan-out** | Required (social-graph feed needs it) | Not required (100% recommendation-driven) |
| **Feed assembly** | Hybrid: social-graph + recommendation | Pure recommendation engine |
| **Scaling simplification** | None — must run both distribution models | No fan-out infrastructure needed |
| **Write path** | Complex (media processing + fan-out to followers + index in recommendation engine) | Simpler (media processing + index in recommendation engine) |
| **Read path** | Complex (merge fan-out inbox + celebrity pull + recommendation injection) | Simpler (query recommendation engine → return ranked videos) |
| **Social graph importance** | Critical (affects feed, notifications, Stories, suggestions) | Minimal (following count matters less than content quality) |
| **Infrastructure complexity** | Higher (two distribution models, social graph, fan-out) | Lower (one distribution model, recommendation-only) |

**Key insight:** TikTok's architectural simplification is a competitive advantage. By not depending on the social graph for content distribution, TikTok avoids the fan-out problem entirely. Every video enters the same recommendation pipeline regardless of creator's follower count. A video from a creator with 0 followers and one from a creator with 100M followers go through the same path. Instagram must maintain both the social-graph path (home feed, Stories) and the recommendation path (Reels, Explore), doubling the infrastructure complexity.
