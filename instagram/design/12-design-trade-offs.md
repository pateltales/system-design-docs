# Design Philosophy & Trade-off Analysis

> Not just "what Instagram does" but "why this and not that."
> Every design decision is a trade-off. Understanding the alternatives reveals the reasoning.

---

## Table of Contents

1. [Hybrid Fan-out vs Pure Fan-out](#1-hybrid-fan-out-vs-pure-fan-out)
2. [Algorithmic Feed vs Chronological Feed](#2-algorithmic-feed-vs-chronological-feed)
3. [Directed Graph vs Undirected Graph](#3-directed-graph-vs-undirected-graph)
4. [Reactive CDN vs Proactive Push](#4-reactive-cdn-vs-proactive-push)
5. [Haystack/f4 vs Off-the-Shelf Object Storage](#5-haystackf4-vs-off-the-shelf-object-storage)
6. [MQTT vs WebSocket for Mobile](#6-mqtt-vs-websocket-for-mobile)
7. [Storage Evolution: PostgreSQL → Cassandra → TAO](#7-storage-evolution-postgresql--cassandra--tao)
8. [Approximate Counting vs Exact Counting](#8-approximate-counting-vs-exact-counting)
9. [Ephemeral + Permanent Content vs Pick One](#9-ephemeral--permanent-content-vs-pick-one)
10. [Social-Graph Feed + Recommendation Feed vs Pick One](#10-social-graph-feed--recommendation-feed-vs-pick-one)

---

## 1. Hybrid Fan-out vs Pure Fan-out

### The Decision

Instagram uses **hybrid fan-out**: fan-out on write for normal users (<500K followers), fan-out on read for celebrities (>500K followers).

### Alternatives Considered

| Approach | Pros | Cons |
|---|---|---|
| **Pure fan-out on write** | Reads are O(1) — just fetch pre-built inbox. Simple read path. | Celebrity posts require 650M writes. Minutes of latency. Massive write amplification. Hot shards. |
| **Pure fan-out on read** | Zero write amplification. Posting is instant for everyone. | Reads are O(following_count) — 500 lookups per feed load. Read latency too high for the common case. |
| **Hybrid (Instagram's choice)** | Reads are fast for 99% of cases. Celebrity posting is instant. | Two code paths (write path + read path). Routing logic (is this user a celebrity?). More complex to debug. |

### Why Hybrid Wins

```
99%+ of users have <500K followers.
For them, fan-out on write is perfect:
  1 post → ~200 writes (avg following) → 200 writes is trivial
  Read: ZREVRANGE feed:{userId} → O(1) from Redis → fast

0.01% of users have >1M followers.
For them, fan-out on read is necessary:
  1 post → stored once in celebrity's post list
  Read: merge from celebrity's post list at query time → ~5-20 extra lookups

The hybrid approach handles both extremes at the cost of:
  • A celebrity threshold parameter (tuned based on system load)
  • Two code paths in feed assembly
  • Routing logic: "is this author a celebrity?" → if yes, don't fan-out on write
```

### What Twitter and TikTok Do

- **Twitter (2012-era)**: Also uses hybrid fan-out. Similar celebrity threshold. Nearly identical solution because the problem is identical — directed follow graph with extreme degree variance.
- **TikTok**: Does NOT use fan-out at all. The For You page is 100% recommendation-driven — content distribution doesn't depend on who follows whom. TikTok sidesteps the fan-out problem entirely by not building a social-graph-based feed.

### The Honest Cost

The hybrid approach adds real engineering complexity:
- Edge case: what happens when a user crosses the celebrity threshold? (Their old posts were fanned out on write; new posts are fanned out on read. Feed assembly must handle both.)
- Threshold tuning: too low → too many users on the read path → read latency increases. Too high → fan-out on write becomes expensive for near-celebrity users.
- Debugging: "Why did user X not see user Y's post?" now has two possible code paths to investigate.

**Is it worth it?** Yes. The alternative (either pure approach) fails catastrophically at one extreme. The hybrid's complexity is manageable; the alternatives' failure modes are not.

---

## 2. Algorithmic Feed vs Chronological Feed

### The Decision

Instagram switched from chronological to algorithmic ranking in **June 2016**. Added an optional "Following" chronological feed in **2022** after user backlash.

### The Reasoning

**VERIFIED — from Instagram's official blog post (March 2016):**

Instagram stated that users were missing 70% of posts in their feed due to chronological ordering. When a user follows 500 accounts posting multiple times per day, the chronological feed becomes an overwhelming reverse-chronological list where important posts are buried.

### Trade-off Analysis

| Dimension | Chronological | Algorithmic |
|---|---|---|
| **User control** | Full — users see everything in order | Low — algorithm decides what's "relevant" |
| **Completeness** | All posts visible (if you scroll enough) | Some posts never seen (algorithm deprioritized them) |
| **Engagement** | Lower (users miss important posts) | Higher (relevant posts surfaced first) |
| **Creator fairness** | Equal — all posts shown in time order | Unequal — popular content gets more visibility |
| **Filter bubble risk** | None — no algorithmic bias | Yes — algorithm reinforces existing interests |
| **Engineering complexity** | Low — simple time-ordered query | High — ML model, feature extraction, retraining |
| **Business value** | Lower (less engagement → less ad revenue) | Higher (more engagement → more ad impressions) |

### Why Algorithmic Won (For the Home Feed)

```
The math is simple:

If a user follows 500 accounts, and each posts once per day:
  → 500 new posts per day
  → User opens app ~7 times/day, views ~30 posts per session
  → 210 posts seen / 500 posts available = 42% seen rate

In practice, many accounts post multiple times, so:
  → User sees even less — Instagram's claim of "missing 70%" is plausible

Algorithmic ranking ensures the 30 posts you DO see are the
30 most relevant ones, not the 30 most recent ones.
```

### The 2022 Compromise

User backlash ("bring back the chronological feed") led Instagram to add the "Following" tab — a pure chronological feed of posts from followed accounts. This is a dual-feed model:
- **Home feed**: Algorithmic (default, includes Suggested Posts from non-followed accounts)
- **Following feed**: Chronological (opt-in, only followed accounts)

**Architectural implication:** Instagram must now maintain two feed assembly paths:
1. **Home feed**: fan-out inbox → ML ranking → diversity injection → suggested posts → serve
2. **Following feed**: fan-out inbox → sort by timestamp → serve (simpler but still needs fan-out infrastructure)

---

## 3. Directed Graph vs Undirected Graph

### The Decision

Instagram uses a **directed graph** (A follows B ≠ B follows A). Facebook uses an **undirected graph** (A friends B = B friends A).

### Why Directed

The directed graph enables the **creator/audience model** — the fundamental product paradigm of Instagram.

```
Creator model (Instagram, Twitter):
  Creator → creates content → distributed to followers
  Followers → consume content → no obligation to follow back
  Asymmetry: Cristiano Ronaldo has 650M followers, follows 600 people

Friend model (Facebook):
  Friends → share content → both see each other's posts
  Symmetry: mutual consent required, max ~5,000 friends
  No "audience" concept — everyone is a peer
```

### Trade-off Analysis

| Dimension | Directed (Instagram) | Undirected (Facebook) |
|---|---|---|
| **Fan-out variance** | Extreme (1 to 650M followers) | Bounded (max 5,000 friends) |
| **Fan-out complexity** | High (celebrity threshold needed) | Lower (max 5,000 writes per post) |
| **Content distribution** | One-to-many (broadcast) | Many-to-many (conversation) |
| **Product feel** | Media publishing platform | Social networking platform |
| **Follow friction** | Low (one-click, no consent for public accounts) | High (request + acceptance) |
| **Creator growth** | Easy (anyone can follow) | Hard (both parties must agree) |
| **Privacy default** | Public by default | Private by default |

### The Deep Implication

The directed graph is WHY the fan-out problem exists at Instagram's scale. If Instagram used Facebook's undirected friendship model (capped at 5,000), fan-out on write would work for every user — no hybrid approach needed. The directed graph's unbounded degree variance is the root cause of the celebrity fan-out problem.

**Is the trade-off worth it?** Absolutely. The directed graph IS Instagram's product. The creator/audience model drives the entire platform's value. The engineering cost of hybrid fan-out is a small price for the product model.

---

## 4. Reactive CDN vs Proactive Push

### The Decision

Instagram uses **reactive caching** — content is cached at CDN edge on first request, not proactively pushed.

### Why Not Proactive Push (Netflix's Approach)?

```
Netflix can use proactive push because:
  • Curated catalog: ~tens of thousands of titles
  • Predictable demand: top 100 titles get 80%+ of traffic
  • Content changes rarely: new titles added weekly, not per-second
  • Per-title size is huge (GB) → pre-positioning saves significant origin bandwidth

Instagram CANNOT use proactive push because:
  • UGC: 100M+ new uploads per day
  • Unpredictable demand: any post could go viral or be viewed only once
  • Content changes constantly: every second brings new posts
  • Per-item size is small (KB) → pushing billions of small items is impractical

Calculation:
  100M uploads × 4 resolutions × 100KB avg = 40TB new content/day
  Push to 100+ PoPs: 40TB × 100 = 4PB of data transfer/day
  Most content is viewed by <100 people → 99%+ of proactive pushes wasted
```

### Reactive Caching Works Because

1. **Long-tail distribution**: A tiny fraction of content gets most views. Reactive caching naturally caches the popular items.
2. **Temporal locality**: Most views happen within hours of posting. Content is cached when hot and evicted when cold.
3. **Request coalescing**: When viral content causes a cache miss, only one request goes to origin — the rest wait for the cache fill.
4. **Multi-tier cache**: L1 edge miss → L2 regional hit (70-80% of remaining) → only 5-10% reach origin.

---

## 5. Haystack/f4 vs Off-the-Shelf Object Storage

### The Decision

Meta built custom blob storage (Haystack for hot, f4 for warm) instead of using off-the-shelf solutions (S3, HDFS).

### The Problem with Filesystems at Scale

**VERIFIED — from "Finding a needle in Haystack" USENIX OSDI 2010:**

```
Traditional filesystem (POSIX):
  To read one photo:
    1. Read directory inode   → 1 disk I/O
    2. Read directory entry   → 1 disk I/O
    3. Read file inode        → 1 disk I/O
    4. Read file data         → 1 disk I/O
  = 4 disk I/Os per photo read (worst case, cold cache)

  At billions of photos, the metadata (inodes, directory entries)
  exceeds RAM → metadata cache misses are frequent

Haystack:
  To read one photo:
    1. Look up offset in in-memory index → 0 disk I/O (RAM)
    2. Read photo data at offset         → 1 disk I/O
  = 1 disk I/O per photo read (always)

  How: Pack multiple photos into large volume files (100GB each).
  Maintain an in-memory index: photoId → (volume, offset, size).
  Index is ~10 bytes per photo → billions of photos fit in RAM.
```

### Trade-off Analysis

| Dimension | Custom Storage (Haystack/f4) | Off-the-Shelf (S3) |
|---|---|---|
| **Read efficiency** | 1 disk I/O per read (in-memory index) | 2-4 disk I/Os (filesystem metadata lookups) |
| **Index overhead** | ~10 bytes per photo (billions fit in RAM) | Filesystem metadata (~1KB+ per file) |
| **Build cost** | Extremely high (custom engineering, years) | Zero (managed service) |
| **Operational cost** | High (own team to maintain, debug, evolve) | Low (provider handles everything) |
| **Warm storage** | f4: Reed-Solomon (14,10), 2.1x effective replication | S3 Glacier: provider-managed, opaque |
| **Space efficiency** | f4 saves ~65% vs hot storage (erasure coding) | S3 IA/Glacier: similar savings |
| **Flexibility** | Purpose-built for Meta's workload (many small files) | General-purpose (works for any workload) |

### When Does Custom Storage Make Sense?

```
The break-even calculation:

Custom storage development cost: ~$50M (team of 50 engineers × 2 years)
Operational cost: ~$10M/year (dedicated team)

S3 cost at Instagram's scale:
  Hundreds of PBs of storage × $0.023/GB/month = ~$50M+/month
  Billions of read requests × $0.0004/1000 = millions/month

At Meta's scale (hundreds of PBs, billions of daily reads),
custom storage pays for itself within MONTHS.

For a startup with 1TB of photos? Use S3. Don't build Haystack.
The threshold is somewhere around 10-100PB where custom becomes viable.
```

---

## 6. MQTT vs WebSocket for Mobile

### The Decision

Meta uses **MQTT for mobile apps** (iOS/Android) and **WebSocket for web** (instagram.com).

### Why MQTT for Mobile?

| Dimension | MQTT | WebSocket |
|---|---|---|
| **Header overhead** | 2 bytes minimum | 2-14 bytes per frame |
| **Battery impact** | Minimal (tiny keepalive packets, ~60-byte ping) | Moderate (WebSocket ping frames are larger) |
| **Network resilience** | Designed for unreliable mobile networks (3G, spotty WiFi) | Good but not mobile-optimized |
| **QoS levels** | 3 levels (at-most-once, at-least-once, exactly-once) | None (manual retry logic needed) |
| **Session awareness** | Built-in (clean session flag, retained messages, will messages) | None (must build on top) |
| **Browser support** | No (requires native library) | Yes (native in all browsers) |
| **Protocol maturity** | Designed in 1999 for satellite links, battle-tested on constrained devices | Standardized 2011, designed for browsers |

### The Decisive Factor: Battery Life

```
On mobile, every network wake-up costs battery:
  • Radio state: idle → connected → active → idle
  • Each transition costs power
  • Frequent small messages with WebSocket: many transitions

MQTT's keepalive is optimized for this:
  • Keepalive interval: 60 seconds (configurable)
  • Keepalive packet: ~2 bytes
  • The radio stays in a low-power state between keepalives
  • OS can batch keepalives with other network activity

WebSocket keepalive is not as battery-friendly:
  • Ping/pong frames are larger
  • Less integration with mobile OS power management
  • Must implement reconnection logic manually

At 500M+ daily mobile users, even a 1% battery efficiency improvement
translates to billions of collective battery-hours saved per day.
```

### Why WebSocket for Web?

WebSocket is natively supported by all modern browsers — no need for a third-party MQTT-over-WebSocket library. For the web client, simplicity wins: WebSocket provides the same real-time push capability without the complexity of an MQTT client library.

---

## 7. Storage Evolution: PostgreSQL → Cassandra → TAO

### The Journey

**VERIFIED — from Instagram Engineering blog (early days) and Meta's published papers:**

```
2010 (Instagram launch):
  PostgreSQL for everything
  • Users, posts, likes, comments, follows — all in one DB
  • 13 employees, ~100 EC2 instances
  • ACID transactions, mature tooling, team familiarity
  • Worked perfectly for 0-30M users

2011-2012 (Rapid growth → 100M users):
  PostgreSQL + sharding
  • Sharded by userId across ~12 PostgreSQL instances
  • Cross-shard queries becoming painful (follower lists, feeds)
  • Write throughput hitting limits (fan-out on write)

2012+ (Facebook acquisition → Meta infrastructure):
  Specialized storage for each access pattern:

  TAO (social graph):
  • Follows, likes, comments (objects + associations)
  • 2-tier cache over sharded MySQL
  • Optimized for graph traversal queries
  • Why not stay with PostgreSQL? Graph operations (who follows X?
    who liked post Y?) need specialized caching and the association-list
    abstraction. PostgreSQL can do it, but not at 500:1 read-write ratio
    with trillions of cache ops.

  Cassandra (write-heavy, time-series):
  • Feed inboxes, activity feeds, Stories metadata
  • Why Cassandra? Write throughput scales linearly.
    Fan-out on write = N writes per post. Cassandra handles this.
    PostgreSQL serializes writes through a single master.
    Cassandra distributes writes across a ring.
  • Native TTL for Stories (24-hour expiration handled by DB,
    no external cleanup job).

  MySQL/MyRocks (structured data):
  • User accounts, post metadata, settings
  • MyRocks: RocksDB-based storage engine, 50% space savings vs InnoDB
  • Why MySQL instead of PostgreSQL? Meta standardized on MySQL.
    MyRocks optimized for Meta's workload (write-heavy, compressible data).

  Redis (in-memory, ephemeral):
  • Feed inboxes (sorted sets), counters, rate limiting, Stories seen-state
  • Why Redis? Sub-millisecond latency for feed reads.
    Sorted sets are the perfect data structure for ranked feed inboxes.
    PostgreSQL can't match Redis's read latency for this access pattern.

  Haystack/f4 (blob storage):
  • All media files
  • Why custom? At hundreds of PBs, filesystem overhead becomes dominant.
    1 disk I/O per read vs 3-4 — that's a 3-4x throughput improvement.
```

### The Lesson

There is no one-size-fits-all database. Each storage system excels at a specific access pattern:

| Access Pattern | Best Tool | Why Not PostgreSQL? |
|---|---|---|
| Social graph queries | TAO | Need association-list abstraction with 2-tier cache. Graph traversal needs specialized caching. |
| Feed inboxes (write-heavy, time-ordered) | Cassandra + Redis | Write throughput scales linearly. Native TTL. Sorted sets in Redis for sub-ms reads. |
| Structured records (accounts, metadata) | MySQL/MyRocks | Meta standardized on MySQL. MyRocks gives 50% compression. |
| Blob storage (photos, videos) | Haystack/f4 | Filesystem metadata overhead at billion-file scale. |
| Caching | Memcache | Trillions of ops/day. Dedicated caching layer needed. |

**The honest answer to "why not just use PostgreSQL for everything?":** You CAN, up to maybe 50M users with aggressive optimization. Beyond that, the single access pattern assumption breaks. Different data has fundamentally different access patterns (graph traversal vs time-series writes vs blob reads vs cached lookups), and each pattern benefits from a specialized system. The cost is operational complexity — more systems to operate, more failure modes, more expertise needed.

---

## 8. Approximate Counting vs Exact Counting

### The Decision

Instagram uses approximate counters for high-traffic metrics (like counts, view counts) rather than exact counts.

### Why Approximate?

```
Exact counting requires serialization:
  10,000 concurrent likes on the same post
  → UPDATE posts SET like_count = like_count + 1 WHERE id = ?
  → Row-level lock → only one writer at a time
  → 10,000 serialized writes → bottleneck

Approximate counting with sharded counters:
  10,000 concurrent likes
  → Distributed across 10 Redis counter shards
  → Each shard handles ~1,000 INCR ops independently
  → Periodic aggregation (every 5 seconds) sums shards
  → Display value may lag by 5 seconds

The difference to the user?
  Exact: "1,234,567 likes"
  Approximate: "1,234,543 likes" (off by 24, or 0.002%)
  Displayed as: "1.2M likes" — identical to the user
```

### Where Exact Counting IS Required

Not everything can be approximate:

| Metric | Exact or Approximate? | Why |
|---|---|---|
| Like count (display) | Approximate | "1.2M likes" — rounding hides imprecision |
| View count (display) | Approximate (HyperLogLog) | "500K views" — same reasoning |
| Follower count (display) | Approximate | "650M followers" — rounding |
| Follower count (threshold check) | Approximate is fine | Celebrity threshold is ~500K — ±0.1% doesn't matter |
| Unread notification count (badge) | Eventually exact | Badge showing "5" vs "4" is noticeable — converge quickly |
| Account balance (monetization) | EXACT | Money must be exact. No approximation. |
| Rate limit counter | EXACT | Security-critical. Must not allow bypass via approximation. |
| Double-like prevention | EXACT | User must not be able to like the same post twice |

### The General Principle

**Approximate what humans can't perceive. Be exact where errors have consequences.**

A like count off by 0.1% is invisible in the UI. A payment off by 0.1% is a financial error. The approximation boundary follows the consequence boundary.

---

## 9. Ephemeral + Permanent Content vs Pick One

### The Decision

Instagram supports both **permanent content** (feed posts, Reels) and **ephemeral content** (Stories — 24-hour TTL). This is in contrast to Snapchat (ephemeral-first) and traditional Instagram (permanent-first before Stories).

### The Engineering Cost

```
Permanent content storage:
  • Haystack/f4 (long-term blob storage)
  • No TTL, no cleanup needed
  • CDN caching with long TTLs
  • Straightforward lifecycle

Ephemeral content storage:
  • Cassandra with column TTL (24 hours)
  • S3 lifecycle policies for media cleanup
  • CDN URLs with embedded expiration
  • Seen-state tracking (25B entries/day, also ephemeral)

Supporting BOTH means:
  • Two storage pipelines (permanent + TTL-based)
  • Migration between them (Stories → Highlights)
  • Different caching strategies (Stories need short CDN TTLs)
  • Different deletion semantics (manual delete vs auto-expire)
  • Different notification models (Stories: "new story available" with urgency;
    posts: "liked your post" without urgency)
```

### Why Support Both?

```
Product reality:
  • Users share ~10x more content when it auto-deletes (lower bar for posting)
  • Stories drive daily engagement (500M DAU) more than feed posts
  • But permanent posts are the portfolio — users curate their grid
  • Different use cases: "here's my lunch" (Story) vs "here's my vacation highlight" (post)

If Instagram only supported permanent content:
  • Users would post less (higher bar — permanent is intimidating)
  • Daily engagement would drop significantly

If Instagram only supported ephemeral content:
  • No user profile curation (no permanent grid)
  • No content discovery for older posts (Explore would lose depth)
  • Creators lose their portfolio
```

### Snapchat's Simpler Architecture

Snapchat was built ephemeral-first. Everything expires. This is architecturally simpler:
- One storage model (TTL-based)
- No dual pipeline
- Consistent deletion semantics

But Snapchat added "Memories" (permanent storage) later — acknowledging that some content needs permanence. The dual model seems to be where platforms converge.

---

## 10. Social-Graph Feed + Recommendation Feed vs Pick One

### The Decision

Instagram runs **two fundamentally different content distribution systems**:
1. **Home feed**: Social-graph-based (you see posts from people you follow)
2. **Reels tab / Explore**: Recommendation-based (you see content from anyone, selected by ML)

### The Infrastructure Cost

```
Social-graph distribution:
  • Fan-out infrastructure (Redis inboxes, celebrity routing)
  • Social graph storage and caching (TAO)
  • Feed ranking model (trained on social-graph signals)
  • Following/follower management, notifications

Recommendation distribution:
  • Content indexing pipeline (video understanding, audio fingerprinting)
  • Candidate generation (IG2Vec, two-tower model, FAISS)
  • Multi-stage ranking (distillation → full MTML model)
  • Content embeddings storage (vector database)
  • No fan-out needed — content goes to recommendation index, not follower inboxes

Running BOTH means:
  • Double the ML model infrastructure
  • Double the data pipelines
  • Double the feature engineering
  • Two teams, two codebases, two monitoring dashboards
```

### TikTok's Simplification

TikTok runs ONLY the recommendation path. No fan-out, no social-graph feed, no feed inboxes. Every video goes to the same recommendation pipeline regardless of creator's follower count.

```
TikTok's advantage:
  • One distribution model → simpler infrastructure
  • No fan-out problem → no celebrity threshold
  • Engineering effort concentrated on one system → better recommendations
  • Any creator can go viral → more creator motivation

TikTok's disadvantage:
  • No "friend feed" — can't see only content from people you know
  • Cold start harder — must learn preferences from scratch (no social graph bootstrap)
  • Less personal connection (content from strangers dominates)
```

### Why Instagram Needs Both

```
Instagram's identity is social (follow graph) + discovery (recommendations).

Dropping the social-graph feed would:
  • Alienate existing users who value seeing friends' posts
  • Remove the "personal connection" differentiator vs TikTok
  • Make Instagram just another TikTok clone

Dropping the recommendation feed would:
  • Lose the Reels/TikTok competitive response
  • Limit content discovery to the social graph
  • Reduce engagement (recommendations drive significant session time)

The dual model is expensive but necessary for Instagram's product positioning:
  "Where you connect with friends AND discover new content."
```

### The Honest Assessment

Running two distribution systems is a form of **architectural debt from product evolution**. Instagram was built social-graph-first (2010-2019). When TikTok proved that recommendation-driven distribution is better for engagement and creator growth, Instagram retrofitted a recommendation engine (Reels, 2020) onto a social-graph platform.

TikTok, starting from scratch in 2016, had the luxury of building recommendation-first. Its architecture is cleaner because it only needs to solve one problem well. Instagram's architecture is more complex because it solves two problems adequately.

The question is whether the dual approach converges to TikTok's model over time (social graph becomes less important) or whether the social graph remains a durable differentiator. Instagram's bet — as of 2024 — is that both matter. The infrastructure cost is the price of that bet.
