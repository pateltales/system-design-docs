# System Design Interview Simulation: Design Facebook Reactions/Likes

> **Interviewer:** Principal Engineer (L8), Meta Social Graph Team
> **Candidate Level:** L6 SDE-3 (Senior Software Development Engineer)
> **Duration:** ~60 minutes
> **Date:** February 2026

---

## PHASE 1: Opening & Problem Statement (~2 min)

**Interviewer:**
Hey, welcome. I'm on the Social Graph infrastructure team at Meta. For today's system design, I'd like you to design the **Facebook Reactions system** — the feature that lets users react to posts, comments, and other content with Like, Love, Haha, Wow, Sad, or Angry. Think about everything from tapping the button to showing "Alice, Bob, and 498 others liked your post."

I care about how you handle scale — billions of users, viral content, the whole thing. I'll push on your decisions. Take it away.

---

## PHASE 2: Requirements Gathering & Scoping (~5 min)

**Candidate:**
Thanks! Reactions touch a lot of surfaces — the write path, the read path, notifications, analytics, News Feed ranking. Let me scope this properly before drawing anything.

**Functional Requirements — what operations do we need?**

> "Core user-facing operations:
> - **React** — a user adds a reaction (Like, Love, Haha, Wow, Sad, Angry) to an entity (post, comment, message, story).
> - **Change reaction** — user switches from one type to another (e.g., Like → Love). This is NOT a delete + create — it's an atomic type change.
> - **Remove reaction** — user removes their reaction entirely.
> - **View reaction summary** — for any entity, show total count, breakdown by type, whether I've reacted, and which of my friends reacted.
> - **View who reacted** — paginated list of who reacted with what type.
>
> A critical constraint: **one reaction per user per entity**. If I already 'liked' a post and now tap 'love', the 'like' is replaced by 'love'. This is upsert semantics — the uniqueness constraint on `(userId, entityId)` is fundamental to the data model.
>
> A few clarifying questions:
> - **Which entity types?** Posts, comments, messages, stories — anything else?"

**Interviewer:** "Posts, comments, and stories are the main ones. Messages use a slightly different system. Focus on posts and comments."

> "- **How many reaction types?** Currently 6 (Like, Love, Haha, Wow, Sad, Angry), but Care was added in 2020 during COVID. Can new types be added?"

**Interviewer:** "Yes. The system should support adding new reaction types without a schema migration on a trillion-row table. That's an important extensibility concern."

> "- **Notifications?** When someone reacts to my post, I should get a notification. But if 500 people react in 5 minutes, I shouldn't get 500 notifications."

**Interviewer:** "Exactly. Notification coalescing is a key part of this design. 'Alice, Bob, and 498 others liked your post' — one notification, not 500."

> "- **Does reaction type affect News Feed ranking?** A 'love' is a stronger engagement signal than a 'like'?"

**Interviewer:** "Yes. Reaction events feed into the News Feed ranking algorithm. The type matters — but that's the ranking team's problem. Your system just needs to publish the events."

**Non-Functional Requirements:**

> "Now the critical constraints. This system's interesting because the read path is extraordinarily hot:
>
> | Dimension | Requirement | Rationale |
> |---|---|---|
> | **Scale** | ~3 billion MAU, billions of reactions/day | Facebook's current scale — [3.07B MAU as of 2024](https://www.demandsage.com/facebook-statistics/) |
> | **Read latency** | < 10ms for reaction summary | Every News Feed post render needs reaction counts — this is THE hot path |
> | **Write latency** | < 100ms for adding a reaction | Tapping 'like' should feel instant |
> | **Consistency** | Eventually consistent for counts (seconds of lag OK) | User sees count go from 1200 to 1201 within seconds, not instantly |
> | **Read-your-own-writes** | Must see your own reaction immediately | If I tap 'love', I must see it reflected — no stale state for my own action |
> | **Availability** | 99.99% | Reaction failures degrade the entire Facebook experience |
> | **Read:Write ratio** | 100:1 to 1000:1 | Every post render reads counts; reactions are written far less frequently |
> | **Peak write rate** | 100K+ reactions/sec for viral posts | Celebrity posts can receive millions of reactions in minutes |
> | **Storage** | Trillions of reaction records historically | Every reaction ever made, across billions of entities |
> | **Notification coalescing** | Batch hundreds of reaction events into one notification | Prevent notification spam on popular content |

**Interviewer:**
Good. You've identified the key tension — the read path is the hot path, and viral posts create write hot spots. That's exactly what makes this problem interesting. Let's continue.

---

### L5 vs L6 vs L7 — Phase 2 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Functional Reqs** | Lists react/unreact/view counts | Proactively raises upsert semantics, notification coalescing, extensibility of reaction types | Additionally discusses privacy implications (blocked users), GDPR data deletion, cross-platform consistency |
| **Non-Functional** | Mentions low latency and high availability | Quantifies read:write ratio (100:1 to 1000:1), identifies viral post write spikes, distinguishes count consistency from record consistency | Frames NFRs in terms of user trust (count accuracy), product metrics (engagement signal quality), and cost (storage at trillion-record scale) |
| **Scoping** | Accepts problem as given | Drives clarifying questions — entity types, extensibility, notification semantics | Negotiates scope based on interview time, proposes phased deep dives, identifies which components are most architecturally interesting |

---

## PHASE 3: API Design (~5 min)

**Candidate:**

> "Let me define the core APIs. I'll focus on three: the write path (react), the hot read path (counts), and the notification contract."

#### React API (★ Core Write Path)

> ```
> POST /v1/reactions
> Authorization: Bearer <token>
>
> Request:
> {
>   "entityId": "post_12345",
>   "entityType": "POST",
>   "reactionType": "LOVE"
> }
>
> Response (200 OK):
> {
>   "reactionId": "rxn_67890",
>   "userId": "user_42",         // from auth token, not request body
>   "entityId": "post_12345",
>   "reactionType": "LOVE",
>   "previousReactionType": "LIKE",  // null if new reaction
>   "timestamp": "2026-02-21T10:30:00Z"
> }
> ```
>
> **Key design decisions:**
> - `userId` comes from the auth token, never from the request body — prevents impersonation.
> - The response includes `previousReactionType` so the client knows if this was a new reaction or a type change. This drives the UI animation.
> - The same endpoint handles both new reactions and type changes (upsert semantics). No separate 'update' endpoint — simpler API surface."

#### Remove Reaction API

> ```
> DELETE /v1/reactions
> Authorization: Bearer <token>
>
> Request:
> {
>   "entityId": "post_12345",
>   "entityType": "POST"
> }
>
> Response: 204 No Content
> ```
>
> "No need for a reactionId in the path — the unique constraint is `(userId, entityId)`, and userId comes from the token."

#### Reaction Summary API (★ THE Hot Read Path)

