# Read Path (Counts, Lists, Personalization) -- Deep Dive

> Companion deep dive to the [interview simulation](01-interview-simulation.md). This document explores the read path for Facebook's reaction system -- the hot path that runs billions of times per day every time a post is rendered in News Feed.

---

## Table of Contents

1. [Reaction Summary -- THE Hot Path](#1-reaction-summary--the-hot-path)
2. [Multi-Layer Caching Strategy](#2-multi-layer-caching-strategy)
3. [Cache Invalidation Strategies](#3-cache-invalidation-strategies)
4. ["Friends Who Reacted" Personalization](#4-friends-who-reacted-personalization)
5. [Count Accuracy vs Latency Tradeoff](#5-count-accuracy-vs-latency-tradeoff)
6. [Reaction List -- Secondary Read Path](#6-reaction-list--secondary-read-path)
7. [End-to-End Read Flow](#7-end-to-end-read-flow)
8. [Contrast with Instagram](#8-contrast-with-instagram)
9. [Interview Cheat Sheet](#9-interview-cheat-sheet)

---

## 1. Reaction Summary -- THE Hot Path

Every time a post is rendered in News Feed, the client needs a **reaction summary** to display the familiar reaction bar beneath the post. This is not an optional API call -- it is part of the critical render path for every single post.

### 1.1 What the Client Needs

For every post rendered, the client requires five pieces of data:

```
Reaction Summary for Post #12345:
┌──────────────────────────────────────────────────────────────────┐
│                                                                  │
│  1. Total reaction count              →  1,247,892               │
│  2. Breakdown by type (top 3)         →  👍 812K  ❤️ 298K  😂 87K │
│  3. Did I (current user) react?       →  Yes, ❤️ (Love)          │
│  4. Top N friends who reacted         →  "Alice, Bob, and 48     │
│                                           others"                │
│  5. Type icons to display             →  [👍, ❤️, 😂]             │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

### 1.2 Scale of the Hot Path

```
Scale math for the read path:

  Monthly active users (MAU):             ~3.07 billion [Meta Q4 2024 earnings]
  Daily active users (DAU):               ~2.1 billion (estimated ~68% of MAU)
  Average posts per News Feed session:    ~50-100 posts viewed
  Average sessions per day per user:      ~3-5 sessions

  Posts rendered per day:
    2.1B users x 3 sessions x 75 posts = ~470 billion post renders/day
                                        = ~5.4 million post renders/second

  Each post render needs a reaction summary.
  That is ~5.4 million reaction summary reads per second, sustained.

  Peak (US evening hours):               ~10-15 million reads/second

  Latency budget: < 10 ms per reaction summary
    (This is part of the News Feed render path. News Feed itself
     must render in < 200 ms. The reaction summary is one of
     ~20 data fetches per post. Each gets ~10 ms budget.)
```

**This is why caching is not optional.** At 5+ million reads per second, hitting the database for every reaction summary would require thousands of MySQL instances dedicated solely to reaction count reads. Caching absorbs 99%+ of this traffic.

### 1.3 API Shape

```
GET /v1/entities/{entityId}/reactions/summary

Response:
{
  "entity_id": "post_12345",
  "total_count": 1247892,
  "type_counts": {
    "like": 812431,
    "love": 298102,
    "haha": 87221,
    "wow": 31044,
    "sad": 12891,
    "angry": 6203
  },
  "top_types": ["like", "love", "haha"],
  "viewer_reaction": {
    "reacted": true,
    "type": "love",
    "reaction_id": "rxn_abc123"
  },
  "friend_reactors": {
    "friends": [
      {"user_id": "u_alice", "name": "Alice", "type": "love"},
      {"user_id": "u_bob", "name": "Bob", "type": "like"}
    ],
    "remaining_count": 48
  }
}
```

**Key observation:** This response contains both **non-personalized data** (total count, type breakdown) and **personalized data** (viewer's own reaction, friends who reacted). This split matters for caching, as we will see in the next section.

---

## 2. Multi-Layer Caching Strategy

The reaction summary is served through a multi-layer caching architecture. Each layer absorbs a portion of the traffic, so that only a tiny fraction ever reaches the database.

### 2.1 Caching Layers

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                    Reaction Read Path -- Caching Layers                      │
│                                                                              │
│  ┌──────────────┐                                                            │
│  │   L1: Client  │  Client-side cache (in-app memory)                        │
│  │   Cache       │  - Caches reaction summaries for posts currently on screen │
│  │               │  - TTL: session lifetime (cleared on app close)           │
│  │               │  - Reduces redundant API calls on scroll-back             │
│  │               │  - Hit rate: ~40-60% of UI requests never leave device    │
│  └──────┬───────┘                                                            │
│         │  Cache miss                                                        │
│         v                                                                    │
│  ┌──────────────┐                                                            │
│  │   L2: CDN /   │  Edge cache (for non-personalized data ONLY)              │
│  │   Edge Cache  │  - CAN cache: total count, type breakdown, top types      │
│  │               │  - CANNOT cache: "did I react?", "friends who reacted"    │
│  │               │  - Partially applicable -- the non-personalized portion   │
│  │               │    could be served from edge, personalized portion merged  │
│  │               │    at app layer. In practice, Facebook does NOT split the  │
│  │               │    response this way [INFERRED -- not officially documented]│
│  │               │  - Hit rate: N/A (not used for this endpoint)             │
│  └──────┬───────┘                                                            │
│         │                                                                    │
│         v                                                                    │
│  ┌──────────────┐                                                            │
│  │   L3: TAO     │  Application cache (TAO cache / Memcache)                 │
│  │   Cache       │  - This is THE primary caching layer for reactions        │
│  │   (Memcache)  │  - Reaction summaries cached per entity                  │
│  │               │  - Cache key: entity:{entityId}:reaction_summary          │
│  │               │  - TTL: ~60 seconds (short, due to high write rate)       │
│  │               │  - Cache hit rate: 99%+ for popular posts                │
│  │               │  - Sub-millisecond read latency from Memcache            │
│  │               │  - Two-tier: Follower caches → Leader caches             │
│  └──────┬───────┘                                                            │
│         │  Cache miss (< 1% of requests)                                     │
│         v                                                                    │
│  ┌──────────────┐                                                            │
│  │   L4: MySQL   │  Database (MySQL via TAO)                                 │
│  │   (via TAO)   │  - Source of truth for reaction counts                   │
│  │               │  - Pre-aggregated counts table:                           │
│  │               │    reaction_counts(entity_id, reaction_type, count)       │
│  │               │  - Only hit on cache miss (< 1% of read traffic)         │
│  │               │  - Read latency: 1-5 ms (indexed lookup)                 │
│  └──────────────┘                                                            │
│                                                                              │
│  Effective hit rate (L1 through L3):                                         │
│    ~99.9% of read traffic is served from cache.                             │
│    MySQL handles < 0.1% of reaction summary reads.                          │
└──────────────────────────────────────────────────────────────────────────────┘
```

### 2.2 Why CDN/Edge Cache Does Not Work for Reactions

This is a common interview mistake -- proposing CDN caching for reaction data. Here is why it fails:

| Data Element | Personalized? | CDN Cacheable? | Why |
|---|---|---|---|
| Total count | No | Yes | Same for all viewers |
| Type breakdown | No | Yes | Same for all viewers |
| "Did I react?" | **Yes** | **No** | Different for every viewer |
| "Friends who reacted" | **Yes** | **No** | Different for every viewer (depends on viewer's friend list) |

The personalized fields make the response user-specific. A CDN would need to cache a separate version of the response for every (entityId, viewerId) pair -- that is billions of unique cache entries, which defeats the purpose of edge caching. The non-personalized portion (counts) could theoretically be split into a separate CDN-cacheable endpoint, but the overhead of two round trips per post outweighs the CDN benefit.

### 2.3 TAO's Two-Tier Cache Architecture

Facebook's TAO (The Associations and Objects store) uses a two-tier caching architecture that is central to understanding how reaction reads work at scale.

```
TAO Two-Tier Caching:

                    ┌─────────────────────────────────────┐
                    │          Web / API Servers           │
                    └───────┬──────────┬──────────┬───────┘
                            │          │          │
                            v          v          v
                    ┌─────────┐ ┌─────────┐ ┌─────────┐
                    │Follower │ │Follower │ │Follower │    Tier 1: Follower Caches
                    │Cache    │ │Cache    │ │Cache    │    - Many instances (thousands)
                    │Cluster  │ │Cluster  │ │Cluster  │    - Handle ALL read traffic
                    │  A      │ │  B      │ │  C      │    - Each is a Memcache cluster
                    └────┬────┘ └────┬────┘ └────┬────┘    - Consistent hashing within
                         │          │          │              each cluster
                         │          │          │
                         └──────────┼──────────┘
                                    │  Cache miss
                                    v
                            ┌─────────────┐
                            │   Leader     │               Tier 2: Leader Cache
                            │   Cache      │               - Fewer instances
                            │   Cluster    │               - Sits in front of MySQL
                            │              │               - Only receives cache misses
                            └──────┬──────┘                  from followers
                                   │  Cache miss
                                   v
                            ┌─────────────┐
                            │   MySQL      │               Tier 3: Database
                            │   (sharded)  │               - Source of truth
                            └─────────────┘               - Rarely hit (< 0.01%)


  Read flow:
    1. Web server sends read to its local Follower Cache
    2. Follower Cache HIT  → return immediately (99%+ of the time)
    3. Follower Cache MISS → forward to Leader Cache
    4. Leader Cache HIT    → return to follower, follower caches the result
    5. Leader Cache MISS   → query MySQL, populate leader cache, return to follower
```

**Source:** The TAO paper (Bronson et al., USENIX ATC 2013) describes this architecture: "TAO: Facebook's Distributed Data Store for the Social Graph."

**Why two tiers?** A single tier of Memcache would require every cache instance to hold the entire working set. With two tiers, follower caches hold a hot subset (recently accessed by their local web servers), while the leader cache holds the full working set. This reduces total memory footprint while maintaining high hit rates.

**Scale numbers from the TAO paper:**
- TAO handles **billions of reads per second** and millions of writes per second
- Read:write ratio is approximately **500:1**
- Cache hit rate in the follower tier: **96.4%** (from the paper)
- Cache hit rate including leader tier: **>99.8%**

### 2.4 Cache Key Design

```
Cache key structure for reaction data:

  Non-personalized (cached per entity):
    entity:{entityId}:reaction_summary
      → {total: 1247892, types: {like: 812K, love: 298K, ...}}

  Personalized (cached per user+entity pair):
    user:{userId}:entity:{entityId}:reaction
      → {reacted: true, type: "love", reaction_id: "rxn_abc"}

  Friends who reacted (cached per user+entity pair, short TTL):
    user:{userId}:entity:{entityId}:friend_reactors
      → {friends: [{id: "alice", type: "love"}, ...], remaining: 48}
```

**Why separate keys?** The non-personalized summary is shared across all viewers -- one cache entry serves millions of reads. The personalized data is per-user and must be cached separately. Separating them means the high-value non-personalized entry is not duplicated per user.

---

## 3. Cache Invalidation Strategies

Cache invalidation is the hardest problem in the reaction read path. When a user adds or removes a reaction, the cached reaction summary becomes stale. The system must invalidate the stale cache without creating a stampede of database queries.

### 3.1 Strategy Comparison

```
┌─────────────────────┬─────────────────────────┬────────────────────────────┐
│ Strategy            │ How It Works            │ Trade-offs                 │
├─────────────────────┼─────────────────────────┼────────────────────────────┤
│ Write-through       │ Update cache            │ + Consistent reads         │
│                     │ synchronously on write. │ + No stale data window     │
│                     │ Write to DB AND cache   │ - Adds ~2-5 ms to write   │
│                     │ in the same request.    │   latency                  │
│                     │                         │ - Cache update can fail    │
│                     │                         │   independently of DB      │
├─────────────────────┼─────────────────────────┼────────────────────────────┤
│ Write-behind        │ Write to DB, publish    │ + Lower write latency     │
│ (async              │ event to Kafka,         │ + Decoupled cache update   │
│  invalidation)      │ consumer invalidates    │ - Brief stale reads        │
│                     │ or updates cache.       │   (100 ms - 5 seconds)     │
│                     │                         │ - More complex pipeline    │
├─────────────────────┼─────────────────────────┼────────────────────────────┤
│ TAO's approach:     │ Write-behind with       │ + Low write latency        │
│ Write-behind +      │ lease-based cache fill. │ + Prevents thundering herd │
│ lease-based         │ On invalidation, cache  │ + Bounded stale window     │
│ invalidation        │ entry is deleted.       │ - Complexity in lease      │
│                     │ First reader to miss    │   management               │
│                     │ gets a "lease" to fill  │                            │
│                     │ the cache. Others wait. │                            │
└─────────────────────┴─────────────────────────┴────────────────────────────┘
```

### 3.2 TAO's Lease-Based Invalidation (What Facebook Actually Uses)

TAO's cache invalidation mechanism solves the **thundering herd** problem -- the scenario where a cache entry expires or is invalidated, and thousands of concurrent requests all miss the cache and slam the database simultaneously.

```
Thundering Herd Problem:

  Without leases:                          With leases (TAO):

  Cache entry deleted                      Cache entry deleted
       │                                        │
       ├─ Request A → cache miss → DB           ├─ Request A → cache miss
       ├─ Request B → cache miss → DB           │    → gets LEASE, queries DB
       ├─ Request C → cache miss → DB           ├─ Request B → cache miss
       ├─ Request D → cache miss → DB           │    → no lease, WAITS
       ├─ Request E → cache miss → DB           ├─ Request C → cache miss
       ...                                      │    → no lease, WAITS
       1000 concurrent DB queries!              ├─ Request D → cache miss
                                                │    → no lease, WAITS
                                                │
                                                │  Request A fills cache
                                                │       │
                                                ├─ Requests B,C,D read
                                                │  from freshly filled cache
                                                │
                                                1 DB query total!
```

**How leases work in TAO:**

1. When a cache entry is **invalidated** (deleted due to a write), the cache marks that key as "pending."
2. The **first reader** that encounters a cache miss for that key receives a **lease** -- a token that grants permission to fill the cache.
3. **Subsequent readers** that miss the same key see the "pending" state and **wait** (short spin/retry) rather than querying the database.
4. The lease holder queries the database, gets the fresh value, and writes it back to the cache with the lease token.
5. The waiting readers now hit the freshly populated cache and return.

**Lease expiration:** If the lease holder crashes or takes too long (e.g., > 10 seconds), the lease expires and a new lease is issued to the next reader. This prevents a crashed client from permanently blocking cache fills.

### 3.3 mcsqueal: Binlog-Based Cache Invalidation

Facebook uses a system called **mcsqueal** to propagate cache invalidations across all frontend clusters.

```
mcsqueal Cache Invalidation Pipeline:

  ┌──────────┐     ┌──────────┐     ┌────────────┐     ┌──────────────────┐
  │ Write    │────>│  MySQL   │────>│  MySQL     │────>│  mcsqueal        │
  │ Request  │     │  (TAO    │     │  Binlog    │     │  Daemon           │
  │          │     │   leader)│     │  (row-     │     │  (tails binlog,  │
  │          │     │          │     │   level    │     │   extracts       │
  └──────────┘     └──────────┘     │   changes) │     │   invalidation   │
                                    └────────────┘     │   messages)      │
                                                       └────────┬─────────┘
                                                                │
                                          Broadcasts invalidation to ALL
                                          Memcache follower clusters
                                                                │
                                    ┌───────────────────────────┤
                                    │               │           │
                                    v               v           v
                              ┌──────────┐   ┌──────────┐ ┌──────────┐
                              │Follower  │   │Follower  │ │Follower  │
                              │Cache A   │   │Cache B   │ │Cache C   │
                              │(delete   │   │(delete   │ │(delete   │
                              │ stale    │   │ stale    │ │ stale    │
                              │ entry)   │   │ entry)   │ │ entry)   │
                              └──────────┘   └──────────┘ └──────────┘
```

**Source:** The Memcache at Facebook paper (Nishtala et al., NSDI 2013) describes mcsqueal: "Scaling Memcache at Facebook."

**Why tail the binlog?** The binlog is the source of truth for all database mutations. By tailing it, mcsqueal catches every write -- including writes from batch jobs, migrations, and manual fixes -- not just writes from the application layer. This makes invalidation comprehensive and difficult to bypass accidentally.

**Invalidation latency:** From the time a reaction is written to MySQL, it takes approximately **10-100 ms** for the binlog event to propagate through mcsqueal and invalidate all follower caches. During this window, some readers may see a stale reaction count. This is the "eventual consistency" window for reaction counts.

---

## 4. "Friends Who Reacted" Personalization

The "Alice, Bob, and 48 others" display is one of the most expensive read operations in the reaction system. It requires a **set intersection** that is personalized per viewer.

### 4.1 The Problem

```
To compute "friends who reacted":

  Set A: Friends of the current viewer
         (e.g., viewer has 500 friends)

  Set B: Users who reacted to this post
         (e.g., post has 1,247,892 reactions)

  Result: A ∩ B
         (e.g., 50 of the viewer's friends reacted)

  Naive approach: Load all 1.2M reactor IDs, check each against
                  the viewer's 500 friends.
                  Cost: O(|B|) = O(1.2M) per post render.
                  At 5M renders/sec = 6 TRILLION set lookups/sec.
                  INFEASIBLE.
```

### 4.2 How TAO Handles This Efficiently

TAO's data model is built around **associations** -- directed edges in a social graph. The "reaction" is an association: `(userId) --[REACTED_TO]--> (entityId)`. The "friendship" is another association: `(userA) --[FRIEND]--> (userB)`.

TAO supports **association queries** that can efficiently find the intersection:

```
TAO Association Query for "friends who reacted":

  Step 1: Get viewer's friend list
          ASSOC_GET(viewerId, FRIEND) → [friend_1, friend_2, ..., friend_500]
          (This is cached in TAO -- the friend list rarely changes)

  Step 2: For each friend, check if they have a REACTED_TO association
          with this entity.
          ASSOC_GET(friend_i, REACTED_TO, entityId) → exists? type?

          Optimized: TAO batches these into a multi-get:
          ASSOC_MULTI_GET([friend_1, ..., friend_500], REACTED_TO, entityId)

  Cost: O(|friends|) = O(500) per post render, NOT O(|reactors|)
        500 cache lookups (batched) is feasible.
```

**Why this is efficient:** Instead of iterating over all reactors (could be millions), TAO iterates over the viewer's friends (typically hundreds). Since most of these friend-reaction checks hit the TAO cache, the actual database load is minimal.

### 4.3 Caching the Friend Reactor List

```
Cache key:    user:{viewerId}:entity:{entityId}:friend_reactors
Cache value:  [{user_id: "alice", name: "Alice", type: "love"}, ...]
TTL:          30-60 seconds (short, because the friend reactor list
              changes whenever any friend reacts or unreacts)

  Why short TTL?
  - If Alice reacts and the cache shows stale data, the viewer
    might not see Alice in the list for up to 60 seconds.
  - This is acceptable -- the viewer is unlikely to notice a
    60-second delay in personalized friend names appearing.
  - The viewer's OWN reaction is shown immediately via optimistic
    update (see Section 5), so their own action feels instant.
```

### 4.4 Priority Ranking for Friend Names

The "Alice, Bob, and 48 others" display must choose which friends to show by name. The selection is NOT random -- it is ranked:

```
Friend reactor display priority:

  1. Close friends (explicitly marked by the viewer)
  2. Friends the viewer interacts with most frequently
     (messages, comments, profile views -- engagement signals)
  3. Mutual friends (friends who are also friends with the post author)
  4. Most recent reactors (recency as a tiebreaker)

  This ranking is computed at cache-fill time, not on every read.
  The ranked list is cached and served as-is until TTL expiry.
```

---

## 5. Count Accuracy vs Latency Tradeoff

### 5.1 Exact Counts Are Not Necessary

A core design insight: **users do not verify exact reaction counts.** Nobody looks at "1,247,892" and checks whether it should be "1,247,893." What matters is:

1. The count is **directionally correct** (growing when reactions are added)
2. The user's **own reaction** is reflected immediately
3. The count is **not wildly wrong** (off by millions would be noticeable)

### 5.2 How Facebook Exploits This Insight

```
Count display strategy:

  Actual count          Displayed as       Precision
  ─────────────         ────────────       ─────────
  3                     "3"                Exact
  47                    "47"               Exact
  1,247                 "1.2K"             Approximate
  87,432                "87K"              Approximate
  1,247,892             "1.2M"             Approximate
  2,100,000,000         "2.1B"             Approximate

  For counts > 1,000, Facebook shows approximate numbers.
  This means the cached count can be off by hundreds or even
  thousands without the user noticing any difference in the
  displayed value.
```

### 5.3 Optimistic Updates for the Current User

When the viewer taps "Like" on a post, the client does NOT wait for the server round trip to update the UI:

```
Optimistic Update Flow:

  User taps "Like"
       │
       ├──────────────────────────┐
       │  CLIENT (immediate)      │  SERVER (async)
       │                          │
       │  1. Increment displayed  │  1. Receive POST /reactions
       │     count by 1           │  2. Write to DB
       │  2. Show "Like" icon     │  3. Update pre-aggregated count
       │     as selected          │  4. Invalidate cache
       │  3. Cache the reaction   │  5. Return 200 OK
       │     locally              │
       │                          │
       │  User sees instant       │  Server confirms within
       │  feedback (< 50 ms)      │  100-500 ms
       │                          │
       └──────────────────────────┘

  If server returns error:
    Client rolls back the optimistic update
    (decrement count, deselect icon)
```

**Why this matters:** The user's own action feels instantaneous even though the actual count propagation through DB, cache invalidation, and mcsqueal takes 100 ms to several seconds. The client-side optimistic update bridges this gap.

### 5.4 Eventual Consistency Window

```
Timeline of count propagation after a reaction:

  T+0 ms      User taps "Like"
  T+50 ms     Client shows optimistic update (local only)
  T+100 ms    API server writes to MySQL
  T+150 ms    Pre-aggregated count updated in MySQL
  T+200 ms    Kafka event published
  T+250 ms    mcsqueal detects binlog change
  T+300 ms    Leader cache invalidated
  T+350 ms    Follower caches invalidated
  T+500 ms    Next reader sees updated count from cache refill

  Total staleness window: ~500 ms for same-region viewers
  Cross-region (async replication): 1-5 seconds
```

For a post with 1.2M reactions, a 500 ms delay means the count might be off by the number of reactions received in that 500 ms window. For a non-viral post (1 reaction per second), that is off by ~1. For a viral post (100K reactions per second), that is off by ~50K -- but the displayed count "1.2M" would not change visibly.

---

## 6. Reaction List -- Secondary Read Path

The reaction list is a **separate, less latency-sensitive** read path. It is only triggered when a user explicitly clicks "see who reacted" -- a much rarer action than viewing the reaction summary.

### 6.1 API

```
GET /v1/entities/{entityId}/reactions?type=love&cursor=ts_1700000000&limit=20

Response:
{
  "reactions": [
    {
      "reaction_id": "rxn_001",
      "user_id": "u_alice",
      "user_name": "Alice Smith",
      "reaction_type": "love",
      "timestamp": "2024-11-14T10:30:00Z"
    },
    {
      "reaction_id": "rxn_002",
      "user_id": "u_bob",
      "user_name": "Bob Jones",
      "reaction_type": "love",
      "timestamp": "2024-11-14T10:29:55Z"
    }
    // ... 18 more
  ],
  "pagination": {
    "next_cursor": "ts_1700000015",
    "has_more": true
  }
}
```

### 6.2 Design Details

| Property | Hot Path (Summary) | Secondary Path (List) |
|---|---|---|
| **Trigger** | Every post render | User clicks "see who reacted" |
| **Frequency** | Billions/day | Millions/day (1000x less) |
| **Latency target** | < 10 ms | < 100 ms (acceptable) |
| **Data source** | Pre-aggregated counts (cached) | Individual reaction records |
| **Pagination** | N/A (single response) | Cursor-based (by timestamp) |
| **Caching** | Aggressively cached (99%+) | Lightly cached or not cached |
| **Personalization** | Yes (viewer's reaction, friends) | Yes (privacy filtering) |

### 6.3 Cursor-Based Pagination

```
Cursor-based pagination for reaction list:

  Page 1: GET /reactions?limit=20
           → returns reactions sorted by timestamp DESC
           → next_cursor = timestamp of the 20th reaction

  Page 2: GET /reactions?cursor=ts_1700000015&limit=20
           → returns reactions with timestamp < cursor
           → next_cursor = timestamp of the 40th reaction

  Page 3: GET /reactions?cursor=ts_1700000030&limit=20
           → ...

  Why cursor-based (not offset-based)?
  ─────────────────────────────────────
  Offset-based:  LIMIT 20 OFFSET 40
    - Problem: If 5 new reactions are added between page requests,
      the offset shifts and the user sees duplicates or misses entries.
    - Problem: OFFSET N is O(N) in most databases -- scanning and
      discarding N rows before returning results.

  Cursor-based:  WHERE timestamp < :cursor LIMIT 20
    - Stable: New reactions don't shift the cursor position.
    - Efficient: Index seek to the cursor value, then scan 20 rows.
    - Stateless: Server doesn't need to track pagination state.
```

### 6.4 Filter by Reaction Type

```
Filtered query:

  GET /reactions?type=love&cursor=ts_1700000015&limit=20

  Database query (behind TAO):
    SELECT user_id, reaction_type, timestamp
    FROM reactions
    WHERE entity_id = :entityId
      AND reaction_type = 'love'
      AND timestamp < :cursor
    ORDER BY timestamp DESC
    LIMIT 20;

  Index required: (entity_id, reaction_type, timestamp DESC)
  This is a composite index that supports both filtered and
  unfiltered queries efficiently.
```

### 6.5 Privacy Filtering

The reaction list must exclude users who should not be visible to the viewer:

```
Privacy filters applied to reaction list:

  1. Blocked users:     If viewer has blocked user X, X does not appear
  2. Blocking users:    If user X has blocked the viewer, X does not appear
  3. Deactivated users: Deactivated accounts are hidden
  4. Privacy settings:  Users who have set their reactions to "private"
                        (if such a setting exists)

  Implementation:
    - Block list is loaded from cache (viewer's block list is small,
      typically < 100 users, cached per user)
    - Reaction list is filtered in the application layer AFTER
      fetching from the database/cache
    - The filter is O(|page_size| * |block_list|) per page -- negligible
```

---

## 7. End-to-End Read Flow

Putting it all together, here is the complete read flow for rendering a reaction summary on a News Feed post.

### 7.1 Sequence Diagram

```
┌────────┐      ┌──────────┐     ┌──────────┐     ┌──────────┐     ┌──────┐
│ Client │      │ API      │     │ TAO      │     │ TAO      │     │MySQL │
│ (App)  │      │ Server   │     │ Follower │     │ Leader   │     │      │
└───┬────┘      └────┬─────┘     └────┬─────┘     └────┬─────┘     └──┬───┘
    │                │               │               │              │
    │  News Feed     │               │               │              │
    │  render        │               │               │              │
    │  (50 posts)    │               │               │              │
    │                │               │               │              │
    │ ──── Step 1: Check client cache ────                          │
    │  30 posts found in L1 cache                                   │
    │  20 posts need server fetch                                   │
    │                │               │               │              │
    │  GET /batch    │               │               │              │
    │  reaction      │               │               │              │
    │  summaries     │               │               │              │
    │  (20 posts) ──>│               │               │              │
    │                │               │               │              │
    │                │ ──── Step 2: Batch TAO read (20 keys) ────   │
    │                │               │               │              │
    │                │  MULTI_GET    │               │              │
    │                │  (20 entity   │               │              │
    │                │   keys) ─────>│               │              │
    │                │               │               │              │
    │                │               │  18 HIT       │              │
    │                │               │  (99% rate)   │              │
    │                │               │               │              │
    │                │               │  2 MISS ─────>│              │
    │                │               │               │              │
    │                │               │               │  2 HIT       │
    │                │               │               │  (leader     │
    │                │               │               │   cache)     │
    │                │               │               │              │
    │                │               │<── return ────│              │
    │                │               │  (fill        │              │
    │                │               │   follower    │              │
    │                │               │   cache)      │              │
    │                │               │               │              │
    │                │<── 20 results─│               │              │
    │                │               │               │              │
    │                │ ──── Step 3: Personalization merge ────      │
    │                │                                              │
    │                │  For each post:                              │
    │                │   a. Lookup viewer's own reaction            │
    │                │      (TAO assoc query, cached per user)      │
    │                │   b. Lookup friends who reacted              │
    │                │      (TAO assoc intersection, cached)        │
    │                │   c. Merge into response                     │
    │                │               │               │              │
    │<── 20 reaction │               │               │              │
    │    summaries ──│               │               │              │
    │                │               │               │              │
    │ ──── Step 4: Client caches results in L1 ────                │
    │  All 50 posts now have reaction data                         │
    │  Render complete                                             │
    │                │               │               │              │
```

### 7.2 Latency Breakdown

```
Step-by-step latency for a single reaction summary read:

  Step                                  Latency       Notes
  ────                                  ───────       ─────
  Client cache check (L1)              < 1 ms        In-memory lookup
  Network: client → API server          10-50 ms      (batched with other data)
  TAO follower cache lookup             0.5-1 ms      Memcache GET
  TAO leader cache lookup (on miss)     1-3 ms        Network hop to leader
  MySQL query (on leader miss)          2-10 ms       Indexed read
  Personalization merge                 1-2 ms        Cache lookups for viewer
  Network: API server → client          10-50 ms      (batched response)

  Total server-side processing:         2-5 ms        (cache hit path)
  Total end-to-end (including network): 30-100 ms     (within News Feed budget)

  Note: The reaction summary fetch is parallelized with other
  News Feed data fetches (post content, comments, ads). The
  total News Feed render latency is dominated by the slowest
  fetch, not the sum of all fetches.
```

---

## 8. Contrast with Instagram

Instagram is also a Meta product and uses the same TAO infrastructure, but its reaction system is simpler and has made different product decisions that affect the read path.

### 8.1 Key Differences

| Dimension | Facebook Reactions | Instagram Likes |
|---|---|---|
| **Reaction types** | 6 types (Like, Love, Haha, Wow, Sad, Angry) | 1 type (Like/heart) |
| **Count display** | Always shown to all viewers | Hidden in some markets (only author sees exact count) |
| **Type breakdown** | Top 3 types shown with icons | N/A (single type) |
| **Friend reactors** | "Alice, Bob, and 48 others" | "Liked by alice and 47 others" |
| **Complexity** | Per-type counters, type selection UI | Single counter, binary toggle |
| **Storage** | 6 counter rows per entity (one per type) | 1 counter row per entity |
| **Cache key** | Contains type breakdown | Single count value |

### 8.2 Instagram's Hidden Likes Experiment

In 2019-2021, Instagram hid like counts from viewers in several markets (Australia, Brazil, Canada, Italy, Japan, and others). Only the post author could see the exact count. This product decision dramatically simplified the read path:

```
Instagram read path with hidden likes:

  For viewers (non-author):
    - No count to fetch or display
    - Only need: "Did I like this?" (boolean, per-user cache)
    - "Liked by alice and others" (no count, just names)
    - Much cheaper read path

  For post author:
    - Full count displayed (same as Facebook's read path)
    - Only 1 read per author view, not per viewer view

  Result: ~99% reduction in count-related read traffic
          (viewers are 1000x more common than authors)
```

Instagram eventually made like count visibility a user-level setting (users can choose to hide counts), but the experiment demonstrated that hiding counts is a valid product lever to reduce infrastructure cost on the read path.

### 8.3 Shared Infrastructure

Despite the product differences, Instagram and Facebook use the same underlying infrastructure:

- **TAO** for the social graph and association storage
- **Memcache** for caching (with TAO's two-tier architecture)
- **MySQL** as the persistent store (behind TAO)
- **mcsqueal** for binlog-based cache invalidation

The infrastructure is the same; the product decisions on top determine how much of the infrastructure is exercised per read.

---

## 9. Interview Cheat Sheet

### 9.1 Key Numbers to Memorize

```
Read Path Scale:
  Post renders per day:             ~hundreds of billions
  Reaction summary reads/sec:       ~millions (sustained), ~10M+ (peak)
  TAO reads/sec:                    billions (across all use cases)
  TAO follower cache hit rate:      ~96% (from TAO paper)
  Overall cache hit rate:           >99%
  Read:write ratio:                 100:1 to 1000:1
  Latency target:                   < 10 ms server-side
  Staleness window:                 ~500 ms same-region, 1-5 sec cross-region
  Memcache latency:                 < 1 ms
  MySQL read latency:               2-10 ms
```

### 9.2 Common Interview Questions

**Q: "How do you serve reaction counts at this scale?"**
A: Multi-layer caching with pre-aggregated counts. The counts are pre-computed and stored in a separate `reaction_counts` table (not computed via `COUNT(*)` at read time). These counts are cached in TAO's two-tier Memcache layer with 99%+ hit rate. Only cache misses (<1%) reach MySQL. The cache is invalidated asynchronously via mcsqueal (binlog tailing), with lease-based fill to prevent thundering herd.

**Q: "Why not use a CDN for reaction data?"**
A: Reaction summaries contain personalized data -- "did I react?" and "friends who reacted" are different for every viewer. A CDN would need to cache a separate response for every (entityId, viewerId) pair, which is billions of unique entries. The non-personalized portion (counts) could theoretically be edge-cached, but splitting the response into two calls adds latency that outweighs the CDN benefit.

**Q: "How do you show 'Alice, Bob, and 48 others'?"**
A: This is a set intersection of the viewer's friend list and the post's reactor list. TAO makes this efficient by iterating over the viewer's friends (typically ~500) and checking each friend's REACTED_TO association with the entity -- O(|friends|) rather than O(|reactors|). The result is cached per (viewerId, entityId) with a short TTL (~60 seconds). Friend names are ranked by social affinity (close friends first, then interaction frequency).

**Q: "What happens if the cache goes down?"**
A: TAO's follower caches failing would cause all traffic to hit the leader caches. Leader caches failing would cause traffic to hit MySQL, which cannot handle the full read load. Mitigations: (1) gutter pools -- spare Memcache clusters that absorb traffic during failures, (2) circuit breakers to shed load rather than overwhelm the database, (3) stale-while-revalidate -- serve stale cached data during recovery rather than returning errors. The Memcache at Facebook paper (NSDI 2013) describes gutter pools as a key failover mechanism.

**Q: "Are reaction counts strongly or eventually consistent?"**
A: Eventually consistent with a convergence window of ~500 ms to 5 seconds. The user's own reaction is shown immediately via client-side optimistic update (not dependent on server propagation). Other viewers may see a slightly stale count for a few seconds after a reaction. For large counts displayed as "1.2M", this staleness is invisible. For exact counts on small posts (e.g., 47 reactions), the count might briefly show 47 instead of 48 -- acceptable for a social feature.

---

*Next: [06-notification-pipeline.md](06-notification-pipeline.md) -- Notification coalescing, delivery, and how "Alice, Bob, and 498 others liked your post" gets generated.*
