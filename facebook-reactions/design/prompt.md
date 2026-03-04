Design Facebook Likes/Reactions as a system design interview simulation.

## Template
Follow the EXACT same format as the S3 interview simulation at:
src/hld/aws/s3/design/interview-simulation.md

Same structure:
- Interviewer/Candidate dialogue format
- PHASE 1: Opening & Problem Statement
- PHASE 2: Requirements Gathering & Scoping (functional + non-functional + scale numbers)
- PHASE 3: API Design
- PHASE 4: High-Level Architecture (iterative build-up: Attempt 0 → 1 → 2 → ..., starting from single server, finding problems, evolving)
- PHASE 5+: Deep dives on each component
- L5/L6/L7 rubric table after EACH deep dive phase — showing exactly how an L5, L6, and L7 candidate would answer differently. This is critical for understanding what "good" looks like at each level.
- Architecture evolution table after each phase
- Wrap-up phase with "what keeps you up at night"
- Links to supporting deep-dive docs

## Candidate Level
**L6 SDE-3 (Senior SDE)** candidate being interviewed by a Principal Engineer (L8).

The candidate's answers should demonstrate L6-level depth:
- Not just "what" but "why" and "why not the alternative"
- Quantitative reasoning (back-of-envelope math, concrete numbers)
- Awareness of operational concerns (monitoring, failure modes, blast radius)
- Proactive identification of tradeoffs before the interviewer asks

The L5/L6/L7 rubric tables after each phase should make it crystal clear how an L5 answer differs from L6 differs from L7.

## Output location
Create all files under: src/hld/facebook-reactions/design/

## Files to create

### 1. 01-interview-simulation.md — the main backbone
The full interview dialogue covering all phases and attempts.

### 2. 02-api-contracts.md — Reaction System APIs

This doc should list all the major API surfaces of a Facebook-like reaction system. The interview simulation (Phase 3) will only cover a subset — this doc is the comprehensive reference.

**Structure**: For each API group, list every endpoint with HTTP method, path, request/response shape, and a brief description. Mark endpoints covered in the interview with a star or highlight.

**API groups to cover**:

- **React APIs**: The core path. `POST /reactions` (add a reaction — like, love, haha, wow, sad, angry — to a post/comment/message), `DELETE /reactions/{reactionId}` (remove reaction), `PUT /reactions/{reactionId}` (change reaction type — user changes from "like" to "love"). Key insight: a user can have **at most one reaction per entity** — this is an UPSERT, not a simple INSERT. If user already reacted, the new reaction replaces the old one. The reaction is associated with a `(userId, entityId, entityType)` tuple — this uniqueness constraint is fundamental.

- **Reaction Count APIs**: `GET /entities/{entityId}/reactions/summary` (return aggregated counts per reaction type: `{like: 1200, love: 340, haha: 56, ...}` + total count + whether the current user has reacted). This is the HOT path — every post render needs this data. Must be extremely fast (< 10ms). **Pre-aggregated counts** vs computing on the fly — at Facebook's scale (billions of posts, trillions of reactions), you MUST pre-aggregate. The count is eventually consistent — a slight delay in count accuracy is acceptable (user adds a "like" and sees count go from 1200 to 1201 within a few seconds, not instantly).

- **Reaction List APIs**: `GET /entities/{entityId}/reactions` (paginated list of who reacted with what — "Alice, Bob, and 1198 others liked this"). `GET /entities/{entityId}/reactions?type=love` (filter by reaction type). Cursor-based pagination. Less latency-sensitive than counts (only fetched when user explicitly clicks "see who reacted").

- **Notification APIs** (internal): When someone reacts to your post, you get a notification. `POST /notifications/reaction` (internal, triggered by reaction event). Notifications are **batched and coalesced** — if 50 people like your post in 1 minute, you get ONE notification ("Alice, Bob, and 48 others liked your post"), not 50 separate notifications. Coalescing window, deduplication, and priority ranking.

- **Activity Feed Integration**: Reactions feed into the News Feed ranking algorithm. A post with many reactions ranks higher. Reaction events are published to the activity feed pipeline (Kafka → feed ranking → News Feed). The reaction type matters — a "love" may signal stronger engagement than a "like".