> ```
> GET /v1/entities/{entityId}/reactions/summary
> Authorization: Bearer <token>
>
> Response (200 OK):
> {
>   "entityId": "post_12345",
>   "total": 1596,
>   "counts": {
>     "LIKE": 1200,
>     "LOVE": 340,
>     "HAHA": 56
>   },
>   "topReactionTypes": ["LIKE", "LOVE", "HAHA"],
>   "viewerReaction": "LOVE",
>   "topFriendReactors": [
>     {"userId": "user_99", "name": "Alice", "reactionType": "LOVE"},
>     {"userId": "user_77", "name": "Bob", "reactionType": "LIKE"}
>   ]
> }
> ```
>
> "This is called on **every single post render** in News Feed. Billions of times per day. It must be < 10ms.
>
> **Why this shape?**
> - `counts` only includes types with > 0 reactions (sparse map, not all 6 types)
> - `topReactionTypes` gives the client the top 3 types to show as emoji icons next to the count
> - `viewerReaction` tells the client which reaction button to highlight — avoids a separate API call
> - `topFriendReactors` enables 'Alice, Bob, and 1594 others' — personalized, requires social graph intersection
>
> **Why NOT compute this on the fly?** At Facebook's scale, a post with 1M reactions would require `SELECT COUNT(*) GROUP BY reactionType` on 1M rows — seconds per query. With billions of post renders per day, this is infeasible. **Pre-aggregated counts are mandatory.**"

**Interviewer:**
Good. The `topFriendReactors` field is interesting — that's a social graph query at read time. How expensive is that?

**Candidate:**

> "It's a set intersection: (friends of current user) ∩ (users who reacted to this post). At scale, you can't compute this on every render. It's precomputed or cached. TAO's association queries make this efficient — 'find users who are both my friend AND reacted to this post' is a native graph operation in TAO. But the result is cached per `(viewerId, entityId)` with a short TTL."

> *For the full API reference, see [02-api-contracts.md](02-api-contracts.md).*

---

### L5 vs L6 vs L7 — Phase 3 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **API Design** | CRUD endpoints for reactions | Upsert semantics in a single endpoint, sparse counts map, personalized viewer reaction in summary | Additionally discusses API versioning strategy, backward compatibility for old clients, feature flags for new reaction types |
| **Read Path** | "GET reaction counts" | Explains why pre-aggregated counts are mandatory, quantifies the cost of COUNT(*) at scale | Discusses CDN cacheability of non-personalized vs personalized portions, API response splitting |
| **Social Graph Integration** | Doesn't mention "friends who reacted" | Identifies set intersection problem, suggests caching per (viewerId, entityId) | Discusses TAO's native graph queries, precomputation strategies, staleness tradeoffs |

---

## PHASE 4: High-Level Architecture — Iterative Build-Up (~15 min)

**Candidate:**

> "Let me start with the simplest thing that works, find what breaks, and fix it. This is how I'd actually build this — ship something simple, then evolve."

### Attempt 0: Single Server with a Reactions Table

> "The simplest possible design:
>
> ```
>     Client (mobile/web)
>           │
>           ▼
>   ┌─────────────────────┐
>   │   Single Web Server  │
>   │                      │
>   │   POST /reactions    │
>   │   → INSERT/UPDATE    │
>   │     reactions table  │
>   │                      │
>   │   GET /summary       │
>   │   → SELECT COUNT(*)  │
>   │     GROUP BY type    │
>   │     WHERE entityId=? │
>   │                      │
>   │   ┌──────────────┐   │
>   │   │   MySQL DB   │   │
>   │   │              │   │
>   │   │  reactions(  │   │
>   │   │   id,        │   │
>   │   │   userId,    │   │
>   │   │   entityId,  │   │
>   │   │   type,      │   │
>   │   │   timestamp  │   │
>   │   │  )           │   │
>   │   │  UNIQUE(userId,│  │
>   │   │   entityId)  │   │
>   │   └──────────────┘   │
>   └─────────────────────┘
> ```
>
> - The `UNIQUE(userId, entityId)` index enforces the one-reaction-per-user constraint.
> - `INSERT ... ON DUPLICATE KEY UPDATE` handles the upsert.
> - Counts computed with `SELECT COUNT(*) ... GROUP BY reactionType`.
>
> **This works for a hackathon.** But let me stress-test it."

**Interviewer:**
What breaks first?

**Candidate:**

> "Two things break immediately at any real scale:
>
> 1. **COUNT(*) is O(N) per post render.** A post with 1M reactions takes seconds to count. Every News Feed render needs this for every visible post (10-20 posts). That's 10-20 full table scans per page load. Completely infeasible.
>
> 2. **Single database can't handle the volume.** Billions of reactions stored, billions of reads per day, millions of writes per day. A single MySQL instance handles maybe 10K queries/sec. We need orders of magnitude more.
>
> 3. **No caching.** Every read hits the database. No notifications. No async processing.
>
> The COUNT(*) problem is the most urgent — let me fix that first."

---

### Attempt 1: Pre-Aggregated Counts + Application Cache

**Candidate:**

> "Two changes:
>
> **1. Pre-aggregated counts table:**
> ```sql
> reaction_counts (
>     entityId    BIGINT,
>     reactionType TINYINT,
>     count       INT,
>     PRIMARY KEY (entityId, reactionType)
> )
> ```
>
> When a user reacts, I don't just write the reaction record — I also increment the count:
> ```sql
> -- New reaction
> INSERT INTO reactions (userId, entityId, reactionType, timestamp) ...
>     ON DUPLICATE KEY UPDATE reactionType = 'LOVE';
> UPDATE reaction_counts SET count = count + 1
>     WHERE entityId = ? AND reactionType = 'LOVE';
> -- If changing type, also decrement old:
> UPDATE reaction_counts SET count = count - 1
>     WHERE entityId = ? AND reactionType = 'LIKE';
> ```
>
> Now reads are O(1): `SELECT * FROM reaction_counts WHERE entityId = ?` returns at most 6 rows (one per type).
>
> **2. Application cache (Memcache):**
>
> ```
>     Client
>       │
>       ▼
>   ┌────────────┐
>   │  Web Server │
>   └──────┬─────┘
>          │
>    ┌─────┼──────────────┐
>    │     │              │
>    ▼     ▼              ▼
>  ┌─────────┐    ┌──────────┐
>  │Memcache │    │  MySQL   │
>  │(cache)  │    │(primary) │
>  │         │    │          │
>  │entity:  │    │reactions │
>  │{entityId}│   │reaction_ │
>  │→ summary│    │counts    │
>  └─────────┘    └──────────┘
> ```
>
> Cache the reaction summary (counts + top types) per entity. Cache hit rate for popular posts will be 99%+. TTL of 60 seconds — acceptable staleness for counts.
>
> **Why Memcache and not Redis?** At Facebook's scale, Memcache is the standard caching layer. Facebook published the definitive paper on scaling Memcache (NSDI 2013) — they operate it at a scale of billions of requests/sec across trillions of items. TAO is built on top of MySQL + Memcache."

**Interviewer:**
Better. What's still broken?

**Candidate:**

> "Several things:
>
> 1. **Count and reaction record writes are not atomic.** If the INSERT succeeds but the count UPDATE fails (crash, timeout), counts drift from reality. Over millions of operations, this drift accumulates.
>
> 2. **Single database still can't handle write volume.** We've improved reads with caching, but writes still bottleneck on one MySQL instance.
>
> 3. **No notifications.** No async processing pipeline for notifications, analytics, or News Feed ranking signals.
>
> 4. **No handling of viral posts.** A celebrity post getting 100K+ reactions/sec will overwhelm a single database — even if we somehow scaled reads."

---

### Attempt 2: Sharded Database + Async Event Pipeline

**Candidate:**