- **Analytics APIs** (internal): `GET /analytics/reactions/trending` (which posts are getting the most reactions right now — used for trending/viral detection), `GET /analytics/reactions/sentiment` (aggregate reaction type distribution — are users predominantly "angry" reacting to a topic? Used for content moderation signals).

**Contrast with Twitter/X Likes**:
- Twitter has a single reaction type (like/heart). Facebook has 6 reaction types (like, love, haha, wow, sad, angry). The multi-type aspect adds complexity: counts must be per-type AND total, the UI shows a summary of top reaction types, and the notification must include the reaction type.
- Twitter likes are simpler (boolean toggle), Facebook reactions are a selection from a menu.

**Contrast with Reddit Upvotes/Downvotes**:
- Reddit has two types (upvote/downvote) that compute a NET score (upvotes - downvotes). Facebook reactions are all "positive" engagement signals (no downvote). Reddit's net score is a single number; Facebook's reaction summary is a breakdown by type.
- Reddit votes directly affect post ranking (score-based). Facebook reactions feed into a more complex News Feed ranking algorithm (ML-based, reactions are one of many signals).

**Contrast with Instagram Likes**:
- Instagram (also Meta) uses a simpler like system (binary, no reaction types). Instagram briefly hid like counts in some regions to reduce social pressure — a product decision that affects the API (counts may not be returned to non-authors).

**Interview subset**: In the interview (Phase 3), focus on: adding a reaction (the write path with upsert semantics), getting reaction counts (the read-heavy hot path), and the notification pipeline (coalescing). The full API list lives in this doc.

### 3. 03-data-model-and-storage.md — Data Model & Storage Layer

The data model is deceptively complex — the uniqueness constraint (one reaction per user per entity) and the need for pre-aggregated counts create interesting storage challenges.