> "Two major changes:
>
> **1. Shard the database by entityId:**
>
> ```
>   Web Server Fleet (stateless)
>          │
>          │  hash(entityId) → shard
>          │
>   ┌──────┼──────┬──────────────┐
>   │      │      │              │
>   ▼      ▼      ▼              ▼
> ┌──────┐ ┌──────┐    ┌──────┐
> │Shard │ │Shard │ ...│Shard │
> │  0   │ │  1   │    │  N   │
> └──────┘ └──────┘    └──────┘
> ```
>
> All reactions for a given entity land on the same shard. This is deliberate:
> - The hot path is 'get counts for this entity' — entity-sharding makes this a single-shard query
> - Alternative: shard by userId (all of a user's reactions on one shard) — optimizes 'show me everything I've liked' but makes the hot path require a scatter-gather across all shards
> - The read path (News Feed render) is 100-1000x hotter than the user-history path, so we shard by entity
>
> **2. Async event pipeline (Kafka):**
>
> ```
>   Client → API Server → MySQL (reaction record + counts)
>                │
>                ▼
>            ┌───────┐
>            │ Kafka │
>            │ Topic: │
>            │reaction│
>            │-events │
>            └───┬───┘
>                │
>        ┌───────┼────────┬──────────┐
>        ▼       ▼        ▼          ▼
>   ┌────────┐┌──────┐┌─────────┐┌────────┐
>   │Cache   ││Notif.││NewsFeed ││Analyt. │
>   │Invalid.││Pipe  ││Ranking  ││Pipeline│
>   └────────┘└──────┘└─────────┘└────────┘
> ```
>
> After writing the reaction to MySQL, publish an event to Kafka:
> ```json
> {
>   "userId": "user_42",
>   "entityId": "post_12345",
>   "entityOwnerId": "user_1",
>   "reactionType": "LOVE",
>   "previousReactionType": "LIKE",
>   "action": "CHANGE",
>   "timestamp": "2026-02-21T10:30:00Z"
> }
> ```
>
> Kafka consumers handle:
> - **Cache invalidation**: Invalidate/update cached reaction summaries
> - **Notification pipeline**: Coalesce reaction events, generate batched notifications
> - **News Feed ranking**: Update engagement signals for the post
> - **Analytics**: Trending detection, sentiment analysis
>
> The write path returns success to the client as soon as MySQL is written. All downstream processing is async."

**Interviewer:**
Good separation. What's still broken?

**Candidate:**

> "Three things:
>
> 1. **Viral posts still create hot shards.** All reactions for entity X go to shard S. If X is a celebrity post receiving 167K reactions/sec (100M followers, 10% react in first hour), shard S gets crushed. A single MySQL shard handles ~10K writes/sec.
>
> 2. **Count drift.** The reaction record and count update are on the same shard (good), but they're still two separate SQL statements. In a crash between them, counts drift. No reconciliation mechanism.
>
> 3. **No cross-region support.** Single-region architecture. Users worldwide hit one datacenter. Latency for users far from the datacenter is poor."

---

### Attempt 3: Hot-Key Handling — Sharded Counters + Write Buffering

**Candidate:**

> "The viral post problem is the hardest scaling challenge. Let me do the math:
>
> **Scale math:**
> - Celebrity with 100M followers
> - Post goes viral, 10% react in first hour
> - 10M reactions / 3600 seconds = **~2,800 reactions/sec** (sustained average)
> - But reactions are bursty — within the first 10 minutes: maybe 3M reactions / 600s = **5,000 reactions/sec**
> - A truly viral moment (announcement, controversy): can spike to **100K+ reactions/sec**
> - Single MySQL shard: ~10K writes/sec capacity
>
> We need to distribute the write load for hot entities. Three techniques:
>
> **1. Sharded Counters:**
>
> Instead of one counter row per entity:
> ```
> reaction_counts: (entityId=X, type=LIKE, count=1200)
> ```
>
> Maintain N counter shards:
> ```
> reaction_counts_sharded:
>   (entityId=X, shardId=0,   type=LIKE, count=5)
>   (entityId=X, shardId=1,   type=LIKE, count=4)
>   ...
>   (entityId=X, shardId=255, type=LIKE, count=6)
>
> Total LIKE count = SUM of all 256 shards = 1200
> ```
>
> Each write goes to a random shard (`shardId = hash(userId) % 256`). With 256 shards and 100K writes/sec, each shard gets ~390 writes/sec — well within MySQL capacity.
>
> Trade-off: reads must SUM 256 rows instead of reading 1 row. But:
> - The SUM is computed once and cached (not on every read)
> - 256 rows × 6 types = 1536 rows — trivial for MySQL
> - The cache TTL absorbs the read amplification
>
> **2. Write Buffering:**
>
> For hot entities, buffer individual reactions in Redis for 1-5 seconds, then batch-write to MySQL:
>
> ```
>   Reaction → Redis (buffer) → batch every 5s → MySQL
>
>   Redis: HINCRBY entity:X:buffer:LIKE 1
>   Every 5 seconds: flush buffer to MySQL sharded counters
> ```
>
> Risk: if Redis crashes, buffered count increments are lost. Acceptable for counts (we have reconciliation). Individual reaction records are written directly to MySQL (or Kafka → MySQL) — they're not buffered.
>
> **3. Adaptive Notification Coalescing:**
>
> For viral posts with high reaction rate, extend the notification coalescing window:
> - Normal post (< 10 reactions/min): 30-second coalescing window
> - Popular post (10-100 reactions/min): 2-minute window
> - Viral post (100+ reactions/min): 10-minute window
>
> This prevents notification spam for the post owner.
>
> **Updated Architecture:**
>
> ```
>   Client → API Server → Hot-Key Detector
>                               │
>                    ┌──────────┼──────────┐
>                    │ Normal   │ Hot      │
>                    ▼          ▼          │
>              ┌──────────┐ ┌─────────┐   │
>              │ MySQL    │ │ Redis   │   │
>              │ (direct  │ │ (buffer)│   │
>              │  write)  │ │ → batch │   │
>              │          │ │ → MySQL │   │
>              └──────────┘ └─────────┘   │
>                    │          │          │
>                    ▼          ▼          │
>                  Kafka → Consumers      │
>                    │                    │
>              ┌─────┼─────┐             │
>              ▼     ▼     ▼             │
>          Cache  Notif  Analytics       │
>          Inv.   Pipe   Pipeline        │
>          (with  (adaptive              │
>          sharded coalescing)           │
>          counter                       │
>          SUM)                          │
> ```"

**Interviewer:**
How do you detect a hot key?

**Candidate:**

> "Monitor reaction rate per entityId in a sliding window:
> - Every API server maintains a local counter per entityId (in-memory, approximate)
> - If rate exceeds threshold (e.g., 1000 reactions/sec), mark entity as 'hot'
> - Hot entity routing: activate sharded counters + write buffering
> - When rate drops below threshold for 5 minutes, deactivate (return to normal path)
>
> For a more principled approach, use a **Count-Min Sketch** — a probabilistic data structure that estimates frequency of events in a stream. It uses sublinear memory and is well-suited for finding heavy hitters (hot keys) in a high-volume event stream."

**Interviewer:**
What's still broken?

**Candidate:**

> "Three things remain:
>
> 1. **Single-region architecture.** Users worldwide hit one datacenter. No geographic distribution.
> 2. **Cache invalidation races.** Multiple concurrent reactions to the same entity can create race conditions in cache invalidation.
> 3. **'Friends who reacted' personalization is expensive.** The set intersection (my friends ∩ entity's reactors) isn't addressed yet.
> 4. **No count reconciliation.** Counts can drift, and there's no mechanism to correct them."

---

### Attempt 4: Production Hardening — TAO, Multi-Region, Monitoring

**Candidate:**

> "This is where we go from 'works at scale' to 'runs in production at Facebook.' Four changes:
>
> **1. TAO — Facebook's Graph Store:**
>
> Instead of managing raw MySQL + Memcache ourselves, we use TAO (published at USENIX ATC 2013). TAO is a distributed graph store purpose-built for the social graph:
>
> ```
>   Client → API Server → TAO
>                          │
>                    ┌─────┼─────┐
>                    ▼     ▼     ▼
>               Follower Follower Follower   (client-facing cache tier)
>               Cache    Cache    Cache
>                    │     │     │
>                    ▼     ▼     ▼
>               Leader  Leader  Leader       (consistency-maintaining tier)
>               Cache   Cache   Cache
>                    │     │     │
>                    ▼     ▼     ▼
>               MySQL   MySQL   MySQL        (persistent storage)
>               Shard   Shard   Shard
> ```
>
> **TAO's key properties:**
> - **Objects** (posts, users) and **Associations** (reactions, friendships) are first-class concepts
> - **Two-tier caching**: Follower caches handle client requests, Leader caches maintain consistency
> - **Write-through with lease-based invalidation**: When a reaction is written, the cache is updated through the leader chain. Leases prevent thundering herd — only one request fills a cold cache entry.
> - **Cross-region replication**: One region is the leader for writes. All regions serve reads. Async replication with ~1-5 seconds lag.
> - **Scale**: Handles a billion reads/sec and millions of writes/sec across hundreds of thousands of shards.
> - **Count queries**: TAO tracks association counts natively — constant-time aggregation.
>
> This replaces our hand-rolled MySQL + Memcache + sharding layer with a battle-tested platform.
>
> **2. Multi-Region Replication:**
>
> ```
>   US-East (Leader)          EU-West (Follower)        AP-South (Follower)
>   ┌───────────────┐        ┌──────────────┐          ┌──────────────┐
>   │ TAO Leader    │ ──────>│ TAO Follower │ ────────>│ TAO Follower │
>   │ Caches        │ async  │ Caches       │ async    │ Caches       │
>   │               │ repl.  │              │ repl.    │              │
>   │ MySQL Primary │        │ MySQL Replica│          │ MySQL Replica│
>   └───────────────┘        └──────────────┘          └──────────────┘
>         ▲                         │                         │
>         │ writes                  │ reads                   │ reads
>         │                         ▼                         ▼
>      Users near US-East     Users near EU-West        Users near AP-South
> ```
>
> - All writes go to the leader region
> - Reads served from the nearest region (low latency)
> - Replication lag: ~1-5 seconds cross-region
> - A user in Europe might see a slightly different count than a user in the US for a few seconds — acceptable
> - **Read-your-own-writes**: After reacting, the user's subsequent reads are routed to the leader region (sticky routing) until replication catches up
>
> **3. Periodic Count Reconciliation:**
>
> A background job that runs hourly for active entities:
> ```sql
> -- For each active entity, recount from individual records
> SELECT reactionType, COUNT(*) as actual_count
> FROM reactions
> WHERE entityId = ?
> GROUP BY reactionType;
>
> -- Compare with pre-aggregated counts, correct if drifted
> UPDATE reaction_counts SET count = actual_count
> WHERE entityId = ? AND reactionType = ? AND count != actual_count;
> ```
>
> This is the safety net — even if individual increments/decrements were lost, the reconciliation job corrects the drift. The reconciliation job only touches entities that had recent activity (hot entities reconciled more frequently).
>
> **4. Privacy-Aware Reads + Monitoring:**
>
> - **Privacy overlay**: Filter reaction lists by viewer's block list. If Alice blocks Bob, Bob's reaction doesn't appear in Alice's 'who reacted' view — but it still counts in the total.
> - **Monitoring**: Reaction rate per second (global/regional/per-entity), count drift detection, cache hit rate (target: 99%+), write/read latency p50/p95/p99, notification delivery latency, hot-key detection, Kafka consumer lag."

---

#### Architecture Evolution Table

| Component | Attempt 0 | Attempt 1 | Attempt 2 | Attempt 3 | Attempt 4 |
|---|---|---|---|---|---|
| **Data Store** | Single MySQL | Single MySQL | Sharded MySQL (by entityId) | Sharded MySQL + Redis write buffer | TAO (MySQL + Memcache graph store) |
| **Counts** | COUNT(*) on every read | Pre-aggregated counts table | Pre-aggregated counts | Sharded counters for hot keys | TAO native association counts |
| **Caching** | None | Memcache | Memcache | Memcache + Redis buffer | TAO two-tier cache (Follower → Leader) |
| **Async Processing** | None | None | Kafka event pipeline | Kafka + adaptive coalescing | Kafka + coalescing + analytics |
| **Hot-Key Handling** | N/A | N/A | N/A | Sharded counters + write buffering + hot-key detection | TAO sharding + sharded counters |
| **Multi-Region** | N/A | N/A | N/A | N/A | Leader-follower replication, 1-5s lag |
| **Consistency** | Strong (single DB) | Eventual (cache TTL) | Eventual (cache + async) | Eventual + reconciliation | Eventually consistent counts, read-your-own-writes |
| **Notifications** | None | None | Kafka → notification consumer | Adaptive coalescing by reaction rate | Full pipeline with suppression + dedup |

---

### L5 vs L6 vs L7 — Phase 4 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Architecture evolution** | Jumps to sharded DB + cache | Builds iteratively — single server → pre-aggregated counts → sharding → hot-key handling → TAO | Same iterative approach but frames each evolution in terms of cost, risk, and organizational capability |
| **Hot-key handling** | "Add more shards" | Sharded counters with concrete math (256 shards, 390 writes/sec each), write buffering, hot-key detection via Count-Min Sketch | Additionally discusses automatic hot/cold path routing, cost of false positives in hot-key detection, operational runbooks |
| **Consistency model** | "Use eventual consistency" | Distinguishes record consistency from count consistency, proposes reconciliation job | Discusses reconciliation frequency tuning, count drift monitoring with alerting, cost of reconciliation at scale |
| **Multi-region** | "Replicate to other datacenters" | Explains leader-follower replication with concrete lag numbers, read-your-own-writes via sticky routing | Discusses region failover (leader election), blast radius of region outage, data sovereignty constraints |

---

## PHASE 5: Deep Dive — Write Path (~8 min)

**Interviewer:**
Let's go deep on the write path. Walk me through exactly what happens when a user taps "Love" on a post they previously "Liked."

**Candidate:**

> "This is the most complex write scenario — a reaction type change. Let me trace it step by step:
>
> ```
> User taps "Love" on post_12345 (previously "Liked")
>
> 1. Client → API Server
>    POST /v1/reactions {entityId: "post_12345", reactionType: "LOVE"}
>    Auth token identifies userId = user_42
>
> 2. API Server: Validate
>    - Entity exists? ✓
>    - User authorized to react? ✓ (public post)
>    - Rate limit check: user_42 < 100 reactions/min? ✓
>
> 3. API Server → TAO: Upsert reaction
>    TAO (via Leader cache → MySQL):
>    a. Read existing: SELECT * FROM reactions
>       WHERE userId='user_42' AND entityId='post_12345'
>       → Found: reactionType='LIKE'
>
>    b. Update: UPDATE reactions SET reactionType='LOVE',
>       timestamp=NOW() WHERE userId='user_42' AND entityId='post_12345'
>
>    c. Decrement old: UPDATE reaction_counts
>       SET count=count-1 WHERE entityId='post_12345' AND reactionType='LIKE'
>
>    d. Increment new: UPDATE reaction_counts
>       SET count=count+1 WHERE entityId='post_12345' AND reactionType='LOVE'
>
>    (Steps b, c, d ideally in one transaction on the same shard)
>
> 4. TAO: Invalidate/update caches
>    Leader cache updated → Follower caches invalidated
>    (lease-based invalidation prevents thundering herd)
>
> 5. API Server → Kafka: Publish event
>    Topic: reaction-events
>    {userId, entityId, entityOwnerId, reactionType: "LOVE",
>     previousReactionType: "LIKE", action: "CHANGE", timestamp}
>
> 6. API Server → Client: 200 OK
>    {reactionId, reactionType: "LOVE", previousReactionType: "LIKE"}
>    (Client updates UI immediately — optimistic update)
>
> 7. Async (Kafka consumers):
>    a. Cache consumer: Refresh cached reaction summary for post_12345
>    b. Notification consumer: Buffer event for coalescing
>    c. News Feed consumer: Update engagement signal
>       (LOVE is stronger signal than LIKE for ranking)
>    d. Analytics consumer: Update reaction type distribution
> ```
>
> **Atomicity concern:** Steps 3b, 3c, 3d should be in one transaction. If a crash happens between 3c (decrement LIKE) and 3d (increment LOVE), the LIKE count is too low and the LOVE count is too low — both counts are wrong. On a single shard (entity-sharded), these can be in one MySQL transaction. The reconciliation job catches any drift."

**Interviewer:**
What about idempotency? What if the client retries?

**Candidate:**

> "The upsert semantics make this naturally idempotent:
> - If the client retries `POST {entityId, reactionType: LOVE}` and the previous attempt already wrote LOVE, the upsert is a no-op (same type, no change).
> - If the retry arrives but the first attempt hasn't committed yet, the unique constraint on `(userId, entityId)` serializes the writes — one succeeds, the other is a no-op.
> - We include a request ID in the Kafka event for downstream deduplication — consumers check `(requestId)` before processing to prevent double-counting in notifications or analytics."

> *For the full write path deep dive, see [04-write-path.md](04-write-path.md).*

---

### L5 vs L6 vs L7 — Phase 5 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Write flow** | "Insert into database, update cache" | Step-by-step trace with SQL, transaction boundaries, async event publishing | Additionally discusses write amplification, WAL ordering, impact of write failures on downstream consumers |
| **Atomicity** | Doesn't mention | Identifies the decrement/increment atomicity risk, proposes single-shard transaction | Discusses two-phase commit alternatives, saga pattern for cross-shard reactions, compensating transactions |
| **Idempotency** | "Use unique constraints" | Explains how upsert semantics provide natural idempotency, adds request ID for downstream dedup | Discusses exactly-once delivery semantics in Kafka, transactional outbox pattern |

---

## PHASE 6: Deep Dive — Read Path & Caching (~8 min)

**Interviewer:**
The read path is the hot path. Walk me through how a News Feed render gets reaction data for 20 posts.

**Candidate:**

> "A user scrolls their News Feed and sees 20 posts. For each post, the client needs the reaction summary. That's 20 calls to the reaction summary API — but we can batch this.
>
> **Batched Read Flow:**
>
> ```
> Client: GET /v1/entities/batch/reactions/summary
>         Body: {entityIds: [post_1, post_2, ..., post_20]}
>
> Server (for each entityId):
>   1. Check L3 cache (TAO Follower cache)
>      Key: entity:{entityId}:reaction_summary
>      → Cache HIT (99%+ of the time for popular posts)
>      → Return cached summary
>
>   2. Cache MISS → Check L3 Leader cache
>      → HIT → Return, update Follower cache
>
>   3. Leader cache MISS → MySQL query
>      SELECT * FROM reaction_counts WHERE entityId = ?
>      → Populate Leader cache → Follower cache → Return
>
>   Personalization (per viewer):
>   4. Check viewer's reaction:
>      TAO lookup: assoc_get(user_42, REACTION, post_X)
>      → "LOVE" (cached in TAO)
>
>   5. Friends who reacted:
>      TAO intersection: friends(user_42) ∩ reactors(post_X)
>      → [Alice, Bob] (cached per viewer+entity)
>
>   6. Assemble response:
>      {total: 1596, counts: {...}, viewerReaction: "LOVE",
>       topFriendReactors: [Alice, Bob]}
> ```
>
> **Multi-layer caching:**
>
> | Layer | What's Cached | TTL | Hit Rate | Latency |
> |---|---|---|---|---|
> | **L1: Client** | Reaction summaries for on-screen posts | Until scroll away | ~50% (same posts visible) | 0ms |
> | **L2: TAO Follower** | Reaction summaries per entity | 60s | 99%+ | < 1ms |
> | **L3: TAO Leader** | Reaction summaries per entity | 300s | 99.9%+ | < 5ms |
> | **L4: MySQL** | Source of truth (reaction_counts table) | N/A | Fallback only | 5-50ms |
>
> With a 99%+ cache hit rate at L2, less than 1% of reads touch MySQL. At a billion reads/sec, that's still ~10M MySQL reads/sec — spread across hundreds of thousands of shards, that's manageable.
>
> **Cache invalidation (TAO's lease-based approach):**
>
> When a reaction is written:
> 1. TAO Leader cache is updated synchronously (write-through)
> 2. Leader sends invalidation to all Follower caches in the region
> 3. Cross-region: `mcsqueal` daemons tail the MySQL binlog and broadcast invalidations to other regions' TAO caches
>
> **Thundering herd prevention:** When a cache entry expires or is invalidated:
> - Only ONE Follower gets a 'lease' to fill the cache entry
> - All other Followers wait for the lease holder to populate the cache
> - Without leases: 1000 concurrent requests for a cold entry → 1000 MySQL queries → DB overwhelmed
> - With leases: 1000 requests → 1 MySQL query → 999 served from the freshly populated cache"

**Interviewer:**
What about the `topFriendReactors` field — how do you make that fast?

**Candidate:**

> "The 'Alice, Bob, and 1594 others' display requires knowing which of my friends reacted. This is a social graph intersection: `friends(viewer) ∩ reactors(entity)`.
>
> **Why it's expensive:** I might have 1000 friends. The post might have 100K reactors. Intersecting two sets of 1000 and 100K is non-trivial at read time.
>
> **TAO's approach:** TAO stores associations (friendships, reactions) as edges in a graph. The query 'find users who are both my friend AND reacted to this post' is a native graph operation — TAO can answer this by walking the graph without loading full sets into memory.
>
> **Caching:** The result is cached per `(viewerId, entityId)` with a short TTL (~30-60 seconds). The friend list changes rarely, and the top 2-3 friend reactors are stable enough to cache.
>
> **Optimization:** We only need the top 2-3 names for the display. TAO can return early after finding 2-3 matches. We don't need to enumerate all friend reactors — just the most relevant ones (close friends first, then frequent interactions)."

> *For the full read path deep dive, see [05-read-path.md](05-read-path.md).*

---

### L5 vs L6 vs L7 — Phase 6 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Caching strategy** | "Cache reaction counts" | Multi-layer caching with hit rates, TTLs, and latency per layer. Explains lease-based invalidation for thundering herd prevention | Discusses cache warming strategies, negative caching (empty results), cache stampede during major events |
| **Cache invalidation** | "Invalidate on write" | Write-through at Leader, async invalidation to Followers, cross-region via mcsqueal | Discusses consistency windows during region failover, cache coherence protocols, cost of invalidation broadcast |
| **Personalization** | Doesn't mention | Identifies set intersection problem, explains TAO's graph query approach, caching per (viewer, entity) | Discusses precomputation strategies for popular posts, privacy-aware caching (blocked users), cost of personalization at scale |

---

## PHASE 7: Deep Dive — Notification Coalescing (~8 min)

**Interviewer:**
Let's talk about the notification pipeline. If 500 people react to my post in 5 minutes, what happens?

**Candidate:**

> "This is the coalescing problem. The goal: turn 500 individual reaction events into one notification: 'Alice, Bob, and 498 others reacted to your post.'
>
> **Pipeline Architecture:**
>
> ```
> Kafka (reaction-events topic)
>         │
>         ▼
> ┌───────────────────────┐
> │  Coalescing Service   │
> │                       │
> │  Per (entityId,       │
> │       ownerId) buffer:│
> │                       │
> │  Buffer: {            │
> │    reactors: [        │
> │      user_99 (Alice), │
> │      user_77 (Bob),   │
> │      ...498 more      │
> │    ],                 │
> │    reactionTypes: {   │
> │      LIKE: 400,       │
> │      LOVE: 80,        │
> │      HAHA: 20         │
> │    },                 │
> │    windowStart: T0,   │
> │    windowEnd: T0+30s  │
> │  }                    │
> │                       │
> │  When window expires: │
> │  → emit coalesced     │
> │    notification       │
> └───────────┬───────────┘
>             │
>             ▼
> ┌───────────────────────┐
> │  Notification Service │
> │                       │
> │  Render: "Alice, Bob, │
> │  and 498 others       │
> │  reacted to your post"│
> │                       │
> │  Channels:            │
> │  - Push (APNs/FCM)    │
> │  - In-app badge       │
> │  - Email digest       │
> └───────────────────────┘
> ```
>
> **Coalescing window logic:**
>
> 1. First reaction event arrives for (entity_X, owner_Y)
> 2. Start a timer: 30 seconds
> 3. All subsequent reaction events for the same (entity, owner) pair are accumulated in the buffer
> 4. When the timer fires, generate one coalesced notification with all accumulated reactors
> 5. If more reactions arrive after the notification is sent, start a new window
>
> **Adaptive window based on reaction rate:**
>
> | Reaction Rate | Window Duration | Rationale |
> |---|---|---|
> | < 10/min (normal post) | 30 seconds | User wants prompt notification |
> | 10-100/min (popular) | 2 minutes | Moderate coalescing |
> | 100-1000/min (viral) | 10 minutes | Heavy coalescing to prevent spam |
> | > 1000/min (mega-viral) | 30 minutes | User is probably already watching |
>
> **Name selection for the notification text:**
>
> 'Alice, Bob, and 498 others' — how to pick which names to show?
> 1. **Close friends** of the post owner (strongest social signal)
> 2. **Frequent interactions** (people the owner messages/comments with often)
> 3. **Most recent reactors** (tiebreaker)
>
> This requires a social graph lookup at notification generation time, which is why we do it in the coalescing service (async), not in the write path.
>
> **Deduplication:** If a previous notification already exists for this entity ('Alice and 48 others'), the new notification **updates** the existing one in-place. The notification ID is keyed by `(entityId, ownerId, notificationType)` — so there's only ever one active reaction notification per post.
>
> **Suppression rules:**
> - Don't notify for reactions on posts older than 30 days
> - Don't notify if the owner muted the post
> - Don't notify if the owner disabled reaction notifications
> - Don't notify for reactions from blocked users
> - Don't notify the owner for their own reaction (reacting to your own post)"

**Interviewer:**
What happens if the coalescing service crashes mid-window?

**Candidate:**

> "Good failure mode to consider:
>
> 1. The coalescing service maintains in-memory buffers — if it crashes, buffered events are lost.
> 2. **Mitigation:** Kafka retains the events. On restart, the consumer replays events from the last committed offset.
> 3. **Problem:** Replayed events might generate duplicate notifications if the window had already emitted before the crash.
> 4. **Solution:** The notification service checks a `lastNotifiedTimestamp` per (entityId, ownerId). If the replayed events are older than the last notification, they're deduplicated.
> 5. **Alternative:** Back the coalescing buffers with Redis (persistent). On restart, reload buffers from Redis instead of replaying Kafka. This is more robust but adds Redis as a dependency."

> *For the full notification pipeline deep dive, see [06-notification-pipeline.md](06-notification-pipeline.md).*

---

### L5 vs L6 vs L7 — Phase 7 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Coalescing** | "Batch notifications together" | Adaptive coalescing windows based on reaction rate, concrete window durations, name selection priority | Discusses notification quality metrics (open rate, dismiss rate), A/B testing window durations, ML-based personalization of notification content |
| **Failure handling** | Doesn't mention | Identifies crash recovery via Kafka replay + deduplication, Redis-backed buffers | Discusses exactly-once notification delivery, idempotent notification updates, monitoring notification delivery latency |
| **Suppression** | Doesn't mention | Lists suppression rules (old posts, muted, blocked) | Discusses notification fatigue modeling, user-level notification budgets, suppression impact on engagement metrics |

---

## PHASE 8: Deep Dive — Extensibility & Reaction Types (~5 min)

**Interviewer:**
You mentioned new reaction types should be addable without schema migration. How?

**Candidate:**

> "Facebook started with just 'Like' in 2009. In February 2016, they added 5 more types (Love, Haha, Wow, Sad, Angry). 'Care' was added in 2020 during COVID. 'Yay' was tested but removed because it didn't translate well globally.
>
> **The extensibility challenge:** If reaction types are an ENUM column, adding a new type requires `ALTER TABLE` on a table with trillions of rows. At Facebook's scale, that could take days or weeks of migration. Unacceptable.
>
> **Solution: Integer type IDs with a registry:**
>
> ```sql
> -- Reaction types registry (small table, cached)
> reaction_type_registry (
>     typeId      TINYINT PRIMARY KEY,  -- 1, 2, 3, 4, 5, 6, 7
>     typeName    VARCHAR(16),           -- 'LIKE', 'LOVE', etc.
>     emoji       VARCHAR(8),            -- '👍', '❤️', etc.
>     isActive    BOOLEAN,               -- can new reactions use this type?
>     addedDate   DATE,
>     availableFrom DATE NULL,           -- for temporary types
>     availableTo   DATE NULL            -- for temporary types
> )
>
> -- Reaction records use integer type IDs
> reactions (
>     userId      BIGINT,
>     entityId    BIGINT,
>     reactionType TINYINT,  -- FK to registry, NOT an enum
>     timestamp   TIMESTAMP,
>     UNIQUE (userId, entityId)
> )
> ```
>
> Adding a new reaction type = `INSERT INTO reaction_type_registry`. **No schema migration.** No downtime. The registry is tiny (< 20 rows) and cached everywhere.
>
> **Client compatibility:** When a new type is added, old clients that haven't updated must handle it:
> - Server sends both `typeId` and a fallback `displayName` in API responses
> - Old clients render unknown types as a generic 'reacted' label
> - Feature flags control rollout: new type available to 1% → 10% → 100% of users
> - Client polls the type registry on app startup or receives it via push config
>
> **Temporary reaction types** (Pride Month, COVID Care): Set `availableFrom` and `availableTo` dates. After `availableTo`, the type is disabled for new reactions but existing reactions remain visible. Users who already reacted with 'Care' still see it — we never delete user data retroactively."

> *For the full reaction types deep dive, see [08-reaction-types-and-extensibility.md](08-reaction-types-and-extensibility.md).*

---

### L5 vs L6 vs L7 — Phase 8 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Extensibility** | "Use a string column instead of ENUM" | Integer type IDs with a registry table. Explains why ALTER TABLE on trillions of rows is infeasible. Feature flags for rollout. | Discusses schema evolution strategies at scale, online DDL tools (pt-online-schema-change, gh-ost), zero-downtime migrations |
| **Temporary types** | Doesn't mention | Explains availableFrom/availableTo dates, preserving existing reactions | Discusses A/B testing new reaction types, measuring impact on engagement signals, rollback strategy if new type hurts engagement |
| **Client compat** | Doesn't mention | Fallback displayName for old clients, feature flag rollout | Discusses forced vs graceful client updates, API versioning, backward-compatible response evolution |

---

## PHASE 9: Deep Dive — Consistency & Edge Cases (~5 min)

**Interviewer:**
Tell me about consistency guarantees. What happens with race conditions?

**Candidate:**

> "Let me distinguish three levels of consistency in this system:
>
> | Data | Consistency Level | Why |
> |---|---|---|
> | **Individual reaction record** | Strong (unique constraint) | `(userId, entityId)` uniqueness enforced by DB. No double reactions. |
> | **Reaction counts** | Eventually consistent (~5-10s) | Pre-aggregated counts may lag by a few seconds. Acceptable for display. |
> | **Notification delivery** | At-least-once with dedup | Kafka provides at-least-once. Deduplication at notification service. |
>
> **Race conditions I've thought about:**
>
> 1. **Double-tap (rapid Like-Like):** User taps Like twice in quick succession. Both requests hit the server. The unique constraint on `(userId, entityId)` ensures the second INSERT is a no-op (`ON DUPLICATE KEY UPDATE` with same type = no change). Count is incremented only once.
>
> 2. **Like then Unlike (rapid toggle):** User likes then immediately unlikes. Two requests in flight. With upsert semantics and unique constraint, the final state depends on which commits last. If Unlike commits last → reaction deleted, count decremented. If Like commits last (unlikely but possible) → reaction exists. Either way, the state is consistent — no orphaned counts.
>
> 3. **Type change during unreact:** User changes Like → Love while another request to unreact is in flight. This is the trickiest case. Solution: optimistic locking with a version field on the reaction record. Each mutation checks the version — if it's changed since read, retry.
>
> 4. **Count drift over time:** Counts can drift from reality due to failed transactions, partial writes, or bugs. **Reconciliation job** runs hourly for active entities, daily for cold ones. It recounts from individual records and corrects the pre-aggregated counts. Monitoring alerts if drift exceeds a threshold (e.g., > 1% difference).
>
> **Deleted entities:** When a post is deleted:
> - Reactions become orphaned. We don't cascade-delete millions of reaction records synchronously — that would lock the shard.
> - Instead: soft-delete the post (mark as deleted). Reactions become invisible but still exist in DB.
> - Background GC job cleans up orphaned reactions asynchronously (hours/days later).
> - Pre-aggregated counts for the deleted entity are zeroed immediately."

> *For the full consistency deep dive, see [09-consistency-and-accuracy.md](09-consistency-and-accuracy.md).*

---

### L5 vs L6 vs L7 — Phase 9 Rubric

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Consistency model** | "Eventually consistent" | Distinguishes record, count, and notification consistency. Explains why each level is appropriate. | Discusses formal consistency models (linearizability vs causal vs eventual), references TAO's consistency guarantees from the USENIX paper |
| **Race conditions** | "Use locks" | Enumerates specific race conditions (double-tap, toggle, concurrent type change) with solutions for each | Discusses distributed deadlock detection, optimistic vs pessimistic concurrency control tradeoffs, CAS operations in TAO |
| **Data cleanup** | Doesn't mention | Soft delete + async GC for orphaned reactions | Discusses GDPR compliance (right to deletion), data retention policies, impact of GC on shard performance |

---

## PHASE 10: Wrap-Up & Summary (~3 min)

**Interviewer:**
We're running low on time. Summarize your design and tell me — what keeps you up at night?

**Candidate:**

> "**Design evolution — how we got here:**
>
> | Component | Started With | Evolved To | Why |
> |---|---|---|---|
> | **Architecture** | Single MySQL server | TAO-based distributed graph store | Single DB can't handle billions of reads/sec or trillions of records |
> | **Counts** | SELECT COUNT(*) per render | Pre-aggregated counts + sharded counters for hot keys | O(N) per render is infeasible; hot keys overwhelm single counter rows |
> | **Caching** | None | TAO two-tier (Follower → Leader) with lease-based invalidation | 99%+ cache hit rate needed; leases prevent thundering herd |
> | **Async pipeline** | None | Kafka → notification coalescing + analytics + News Feed ranking | Decouple write latency from downstream processing |
> | **Multi-region** | Single datacenter | Leader-follower replication, ~1-5s lag | Global user base needs low-latency reads from nearest region |
> | **Consistency** | Strong (single DB) | Eventually consistent counts + read-your-own-writes | Strong consistency at this scale would add 10-50ms per write |
> | **Extensibility** | ENUM column | Integer type IDs + registry table | Adding new reaction types without schema migration on trillions of rows |
>
> **Final Architecture:**
>
> ```
>                        ┌──────────────────────┐
>                        │    Clients (3B MAU)   │
>                        └──────────┬───────────┘
>                                   │
>                        ┌──────────▼───────────┐
>                        │   API Gateway         │
>                        │   (Auth, Rate Limit)   │
>                        └──────────┬───────────┘
>                                   │
>                    ┌──────────────┼──────────────┐
>                    │              │              │
>         ┌──────────▼──────┐  ┌───▼───┐  ┌──────▼──────────┐
>         │  TAO (Graph     │  │ Kafka │  │ Hot-Key         │
>         │  Store)         │  │       │  │ Detector        │
>         │                 │  └───┬───┘  │ (Count-Min      │
>         │  Follower →     │      │      │  Sketch)        │
>         │  Leader →       │      │      └────────┬────────┘
>         │  MySQL          │      │               │
>         │                 │  ┌───┼───┬───┐       │
>         │  Objects &      │  │   │   │   │   ┌───▼──────┐
>         │  Associations   │  ▼   ▼   ▼   ▼   │ Sharded  │
>         │  (reactions)    │ Cache Notif Feed  │ Counters │
>         │                 │ Inv. Pipe  Rank.  │ + Redis  │
>         │  Pre-aggregated │      │            │ Buffer   │
>         │  counts         │      ▼            └──────────┘
>         │                 │ Coalescing
>         │  Sharded        │ Service
>         │  counters       │      │
>         │  (hot keys)     │      ▼
>         └─────────────────┘  Push/In-app/Email
> ```
>
> **What keeps me up at night:**
>
> 1. **The celebrity post thunderstorm.** A mega-celebrity (500M+ followers) posts something controversial. Millions of reactions per minute. Even with sharded counters and write buffering, a single entity's shard takes massive write load. And it's not just writes — everyone in News Feed is reading this post's reaction counts simultaneously. The shard is both write-hot and read-hot. Cache absorbs reads, but cache invalidation storms (every new reaction invalidates the cache) can cause thrashing. I'd want: per-entity rate limiting on cache invalidation (invalidate at most once per second), stale-while-revalidate caching, and the ability to 'pin' a hot entity's summary in cache with async background refresh.
>
> 2. **Count accuracy erosion.** Over months, count drift accumulates across trillions of entities. The reconciliation job can't scan trillions of entities daily — it must prioritize. If a low-traffic entity's count drifts, it might go undetected for months. User reports 'my post says 47 likes but I can only see 45 people in the who-reacted list.' I'd want: tiered reconciliation (hot entities hourly, warm daily, cold weekly), plus user-triggered reconciliation ('refresh counts' button for post owners).
>
> 3. **Notification quality vs latency.** Longer coalescing windows produce better notifications (more context, less spam) but delay delivery. Too long, and the user discovers reactions by opening the app, not from the notification — making the notification worthless. Finding the sweet spot requires A/B testing notification open rates against coalescing window duration. The 'right' answer varies by user and by post.
>
> 4. **Cross-region consistency during region failover.** If the leader region goes down, we need to promote a follower to leader. During the failover window (seconds to minutes), writes are blocked or routed to a new leader that may have stale data (replication lag). This can cause: lost recent reactions, duplicate counts (if a reaction was written to the old leader but not yet replicated). I'd want: automated failover with < 30s detection, conflict resolution for in-flight writes, and a post-failover reconciliation sweep.
>
> 5. **Reaction spam and abuse.** Botnets can like/unlike millions of posts to manipulate engagement signals or to harass users. Rate limiting per user is necessary but not sufficient — a botnet with 100K accounts at 100 reactions/min each = 10M reactions/min. I'd want: anomaly detection on reaction patterns (temporal clustering, target concentration), automatic throttling of suspicious accounts, and the ability to bulk-remove bot reactions without manual intervention."

---

## Interviewer's Final Assessment

**Hire Recommendation: Strong Hire (L6 — solid SDE-3)**

| Dimension | Rating | Notes |
|---|---|---|
| **Problem Decomposition** | Exceeds Bar | Clean iterative build-up from single server to TAO. Each attempt clearly motivated by concrete problems. |
| **Requirements & Scoping** | Exceeds Bar | Proactively raised upsert semantics, notification coalescing, extensibility. Quantified scale numbers with sources. |
| **API Design** | Meets Bar | Clean upsert API, well-designed reaction summary with personalization. Understood pre-aggregated counts are mandatory. |
| **Architecture Evolution** | Exceeds Bar | 5 clear iterations (Attempt 0→4), each driven by specific scaling bottlenecks. Hot-key handling was strong. |
| **Write Path** | Exceeds Bar | Detailed step-by-step trace including atomicity concerns, idempotency, and transaction boundaries. |
| **Read Path & Caching** | Exceeds Bar | Multi-layer caching with hit rates and latencies. Lease-based invalidation, thundering herd prevention. |
| **Notification Pipeline** | Exceeds Bar | Adaptive coalescing windows, name selection priority, crash recovery via Kafka replay. |
| **Extensibility** | Meets Bar | Integer type IDs with registry. Feature flag rollout. Client compatibility. |
| **Consistency** | Exceeds Bar | Distinguished three consistency levels. Enumerated specific race conditions with solutions. Reconciliation job. |
| **Operational Maturity** | Exceeds Bar | "What keeps you up at night" was excellent — count accuracy erosion, notification quality tradeoff, cross-region failover. |
| **Communication** | Exceeds Bar | Structured, used diagrams and tables, drove the conversation proactively. |

**What would push this to L7:**
- Cost modeling: $/reaction, storage cost of trillions of records, Redis memory cost for hot key buffering
- Organizational impact: how to roll out this system across multiple product teams (Instagram reactions, Messenger reactions)
- Formal consistency analysis: references to CAP theorem, PACELC, discussion of TAO's consistency guarantees in the context of Brewer's conjecture
- Deeper operational thinking: runbooks for specific failure scenarios, game day exercises, capacity planning for major events (Super Bowl, elections)
- Cross-system implications: how reaction signals affect content moderation, integrity systems, and advertiser metrics

---

## Key Differences: L5 vs L6 vs L7 for This Problem

| Aspect | L5 (SDE-2) | L6 (SDE-3) | L7 (Principal) |
|---|---|---|---|
| **Requirements** | Lists CRUD operations | Identifies upsert semantics, coalescing, extensibility, quantifies read:write ratio and peak write rates | Frames requirements around user trust, engagement quality, cost constraints, and cross-product implications |
| **Architecture** | Correct but static design (DB + cache + queue) | Iterative evolution with concrete problems driving each change. Mentions TAO by name with properties. | Discusses cell-based architecture, blast radius isolation, organizational ownership boundaries |
| **Hot-Key Handling** | "Add more servers" | Sharded counters with math (256 shards, 390 writes/sec/shard), write buffering, Count-Min Sketch detection | Discusses automatic hot/cold path routing, cost of false positives, tiered counter strategies, hardware-level optimization |
| **Consistency** | "Use eventual consistency" | Distinguishes record/count/notification consistency. Reconciliation job with tiered frequency. | Formal consistency analysis, references TAO paper, discusses consensus protocol for leader election |
| **Notifications** | "Send push notifications" | Adaptive coalescing windows with concrete durations, name selection priority, crash recovery | ML-based notification personalization, notification quality metrics, A/B testing framework |
| **Operational** | Mentions monitoring | Identifies specific failure modes (count drift, cache thrashing, region failover) with concrete mitigations | Proposes runbooks, game days, capacity planning. Discusses blast radius of bugs at scale (1 bug → billions of users affected) |

---

*For detailed deep dives on each component, see the companion documents:*
- [API Contracts](02-api-contracts.md) — Comprehensive API reference for the reaction system
- [Data Model & Storage](03-data-model-and-storage.md) — Schema design, storage options, sharding strategy
- [Write Path](04-write-path.md) — Upsert semantics, write flow, hot-key write handling
- [Read Path](05-read-path.md) — Caching strategy, personalization, count accuracy tradeoffs
- [Notification Pipeline](06-notification-pipeline.md) — Coalescing architecture, adaptive windows, delivery
- [Celebrity & Hot-Key Problem](07-celebrity-and-hot-key-problem.md) — Sharded counters, write buffering, detection
- [Reaction Types & Extensibility](08-reaction-types-and-extensibility.md) — Type registry, client compatibility, temporary types
- [Consistency & Accuracy](09-consistency-and-accuracy.md) — Race conditions, count drift, reconciliation, privacy
- [Scaling & Reliability](10-scaling-and-reliability.md) — TAO architecture, multi-region, failure modes, monitoring
- [Design Trade-offs](11-design-trade-offs.md) — Opinionated analysis of every major design decision

*End of interview simulation.*