- **Reaction record**: `(userId, entityId, entityType, reactionType, timestamp)`. The primary key is `(userId, entityId)` — this enforces the uniqueness constraint (one reaction per user per entity). `entityType` is "post", "comment", "message", etc.
- **Storage options**:
  - **MySQL/PostgreSQL (relational)**: `UNIQUE INDEX (user_id, entity_id)` enforces uniqueness. `UPSERT` (INSERT ON DUPLICATE KEY UPDATE) for atomic reaction change. Good for consistency. Problem: at trillions of reactions, a single relational DB doesn't scale. Must shard.
  - **Cassandra (wide-column)**: Partition key = `entityId`, clustering key = `userId`. Natural fit for "get all reactions for entity X." Eventually consistent. No native UPSERT — must use lightweight transactions (expensive) or accept last-writer-wins.
  - **Redis (in-memory)**: For reaction counts (not individual records). `HINCRBY entity:{entityId}:counts like 1`. Sub-millisecond reads. Problem: memory is expensive at Facebook's scale; Redis is for hot counts, not the full history.
  - **TAO (Facebook's social graph store)**: Facebook's actual solution. TAO is a distributed graph store built on top of MySQL + Memcache. Optimized for the social graph — objects (posts, users) and associations (likes, friendships). TAO handles caching, consistency, and sharding.
- **Sharding strategy**: Shard by `entityId` (all reactions for a post are on the same shard → efficient aggregation) vs shard by `userId` (all of a user's reactions are on the same shard → efficient for "show me everything I've liked"). Facebook shards by `entityId` for the reaction store (optimizes the hot path: showing reaction counts on a post).
- **Pre-aggregated counts**: Maintain a separate `reaction_counts` table: `(entityId, reactionType) → count`. Updated atomically on every react/unreact. This avoids `SELECT COUNT(*)` on potentially millions of rows. The count table is the source of truth for display — individual reaction records are the source of truth for "who reacted."
  - **Count consistency challenge**: If a user reacts and the count increment fails (or succeeds but the user's individual record write fails), counts become inconsistent. Solutions: (a) transactional update (both in one transaction — expensive at scale), (b) eventual consistency via async count reconciliation (periodic job that recounts from individual records and corrects drift).
- **Denormalization**: The reaction summary shown on a post (`{like: 1200, love: 340, ...}` + top 3 friends who reacted) is denormalized and cached. Computing it from raw data on every post render is infeasible.
- **Contrast with Instagram**: Instagram stores likes in a simpler model (no reaction types, just `(userId, entityId)`). The count is a single number, not a breakdown.
- **Contrast with YouTube**: YouTube stores likes and dislikes separately with a net score. YouTube briefly hid dislike counts (still stored, just not displayed) — a product decision that doesn't change the storage model.

### 4. 04-write-path.md — Write Path (React / Unreact / Change Reaction)

The write path must handle the upsert semantics, update counts, trigger notifications, and publish to the activity feed — all with low latency.

- **Upsert semantics**: When a user taps "love" on a post:
  1. Check if user already has a reaction on this entity
  2. If no existing reaction → INSERT new reaction + INCREMENT count for "love"
  3. If existing reaction with same type → no-op (already loved)
  4. If existing reaction with different type (e.g., was "like", now "love") → UPDATE reaction type + DECREMENT "like" count + INCREMENT "love" count
  - This multi-step logic must be atomic or at least eventually consistent. A crash between decrement and increment could leave counts wrong.
- **Write flow**:
  1. Client sends `POST /reactions` with `{entityId, reactionType}`
  2. API server validates request (entity exists, user is authorized)
  3. Write reaction record to primary store (MySQL/TAO)
  4. Update pre-aggregated counts (increment new type, decrement old type if changing)
  5. Publish event to Kafka: `{userId, entityId, reactionType, previousType, timestamp}`
  6. Return success to client (at this point, the reaction is persisted)
  7. **Asynchronously** (via Kafka consumers):
     - Update cache (invalidate/update cached reaction summary)
     - Trigger notification pipeline (coalesce and send push notification)
     - Update News Feed ranking signal
     - Update analytics/trending counters
- **Idempotency**: If the client retries (network timeout), the upsert semantics naturally make this idempotent — writing the same reaction again is a no-op.
- **Rate limiting**: Prevent abuse (bot liking millions of posts). Rate limit per user: e.g., max 100 reactions per minute. Rate limit per entity: e.g., max 10,000 reactions per second per post (to handle viral posts without overwhelming a single shard).
- **Celebrity/viral post problem**: A post by a celebrity with 100M followers could receive millions of reactions in minutes. This creates a write hot spot on the entity's shard. Solutions:
  - **Write buffering**: Buffer reactions in memory for a few seconds, batch-write to the database.
  - **Sharded counters**: Instead of one counter per entity, maintain N counter shards. Each write goes to a random shard. Read = SUM all shards. Trades read cost for write distribution.
  - **Async count propagation**: Accept that counts are eventually consistent. The write path only stores the individual reaction; count aggregation happens async.
- **Contrast with Twitter**: Twitter likes are simpler (no type, no upsert with type change). But Twitter faces the same celebrity hot-spot problem (a tweet by a celebrity getting millions of likes).

### 5. 05-read-path.md — Read Path (Counts, Lists, Personalization)

The read path is THE hot path — every post render needs reaction counts. At Facebook's scale, this is billions of post renders per day.

- **Reaction summary (the hot path)**: For every post rendered in News Feed, the client needs:
  - Total reaction count
  - Breakdown by reaction type (at least the top 3 types with counts)
  - Whether the current user has reacted (and what type)
  - Top N friends who reacted ("Alice, Bob, and 48 others")
  - This data must be served in < 10ms (it's part of the News Feed render path)
- **Caching strategy (multi-layer)**:
  - **L1: Client-side cache** — the app caches reaction summaries for posts currently on screen. Reduces redundant API calls on scroll.
  - **L2: CDN / edge cache** — not applicable for personalized data (the "did I react?" part is user-specific).
  - **L3: Application cache (Memcache/TAO cache)** — reaction summaries cached per entity. Cache key: `entity:{entityId}:reaction_summary`. TTL: 60 seconds. Cache hit rate: 99%+ for popular posts.
  - **L4: Database (MySQL/TAO)** — source of truth. Only hit on cache miss (< 1% of requests).
- **Cache invalidation**: When a reaction is added/removed:
  - **Write-through**: Update cache synchronously on write. Low latency for the reactor, but adds write latency.
  - **Write-behind (async invalidation)**: Write to DB, publish event, consumer invalidates/updates cache. Lower write latency, but brief stale reads.
  - Facebook uses **write-behind with leasing** in TAO — a mechanism that prevents thundering herd (many concurrent cache misses for the same key).
- **"Friends who reacted" personalization**: The "Alice, Bob, and 48 others" display requires knowing which of the current user's friends reacted. This is a **set intersection** (friends of current user ∩ users who reacted to this post). At scale, this is precomputed or cached — not computed on every render.
- **Count accuracy vs latency trade-off**: Displaying the exact count (e.g., 1,247,892) is less important than displaying a directionally correct count. Facebook often shows approximate counts for large numbers ("1.2M likes"). This allows more aggressive caching and eventual consistency.
- **Contrast with Instagram**: Instagram hid like counts in some markets (only the post author sees the exact count). This is a product decision that simplifies the read path — no count to render for viewers.

### 6. 06-notification-pipeline.md — Notification Coalescing & Delivery

Notifications are the most complex async pipeline in the reaction system. The key challenge: **coalescing** — grouping many individual reaction events into a single notification.

- **The coalescing problem**: If 500 people like your post in 5 minutes:
  - BAD: 500 separate push notifications ("Alice liked your post", "Bob liked your post", ...)
  - GOOD: 1 notification ("Alice, Bob, and 498 others liked your post")
  - BETTER: 1 notification with the most relevant names first (close friends, people you interact with frequently)
- **Coalescing window**: Buffer reaction events for an entity-owner pair for N seconds (e.g., 30 seconds). After the window closes, emit one coalesced notification. If more reactions arrive after the notification is sent, start a new window.
  - **Trade-off**: Longer window = better coalescing (fewer notifications) but more delay. Shorter window = faster notification but more notification spam.
  - **Adaptive window**: For viral posts (high reaction rate), extend the window (reactions are coming fast, coalesce more aggressively). For normal posts (low reaction rate), use a short window (user wants prompt notification).
- **Notification content**: "Alice, Bob, and 498 others liked your post" — how to choose which names to show?
  - Priority: (1) close friends, (2) people the recipient interacts with frequently, (3) most recent reactors.
  - This requires a social graph lookup at notification generation time.
- **Delivery channels**: Push notification (APNs for iOS, FCM for Android), in-app notification badge, email digest (batched, lower priority).
- **Notification deduplication**: If the user already saw a notification for this post's reactions, should a new batch generate a NEW notification or UPDATE the existing one? Facebook updates the existing notification (change "Alice and 48 others" to "Alice, Bob, and 498 others").
- **Notification suppression**: Don't notify for reactions on very old posts (> 30 days). Don't notify if the user has muted the post. Don't notify if the user has turned off reaction notifications.
- **Contrast with WhatsApp**: WhatsApp message reactions generate individual notifications (no coalescing) because the reaction rate per message is low (typically 1-5 reactions, not thousands).
- **Contrast with YouTube**: YouTube coalesces like notifications similarly ("100 people liked your video") but with longer windows (YouTube notification cadence is less real-time than Facebook).

### 7. 07-celebrity-and-hot-key-problem.md — Handling Viral Content & Hot Keys

The single hardest scaling challenge: a celebrity post receiving millions of reactions per minute.

- **The hot key problem**: All reactions for a post go to the same database shard (sharded by entityId). A viral post creates a write hot spot that can overwhelm a single shard.
  - **Scale math**: Celebrity with 100M followers. Post goes viral. 10% react in the first hour = 10M reactions in 60 minutes = ~167K reactions/second. A single MySQL shard handles ~10K writes/second. 167K writes/second would crash the shard.
- **Solutions**:
  - **Sharded counters**: Instead of one counter row `(entityId, like_count)`, maintain N counter shards: `(entityId, shardId, like_count)`. Each write randomly picks a shard. Total count = SUM across all shards. Trade-off: read is O(N) instead of O(1), but N is small (e.g., 256 shards).
  - **Write buffering / batching**: Accumulate reactions in an in-memory buffer (e.g., Redis) for 1-5 seconds, then batch-write to the database. Reduces write amplification. Risk: buffer loss on crash = lost reactions (acceptable for counts, not for individual records).
  - **Queue-based write leveling**: Write reactions to Kafka, consume at a controlled rate. The database processes reactions at its own pace, not at the rate users submit them. Latency: counts update with a delay (seconds to minutes for viral posts).
  - **Separate hot/cold paths**: Detect viral posts (reaction rate > threshold) and route them through a different pipeline optimized for high-throughput writes (Redis for real-time counts, async reconciliation to persistent storage).
- **Read hot spot**: Viral posts are also read-heavy (everyone viewing News Feed sees the post). Caching with short TTL + stale-while-revalidate absorbs the read load.
- **Contrast with Twitter**: Twitter faced the same problem with viral tweets. Twitter uses a fanout-on-write model for the timeline, but for like counts, they use similar techniques (sharded counters, caching).
- **Contrast with Reddit**: Reddit's vote system has the same hot-key problem for viral posts. Reddit uses fuzzy vote counts (intentionally adds noise to displayed counts) to reduce the precision requirement and allow more aggressive caching.

### 8. 08-reaction-types-and-extensibility.md — Reaction Type System & Extensibility

How to design the reaction type system so new reactions can be added without a schema migration.

- **Current Facebook reactions**: Like (👍), Love (❤️), Haha (😂), Wow (😮), Sad (😢), Angry (😡). Originally just "Like" (2009). Reactions added in 2016. "Care" reaction added in 2020 (during COVID). Reactions can be added and removed over time.
- **Data model for extensibility**:
  - **Option A: Enum column** — `reaction_type ENUM('like','love','haha','wow','sad','angry')`. Simple, type-safe, but adding a new type requires schema migration (ALTER TABLE on a trillion-row table = days/weeks of downtime).
  - **Option B: Integer/string type** — `reaction_type TINYINT` or `reaction_type VARCHAR(16)`. New types added by convention (code change, not schema change). Less type-safe but more extensible.
  - **Option C: Type registry** — separate `reaction_types` table mapping `typeId → typeName, emoji, added_date, active`. Reaction records reference `typeId`. Adding a new type = INSERT into registry. Most extensible.
  - Facebook likely uses a combination: integer type IDs with a registry, plus client-side mapping of type IDs to emojis/names.
- **Client-server contract**: When a new reaction type is added, old clients (who haven't updated) must handle it gracefully. Options:
  - Old clients display unknown reactions as a generic "reacted" label
  - Server sends both typeId and a fallback display name
  - Feature flags control which reactions are available per region/rollout
- **Temporary/event reactions**: Facebook has experimented with temporary reaction types (Pride reaction for Pride Month, Care reaction during COVID). These need time-limited availability — the reaction type is available for a period, then disabled for new reactions but existing reactions remain visible.
- **Contrast with Slack**: Slack allows arbitrary emoji reactions (any emoji in the set, including custom emojis). This is maximally extensible but creates a different UI challenge (potentially hundreds of different reaction types on a single message). Slack's model is `(userId, messageId, emojiCode)` — no enum at all, just a string.
- **Contrast with LinkedIn**: LinkedIn has 5 reaction types (Like, Celebrate, Support, Love, Insightful, Funny). Similar to Facebook's model.

### 9. 09-consistency-and-accuracy.md — Consistency, Accuracy & Edge Cases

Reaction counts must be accurate enough for user trust but don't need to be perfectly real-time.

- **Consistency model**: Reaction counts are **eventually consistent** with a convergence window of ~5-10 seconds. The individual reaction record is strongly consistent (you can always check "did I react to this post?").
- **Count drift**: Over time, pre-aggregated counts can drift from the true count (missed increments, double decrements, failed transactions). Solutions:
  - **Periodic reconciliation**: Background job that counts actual reaction records and corrects the pre-aggregated count. Runs hourly/daily.
  - **Audit events**: Every increment/decrement is logged. Replay the log to verify counts.
  - **Checksum approach**: Store a rolling checksum of reaction operations per entity. If checksum diverges between the count table and the event log, trigger reconciliation.
- **Race conditions**:
  - **Double react**: User taps "like" twice quickly. The upsert semantics (unique constraint on userId+entityId) prevents double counting.
  - **React + unreact**: User likes then unlikes within milliseconds. If both operations are in flight, the final state depends on which commits last. With a unique constraint and upsert, the last write wins (correct).
  - **Concurrent type change**: User changes from "like" to "love" while another request to "unreact" is in flight. Needs careful ordering or a version/revision field.
- **Deleted entities**: When a post is deleted, what happens to its reactions?
  - Option A: Cascade delete (remove all reaction records). Expensive for a post with millions of reactions.
  - Option B: Soft delete (mark post as deleted, reactions remain but are not queryable). Cheaper, reactions cleaned up by background GC.
  - Facebook uses soft delete with async cleanup.
- **Blocked users**: If Alice blocks Bob, should Bob's reaction still count and be visible to Alice? No — Alice should not see Bob in the "who reacted" list, and Bob's reaction should not appear in Alice's notification. This requires filtering the reaction list by the viewer's block list — a privacy overlay.
- **Contrast with YouTube**: YouTube hid the dislike count from viewers (still stored internally) to reduce "dislike bombing." The count is accurate but not displayed. This is a product-level consistency decision.

### 10. 10-scaling-and-reliability.md — Scaling & Reliability

Cross-cutting doc that ties together scaling decisions from all deep dives.

- **Scale numbers** (Facebook/Meta-scale):
  - **~3 billion monthly active users** on Facebook.
  - **Billions of reactions per day** (posts + comments + messages + stories).
  - **Trillions of reaction records** stored historically.
  - **Billions of posts rendered per day**, each needing a reaction summary → billions of read queries per day.
  - **Peak reaction rate**: celebrity/viral posts can receive 100K+ reactions per second.
- **Read:write ratio**: Extremely read-heavy. Every post render reads reaction counts (billions/day). Reactions are written much less frequently (billions/day, but each post is read orders of magnitude more than it's reacted to). Estimated ratio: 100:1 to 1000:1 read:write.
- **TAO (Facebook's graph store)**: The actual system Facebook uses. TAO is a distributed, caching graph store built on MySQL + Memcache. It handles:
  - Objects (posts, users, pages) and Associations (likes, friendships, comments)
  - Per-type caching policies
  - Write-through caching with lease-based invalidation
  - Cross-region asynchronous replication
  - Leader-follower model: one region is the "leader" for writes, all regions can read
- **Multi-region**: Facebook operates across multiple data centers globally. Reaction writes go to the leader region. Reads are served from the nearest replica. Replication lag: ~1-5 seconds cross-region. This means a user in Europe might see a slightly different reaction count than a user in the US for a few seconds after a reaction.
- **Monitoring**: Reaction rate per second (global, per-region, per-entity), count accuracy (drift detection), cache hit rate, write latency p50/p95/p99, notification delivery latency, hot-key detection.
- **Failure modes**: Cache failure (thundering herd on DB), shard failure (reactions for affected entities unavailable), Kafka lag (notifications delayed), count drift (reconciliation needed).
- **Contrast with smaller-scale systems**: At startup scale (< 1M users), a single PostgreSQL database with a reactions table and COUNT(*) queries is perfectly fine. The complexity described here is only needed at Facebook/Meta scale.

### 11. 11-design-trade-offs.md — Design Philosophy & Trade-off Analysis

Opinionated analysis of the reaction system's design choices — not just "what" but "why this and not that."

- **Pre-aggregated counts vs real-time COUNT(*)**: Pre-aggregated counts add write complexity (must maintain a separate counter) but make reads O(1) instead of O(N). At Facebook's scale, COUNT(*) on millions of rows per post render is infeasible. Pre-aggregation is mandatory above ~10K reactions per entity.
- **Eventual consistency vs strong consistency for counts**: Facebook chose eventual consistency for counts (seconds of lag is acceptable). Strong consistency would require synchronous distributed transactions for every reaction — adding 10-50ms latency to every write. For a "like" button, that latency is unacceptable.
- **Upsert vs separate create/delete**: Facebook uses upsert semantics (one reaction per user per entity). The alternative — allowing multiple reactions (like Slack's emoji reactions) — is simpler to implement but creates a different product (many small reactions vs one per user).
- **Sharded counters vs single counter**: For hot entities (viral posts), sharded counters distribute write load. Trade-off: reads must sum N shards (more expensive). The math works because hot entities are also cached — the sum is computed once and cached, not on every read.
- **Notification coalescing vs individual notifications**: Coalescing reduces notification spam but adds delay. The optimal coalescing window depends on the reaction rate — adaptive windows balance immediacy and spam.
- **Fixed reaction types vs arbitrary emojis**: Facebook chose a fixed set of 6 reactions. Slack allows arbitrary emojis. Fixed set is simpler for aggregation (6 counters vs unbounded), analytics (sentiment analysis on 6 types is tractable), and UI (predictable display). Arbitrary emojis are more expressive but harder to aggregate and analyze.
- **Entity-sharded vs user-sharded**: Sharding by entity optimizes the hot path (show reactions for a post). Sharding by user optimizes "show me all posts I've liked." Facebook chose entity-sharding because the read path (every News Feed render) is much hotter than the user-history path.
- **TAO (custom graph store) vs off-the-shelf databases**: Facebook built TAO because no off-the-shelf database met their requirements (graph semantics, caching, cross-region replication, lease-based invalidation). For any company below Facebook's scale, PostgreSQL or Cassandra is sufficient.
- **Contrast: "Why didn't Facebook just use Redis for everything?"**: Redis is great for counters (HINCRBY) but: (a) memory is expensive at trillions of reactions, (b) Redis doesn't provide the durability guarantees needed for individual reaction records, (c) Redis doesn't natively support the social graph queries (friends who reacted). Redis is part of the solution (caching layer) but not the whole solution.

## CRITICAL: The design must be Facebook-centric
Facebook's reaction system is the reference implementation. The design should reflect how Facebook actually works — TAO for the social graph, Memcache for caching, Kafka for event streaming, the coalescing notification pipeline, and the sharded counter pattern for viral posts. Where Twitter, Reddit, Instagram, or Slack made different choices, call those out explicitly as contrasts.

## CRITICAL: Iterative build-up (DO NOT skip this)
The architecture MUST evolve iteratively. Each attempt builds on the previous one by identifying a concrete problem and solving it. The candidate should NOT jump to the final architecture. The progression should feel natural:

### Attempt 0: Single server with a reactions table
- A web server with a MySQL table: `reactions(id, userId, entityId, reactionType, timestamp)`. UNIQUE INDEX on `(userId, entityId)`. Client sends POST, server inserts/updates. Counts computed with `SELECT COUNT(*) GROUP BY reactionType WHERE entityId = ?`.
- **Problems found**: COUNT(*) is O(N) per post render — a post with 1M reactions takes seconds to count. Single database can't handle billions of reactions. No caching, no notifications, no real-time updates.

### Attempt 1: Pre-aggregated counts + caching
- **Pre-aggregated counts table**: `reaction_counts(entityId, reactionType, count)`. Incremented/decremented on every react/unreact. Counts are O(1) reads instead of O(N).
- **Application cache (Memcache)**: Cache reaction summaries per entity. 99%+ cache hit rate for popular posts. Cache invalidated on reaction write.
- **Problems found**: Count and reaction record writes are not atomic (can drift). Single database still can't handle the write volume. No notifications. No handling of viral posts (hot keys).

### Attempt 2: Sharded database + async event pipeline
- **Shard the database** by entityId. Each shard handles reactions for a subset of entities. Write load distributed across shards.
- **Async event pipeline (Kafka)**: Reaction writes publish events to Kafka. Consumers handle: cache invalidation, notification generation, analytics, News Feed ranking signal update.
- **Notification pipeline**: Consume Kafka events, coalesce reactions per entity-owner, generate batched notifications.
- **Problems found**: Viral posts still create hot shards (all reactions for one entity go to one shard). Count drift from eventual consistency between reaction records and count table. No cross-region support.

### Attempt 3: Hot-key handling (sharded counters, write buffering)
- **Sharded counters for hot entities**: Detect hot entities (reaction rate > threshold). Split their counter into N shards. Writes go to random shard. Reads sum all shards (cached, so SUM is infrequent).
- **Write buffering**: For viral posts, buffer reactions in Redis for 1-5 seconds, then batch-write to MySQL. Reduces per-second write pressure on the shard.
- **Adaptive notification coalescing**: For high-reaction-rate posts, extend the coalescing window (less spam). For normal posts, use shorter window (prompt notification).
- **Problems found**: Single-region architecture. No global presence. Cache invalidation races. "Friends who reacted" personalization is expensive.

### Attempt 4: Production hardening (TAO, multi-region, monitoring)
- **TAO (or TAO-like graph store)**: Replace raw MySQL + Memcache with a unified graph store that handles caching, sharding, and cross-region replication as a platform. Objects (posts) and Associations (reactions) are first-class concepts.
- **Multi-region replication**: Leader region handles writes, all regions serve reads. Replication lag: 1-5 seconds. Eventual consistency for counts.
- **Periodic count reconciliation**: Background job recomputes counts from individual records, corrects drift.
- **Privacy-aware reads**: Filter reaction lists by viewer's block list. Don't show blocked users in "who reacted." Don't send notifications for reactions from blocked users.
- **Monitoring & alerting**: Reaction rate, count drift, cache hit rate, notification delivery latency, hot-key detection, shard health.
- **Contrast with Twitter/Reddit at every stage**: How their different product requirements (single reaction type, net score, etc.) lead to simpler architectures.

## CRITICAL: Accuracy requirements

I DO NOT TRUST AI-GENERATED FACTS ABOUT SPECIFIC SYSTEMS. Every concrete claim about Facebook internals must be verifiable against official sources. Specifically:

1. **Use WebSearch and WebFetch tools** to look up Meta Engineering Blog, Facebook Engineering Blog, TAO paper, and relevant documentation BEFORE writing. Search for:
   - "Facebook TAO social graph store"
   - "Facebook reaction system architecture"
   - "Facebook like button technical architecture"
   - "Facebook Memcache scaling"
   - "Facebook notification coalescing"
   - "Meta engineering blog reactions"
   - "Facebook sharded counters hot key"
   - "TAO paper USENIX ATC 2013"
   - "Facebook News Feed ranking reactions"
   - "Facebook active users 2024 2025"

   **BLANKET PERMISSION**: You have full permission to use WebSearch and WebFetch to read any online documentation, blog posts, or research papers. Do NOT ask the user for permission to read — just read. This applies to engineering.fb.com, engineering.meta.com, research.facebook.com, and any other reference source. Read as many pages as needed to verify facts.

2. **For every concrete number** (user count, reaction volume, latency targets), verify against official sources. If you cannot verify a number, explicitly write "[UNVERIFIED — check Meta Engineering Blog]" next to it.

3. **For every claim about Facebook internals** (TAO architecture, notification pipeline), if it's not from an official source, mark it as "[INFERRED — not officially documented]".

4. **CRITICAL: Do NOT confuse Facebook with Twitter, Reddit, or Instagram.** These are different systems:
   - Facebook: 6 reaction types, social graph integration, News Feed ranking, TAO graph store
   - Twitter/X: single like type, simpler model, timeline-based
   - Reddit: upvote/downvote with net score, community-based
   - Instagram: binary like, social graph (same parent company, simpler model)
   - Slack: arbitrary emoji reactions, workspace-scoped, message-based

## Key topics to cover

### Requirements & Scale
- Reaction system supporting 6 reaction types on posts, comments, messages
- ~3B MAU, billions of reactions per day, trillions stored
- Read-heavy: every News Feed post render needs reaction summary (< 10ms)
- Write spikes: viral posts → 100K+ reactions/second
- Eventually consistent counts (seconds of lag acceptable)
- Notification coalescing for high-volume reaction events

### Architecture deep dives (create separate docs as listed above)

### Design evolution (iterative build-up)
- Attempt 0: Single MySQL, COUNT(*)
- Attempt 1: Pre-aggregated counts + Memcache
- Attempt 2: Sharded DB + Kafka event pipeline + notifications
- Attempt 3: Hot-key handling (sharded counters, write buffering, adaptive coalescing)
- Attempt 4: TAO, multi-region, reconciliation, privacy, monitoring

Each attempt MUST:
1. Start by identifying concrete problems with the previous attempt
2. Propose a solution
3. Explain WHY this approach (and why not the alternative)
4. End with "what's still broken?" to motivate the next attempt

### Consistency & Data
- Individual reaction records: strongly consistent (unique constraint)
- Reaction counts: eventually consistent (seconds of lag)
- Notification delivery: at-least-once with deduplication
- Count reconciliation: periodic background job
- Cache: write-behind with lease-based invalidation (TAO model)

## What NOT to do
- Do NOT treat this as a simple CRUD app — the scale, hot-key handling, notification coalescing, and social graph integration are the interesting parts.
- Do NOT confuse Facebook reactions with Twitter likes, Reddit votes, or Slack emoji reactions.
- Do NOT jump to the final architecture. Build it step by step (Attempt 0 → 4).
- Do NOT make up internal implementation details — verify or mark as inferred.
- Do NOT skip the hot-key / viral post problem — it's the hardest part of this design.
- Do NOT describe features without explaining WHY they exist.
- Do NOT ask the user for permission to read online documentation — blanket permission is granted.
- Do NOT write a Wikipedia article. This is an INTERVIEW SIMULATION with dialogue.
