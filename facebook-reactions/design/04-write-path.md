# Write Path (React / Unreact / Change Reaction) -- Deep Dive

The write path for Facebook Reactions is deceptively complex. What looks like a simple
"tap the like button" on the client is a multi-step upsert with count maintenance,
cache invalidation, event publishing, and notification triggering -- all while handling
the write hot-spot problem for viral posts.

**Key research facts from Meta Engineering:**
- TAO handles billions of reads/sec and millions of writes/sec, using a Followers ->
  Leaders -> MySQL topology with lease-based cache invalidation
  [TAO: Facebook's Distributed Data Store for the Social Graph, USENIX ATC 2013].
- Memcache invalidation uses `mcsqueal` daemons that tail the MySQL commit log and
  issue deletes to Memcache clusters
  [Scaling Memcache at Facebook, NSDI 2013].
- ~3.07B MAU as of Q3 2024 [Meta Investor Relations].
- Reactions launched February 2016 with 6 types: Like, Love, Haha, Wow, Sad, Angry.

---

## 1. Upsert Semantics -- The Core Complexity

A reaction is not a simple INSERT. The uniqueness constraint `(userId, entityId)` means
each user can have **at most one** reaction per entity. Every write must handle four
distinct cases:

```
CASE 1: No existing reaction
  User has never reacted to this entity.
  Action: INSERT reaction record + INCREMENT count for new type.

CASE 2: Same type already exists
  User taps "Like" but already has a "Like" on this entity.
  Action: No-op. Return success immediately.

CASE 3: Different type exists (TYPE CHANGE)
  User had "Like", now taps "Love".
  Action: UPDATE reaction type
        + DECREMENT "Like" count
        + INCREMENT "Love" count.

CASE 4: Removing reaction (UNREACT)
  User taps the active reaction to toggle it off.
  Action: DELETE reaction record + DECREMENT count for old type.
```

### Why This Is Hard

Case 3 is the dangerous one. It requires **three writes** that must appear atomic:

```
1. UPDATE reactions SET type = 'love' WHERE userId = ? AND entityId = ?
2. UPDATE reaction_counts SET count = count - 1 WHERE entityId = ? AND type = 'like'
3. UPDATE reaction_counts SET count = count + 1 WHERE entityId = ? AND type = 'love'
```

If the system crashes between step 2 and step 3, the "like" count is decremented but
the "love" count is never incremented. The total reaction count for this entity is now
permanently off by one. Multiply this by millions of reactions and counts drift
significantly over time.

**Solutions to the atomicity problem:**

| Approach | Mechanism | Trade-off |
|----------|-----------|-----------|
| Single-row transaction | Store reaction + all counts in same row/shard, use DB transaction | Works at small scale; at Facebook scale, reaction records and count tables may live on different shards |
| Two-phase commit | Distributed transaction across shards | Adds 10-50ms latency per write; unacceptable for a "like" button |
| Eventual consistency + reconciliation | Write reaction record synchronously, update counts best-effort, fix drift with periodic reconciliation job | Counts may be briefly wrong (seconds); acceptable for Facebook's use case |
| Idempotent event log | Log the intent `(CHANGE, like->love)` to Kafka; a single consumer applies all three writes transactionally on the same shard | Adds latency (Kafka consumer lag) but guarantees atomicity within the consumer |

Facebook's approach (via TAO): the reaction record and counts are stored as
**associations** in TAO. TAO's write-through caching and leader-based write path
allow the leader region to apply the upsert and count updates within a single MySQL
transaction on the leader shard [INFERRED -- TAO paper describes associations and
write-through but does not detail reaction-specific transaction boundaries].

---

## 2. Write Flow -- Step by Step

```
                                    SYNCHRONOUS PATH
                                    (user waits for this)
  ┌────────┐    POST /v1/reactions   ┌───────────┐
  │ Client │ ──────────────────────> │ API       │
  │ (App)  │                         │ Gateway   │
  └────────┘                         └─────┬─────┘
       ▲                                   │
       │                          ┌────────▼────────┐
       │                          │ Rate Limiter     │
       │                          │ (token bucket)   │
       │                          └────────┬────────┘
       │                                   │
       │                          ┌────────▼────────┐
       │                          │ Reaction         │
       │                          │ Service          │
       │                          │                  │
       │                          │ 1. Read existing │
       │                          │ 2. Determine op  │
       │                          │    (insert/update│
       │                          │     /delete/noop)│
       │                          └────────┬────────┘
       │                                   │
       │         ┌─────────────────────────┼──────────────────────┐
       │         │                         │                      │
       │         ▼                         ▼                      ▼
       │  ┌──────────────┐   ┌──────────────────┐   ┌──────────────────┐
       │  │ TAO Leader   │   │ TAO Leader       │   │ Kafka            │
       │  │              │   │                  │   │                  │
       │  │ Write reaction│  │ Update counts    │   │ Publish event    │
       │  │ record        │  │ (incr/decr)      │   │                  │
       │  │ (MySQL)       │  │ (MySQL)          │   │                  │
       │  └──────┬───────┘   └────────┬─────────┘   └────────┬─────────┘
       │         │                    │                       │
       │         ▼                    ▼                       │
       │  ┌──────────────────────────────────┐               │
       │  │ TAO Cache (Memcache)             │               │
       │  │ Write-through: cache updated     │               │
       │  │ synchronously on successful DB   │               │
       │  │ write. Lease-based invalidation  │               │
       │  │ prevents thundering herd.        │               │
       │  └──────────────────────────────────┘               │
       │                                                      │
       │  200 OK {reactionId, reactionType}                  │
       │ <────────────────────────────────────                │
       │                                                      │
       │                                                      │
       │                    ASYNCHRONOUS PATH                  │
       │                    (user does NOT wait)               │
       │                                                      ▼
       │                                        ┌─────────────────────────┐
       │                                        │   Kafka Consumers       │
       │                                        ├─────────────────────────┤
       │                                        │                         │
       │                                        │ 1. Cache invalidation   │
       │                                        │    (Follower regions)   │
       │                                        │                         │
       │                                        │ 2. Notification         │
       │                                        │    pipeline             │
       │                                        │    (coalesce + deliver) │
       │                                        │                         │
       │                                        │ 3. News Feed ranking    │
       │                                        │    signal update        │
       │                                        │                         │
       │                                        │ 4. Analytics / trending │
       │                                        │    counters             │
       │                                        │                         │
       │                                        │ 5. mcsqueal daemons     │
       │                                        │    (Memcache inval.)    │
       │                                        └─────────────────────────┘
```

### Detailed Step Breakdown

**Step 1: Client Request**

```http
POST /v1/reactions HTTP/1.1
Host: graph.facebook.com
Authorization: Bearer <access_token>
Content-Type: application/json
X-Request-ID: 550e8400-e29b-41d4-a716-446655440000

{
  "entityId": "post_82649173",
  "entityType": "post",
  "reactionType": "love"
}
```

The `X-Request-ID` serves as an idempotency key. If the client retries due to a
network timeout, the server can detect the duplicate.

**Step 2: API Server Validation**

The API gateway and reaction service perform these checks before touching storage:

1. **Authentication**: Is the access token valid? Is it the user it claims to be?
2. **Authorization**: Can this user see this entity? (Privacy check -- e.g., the post
   may be friends-only and the user is not a friend.)
3. **Entity existence**: Does `post_82649173` exist and is it not deleted?
4. **Rate limit check**: Has this user exceeded 100 reactions/minute?
5. **Entity rate limit**: Is this entity receiving > 10K reactions/sec? (If so, route
   to the hot-key write path -- see Section 5.)

**Step 3: Write Reaction Record to TAO (MySQL)**

The reaction service issues an upsert to the TAO leader for this entity's shard:

```sql
-- MySQL upsert (INSERT ... ON DUPLICATE KEY UPDATE)
INSERT INTO reactions (user_id, entity_id, entity_type, reaction_type, created_at, updated_at)
VALUES (12345, 'post_82649173', 'post', 'love', NOW(), NOW())
ON DUPLICATE KEY UPDATE
  reaction_type = VALUES(reaction_type),
  updated_at = NOW();
```

The `UNIQUE INDEX (user_id, entity_id)` enforces the one-reaction-per-user constraint.
The upsert returns whether it was an INSERT or UPDATE, and if UPDATE, the previous
`reaction_type` value (needed for count adjustment).

In TAO's abstraction, this is an **association write**:
- Object: the entity (post)
- Association type: `REACTED_TO`
- Association: `(userId) --REACTED_TO--> (entityId)` with `reactionType` as metadata

**Step 4: Update Pre-Aggregated Counts**

Based on the upsert result:

```sql
-- CASE 1: New reaction (INSERT happened)
UPDATE reaction_counts
SET count = count + 1
WHERE entity_id = 'post_82649173' AND reaction_type = 'love';

-- CASE 3: Type change (UPDATE happened, old type was 'like')
UPDATE reaction_counts
SET count = count - 1
WHERE entity_id = 'post_82649173' AND reaction_type = 'like';

UPDATE reaction_counts
SET count = count + 1
WHERE entity_id = 'post_82649173' AND reaction_type = 'love';

-- CASE 4: Removal (DELETE)
UPDATE reaction_counts
SET count = count - 1
WHERE entity_id = 'post_82649173' AND reaction_type = 'like';
```

In TAO, these are **association count updates** -- TAO maintains association counts as
a first-class feature. When you add or remove an association, TAO automatically
maintains the count [TAO paper, Section 4].

**Step 5: Publish Event to Kafka**

After the synchronous DB write succeeds, the reaction service publishes an event:

```json
{
  "eventId": "evt_a1b2c3d4",
  "userId": 12345,
  "entityId": "post_82649173",
  "entityType": "post",
  "entityOwnerId": 67890,
  "reactionType": "love",
  "previousType": "like",
  "action": "CHANGE",
  "timestamp": "2025-01-15T10:30:00Z"
}
```

The `action` field is one of: `ADD`, `CHANGE`, `REMOVE`. The `previousType` is null
for `ADD` and populated for `CHANGE` and `REMOVE`. The `entityOwnerId` is included
so downstream consumers (notification pipeline) do not need to look it up.

**Step 6: Return Success to Client**

```http
HTTP/1.1 200 OK
Content-Type: application/json

{
  "reactionId": "rxn_f7e8d9c0",
  "entityId": "post_82649173",
  "reactionType": "love",
  "previousType": "like",
  "action": "CHANGE",
  "counts": {
    "like": 1199,
    "love": 341,
    "haha": 56,
    "wow": 12,
    "sad": 3,
    "angry": 1,
    "total": 1612
  }
}
```

The response includes updated counts so the client can immediately render the new
state without a separate read call. This is an **optimistic response** -- the counts
are from the write path's local view and may differ slightly from what a concurrent
reader sees (eventual consistency).

**Step 7: Asynchronous Processing (Kafka Consumers)**

Multiple independent consumer groups process the reaction event:

| Consumer | Action | Latency Budget |
|----------|--------|---------------|
| Cache Invalidation | Invalidate/update cached reaction summary in Follower TAO regions. TAO's `mcsqueal` daemons also tail the MySQL binlog to delete stale Memcache entries. | < 1 second |
| Notification Pipeline | Buffer the event in a coalescing window. After the window closes, generate a batched notification ("Alice and 48 others loved your post"). | 5-60 seconds |
| News Feed Ranking | Update the engagement signal for this post in the ranking model. A post receiving many reactions is boosted in friends' feeds. | < 5 seconds |
| Analytics / Trending | Increment real-time counters for trending detection. Update per-reaction-type sentiment signals for content moderation. | < 10 seconds |
| Search Indexing | Update the post's engagement score in the search index for relevance ranking. | < 30 seconds |

---

## 3. Idempotency

Client retries are a given -- mobile networks are unreliable, and users double-tap.
The write path must be idempotent: processing the same request twice must produce the
same result as processing it once.

### Natural Idempotency from Upsert Semantics

The `UNIQUE INDEX (user_id, entity_id)` constraint provides natural idempotency:

```
Request 1: User 12345 reacts "love" to post_82649173
  -> INSERT succeeds. Count incremented. Event published.

Request 2 (retry): User 12345 reacts "love" to post_82649173
  -> INSERT hits duplicate key. ON DUPLICATE KEY UPDATE sets
     reaction_type = 'love' (same value). No count change needed.
     This is effectively a no-op.
```

### Idempotency Key for Non-Upsert Operations

For removal (`DELETE`), the natural idempotency key is `(userId, entityId)`:
- If the reaction record does not exist, the DELETE is a no-op.
- If it was already deleted, the second DELETE has no effect.

For defense-in-depth, the API gateway can also use the `X-Request-ID` header:

```
┌──────────────────────────────────────────────────────────────┐
│ Idempotency Cache (Redis, TTL = 5 minutes)                   │
│                                                              │
│ Key: "idempotency:{userId}:{requestId}"                      │
│ Value: serialized response                                   │
│                                                              │
│ On request:                                                  │
│   1. Check cache for (userId, requestId)                     │
│   2. If hit -> return cached response (skip all processing)  │
│   3. If miss -> process request, cache response, return      │
└──────────────────────────────────────────────────────────────┘
```

This protects against edge cases where the upsert semantics alone are insufficient --
for example, if the event publish to Kafka should not be duplicated (even though Kafka
consumers should also be idempotent).

---

## 4. Rate Limiting

### Per-User Rate Limit

```
Limit: 100 reactions per minute per user
Algorithm: Token bucket at the API gateway level
Storage: Redis (INCR with TTL)
```

**Why 100/min?** A human can reasonably tap reactions on ~1-2 posts per second while
scrolling a feed. 100/min allows for burst scrolling behavior while blocking automated
bots that might attempt thousands of reactions per second.

```
Token Bucket (per user):
┌─────────────────────────────────────────────────┐
│ Bucket capacity: 100 tokens                      │
│ Refill rate: 100 tokens per 60 seconds           │
│                                                  │
│ Each reaction consumes 1 token.                  │
│ If bucket is empty -> 429 Too Many Requests      │
│                                                  │
│ Redis implementation:                            │
│   Key: "ratelimit:reactions:{userId}"            │
│   INCR + EXPIRE (60s window)                     │
│   If count > 100 -> reject                       │
└─────────────────────────────────────────────────┘
```

### Per-Entity Rate Limit

```
Limit: 10,000 reactions per second per entity
Purpose: Protect a single DB shard from being overwhelmed by a viral post
Algorithm: Sliding window counter
```

When a post exceeds the per-entity threshold, the system does not reject the reaction.
Instead, it **routes the write to the hot-key pipeline** (write buffering + sharded
counters -- see Section 5). The user's reaction is still accepted; it just takes a
different, more resilient code path.

```
if (getEntityReactionRate(entityId) > HOTKEY_THRESHOLD) {
    // Route to hot-key write path
    hotKeyWriteService.bufferReaction(userId, entityId, reactionType);
} else {
    // Normal write path (direct to TAO)
    taoClient.upsertReaction(userId, entityId, reactionType);
}
```

### Rate Limit Response

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 12
Content-Type: application/json

{
  "error": {
    "code": "RATE_LIMITED",
    "message": "Too many reactions. Please wait before reacting again.",
    "retryAfterSeconds": 12
  }
}
```

---

## 5. Celebrity / Viral Post Write Problem

This is the single hardest scaling problem in the reaction write path.

### The Scale Math

```
Celebrity with 100M followers posts a photo.
Post goes viral. 10% of followers react in the first hour.

  10M reactions / 3600 seconds = ~2,778 reactions/second (average)

But reactions are not uniform -- the first 10 minutes see 50% of the volume:

  5M reactions / 600 seconds = ~8,333 reactions/second (burst)

Peak spike (first 60 seconds after post):
  ~167,000 reactions/second

Single MySQL shard capacity: ~10,000 writes/second

  167,000 / 10,000 = 16.7x over capacity

  Result: shard overwhelmed, write latency spikes, timeouts, cascading failures.
```

### Solution 1: Write Buffering (Redis)

Buffer individual reactions in Redis for 1-5 seconds, then batch-write to MySQL.

```
┌──────────┐     ┌──────────────────┐     ┌──────────────────┐
│ Reaction │     │ Redis Buffer     │     │ Batch Writer     │
│ Service  │────>│                  │────>│ (every 1-5 sec)  │
│          │     │ LPUSH            │     │                  │
└──────────┘     │ reactions:buffer │     │ Drain buffer,    │
                 │ :post_82649173   │     │ batch INSERT     │
                 │                  │     │ to MySQL         │
                 │ [reaction1,      │     │                  │
                 │  reaction2,      │     │ Single batch of  │
                 │  reaction3, ...] │     │ 500 rows instead │
                 └──────────────────┘     │ of 500 individual│
                                          │ writes           │
                                          └──────────────────┘
```

**Trade-offs:**
- Pro: Reduces per-second write pressure on MySQL by 100-500x (batching).
- Pro: Redis handles 100K+ writes/sec easily.
- Con: If Redis crashes before the batch is flushed, buffered reactions are lost.
  Acceptable for counts (reconciliation will fix); problematic for individual reaction
  records (user thinks they reacted but the record is gone).
- Mitigation: Use Redis with AOF persistence (fsync every second) to limit data loss
  to at most 1 second of reactions.

### Solution 2: Sharded Counters

Instead of one counter row per (entityId, reactionType), maintain N counter shards:

```
BEFORE (single counter):
┌───────────────────────────────────────────┐
│ reaction_counts                            │
│ entity_id       | type | count            │
│ post_82649173   | like | 1,247,892        │  <-- ALL writes hit this row
│ post_82649173   | love |   340,551        │
└───────────────────────────────────────────┘

AFTER (sharded counters, N = 256):
┌───────────────────────────────────────────────────┐
│ reaction_counts_sharded                            │
│ entity_id       | type | shard_id | count         │
│ post_82649173   | like |    0     |  4,874        │
│ post_82649173   | like |    1     |  4,921        │
│ post_82649173   | like |    2     |  4,887        │
│ ...             | ...  |   ...    |   ...         │
│ post_82649173   | like |   255    |  4,903        │
└───────────────────────────────────────────────────┘

Write: Pick random shard_id, INCREMENT that shard's count.
Read:  SELECT SUM(count) WHERE entity_id = ? AND type = ? (sum 256 rows).
```

**Why this works:**

```
167K writes/sec to a single row       --> single row lock contention, DB overwhelmed
167K writes/sec across 256 shards     --> ~652 writes/sec per shard, easily handled

Read cost: SUM of 256 rows is ~0.5ms (all on the same MySQL shard, sequential scan
of 256 small rows). And the SUM result is cached -- so reads only hit the DB on
cache miss (< 1% of the time for popular posts).
```

**When to shard:** Not all entities need sharded counters. A post with 5 reactions
does not need 256 counter shards. The system **dynamically promotes** entities to
sharded counters when their reaction rate exceeds a threshold:

```
if (reactionRatePerSecond(entityId) > 1000) {
    // Promote to sharded counters
    promoteToShardedCounters(entityId, numShards=256);
}
```

### Solution 3: Async Count Propagation via Kafka

Decouple the reaction record write from the count update entirely:

```
┌──────────────┐     ┌─────────┐     ┌─────────────────┐     ┌──────────────┐
│ Reaction     │     │  MySQL  │     │     Kafka       │     │ Count        │
│ Service      │────>│  (TAO)  │────>│  reaction.events│────>│ Aggregator   │
│              │     │         │     │                 │     │ Consumer     │
│ Writes the   │     │ Store   │     │ Event:          │     │              │
│ individual   │     │ reaction│     │ {ADD, love,     │     │ Applies      │
│ record ONLY  │     │ record  │     │  post_82649173} │     │ count        │
│              │     │         │     │                 │     │ increments   │
│ Does NOT     │     │         │     │                 │     │ at its own   │
│ touch counts │     │         │     │                 │     │ pace         │
└──────────────┘     └─────────┘     └─────────────────┘     └──────────────┘
```

**Trade-offs:**
- Pro: Write path latency is minimized (only one DB write, no count update).
- Pro: Kafka absorbs the spike; the count aggregator consumes at a steady rate.
- Con: Counts are delayed by Kafka consumer lag (seconds to minutes during spikes).
- Con: If the count aggregator crashes, it must replay events from Kafka to catch up.

### Solution 4: Queue-Based Write Leveling

Use Kafka as a **write-ahead buffer** for the entire write path, not just counts:

```
Client --> API Gateway --> Kafka --> DB Writer Consumer --> MySQL

The API gateway writes the reaction event to Kafka and returns 202 Accepted.
The DB writer consumer reads from Kafka at a controlled rate (matching the
DB's capacity) and writes to MySQL.
```

**Trade-offs:**
- Pro: The database is never overwhelmed; it processes at its own pace.
- Con: The reaction is not immediately persisted. If the user refreshes, they may not
  see their reaction for a few seconds.
- Con: The API returns 202 (accepted) not 200 (completed). The client must handle
  the "pending" state.

### Facebook's Likely Approach [INFERRED]

Facebook most likely uses a combination:
1. **TAO's write-through** for normal traffic (direct write to leader MySQL + cache).
2. **Write buffering in a Redis-like layer** for detected hot keys, flushing in batches.
3. **Sharded counters** for entity counts on viral posts, dynamically promoted.
4. **Kafka for async downstream processing** (notifications, analytics, feed ranking)
   but NOT for the primary reaction record write (which goes directly to TAO for
   consistency).

---

## 6. Transactional Concerns

### The Ideal: Single Transaction

In a perfect world, every reaction write is a single ACID transaction:

```sql
BEGIN TRANSACTION;

-- 1. Upsert the reaction record
INSERT INTO reactions (user_id, entity_id, reaction_type, created_at)
VALUES (12345, 'post_82649173', 'love', NOW())
ON DUPLICATE KEY UPDATE reaction_type = 'love', updated_at = NOW();

-- 2. Decrement old type count (if type change)
UPDATE reaction_counts SET count = count - 1
WHERE entity_id = 'post_82649173' AND reaction_type = 'like';

-- 3. Increment new type count
UPDATE reaction_counts SET count = count + 1
WHERE entity_id = 'post_82649173' AND reaction_type = 'love';

COMMIT;
```

This works when both tables are on the **same MySQL shard** (which they are, if
sharded by `entityId`). TAO's leader handles this within a single MySQL instance.

### The Reality: When Transactions Are Not Enough

Transactions solve the local consistency problem, but distributed concerns remain:

```
┌────────────────────────────────────────────────────────────────────────┐
│ What a single MySQL transaction guarantees:                            │
│                                                                        │
│  + Reaction record and count are atomically updated on the leader     │
│  + If the transaction fails, both are rolled back                     │
│  + Local consistency is maintained                                    │
│                                                                        │
│ What it does NOT guarantee:                                           │
│                                                                        │
│  - Cache in other regions may be stale (replication lag)              │
│  - Kafka event may not be published (if publish fails after commit)   │
│  - Notification may never be generated (Kafka consumer failure)       │
│  - Another user reading from a follower region sees old counts        │
└────────────────────────────────────────────────────────────────────────┘
```

### Eventual Consistency Model

Facebook's practical approach:

1. **Reaction record + count update**: Strongly consistent within the leader region
   (single MySQL transaction via TAO leader).
2. **Cache update in leader region**: Write-through -- TAO updates the Memcache entry
   synchronously after the DB commit succeeds.
3. **Cache update in follower regions**: Eventually consistent. TAO's `mcsqueal`
   daemons in the leader region tail the MySQL binlog and issue cache invalidation
   messages to follower regions. Convergence: 1-5 seconds cross-region.
4. **Kafka event publish**: At-least-once delivery. If the publish fails, the reaction
   service retries. Kafka consumers are idempotent.

### Periodic Reconciliation

Even with transactions, counts can drift over time due to:
- Partial failures during type changes that span sharded counter updates
- Race conditions between concurrent reactions and unreactions
- Bug fixes that retroactively change counting logic

The reconciliation job runs periodically (hourly for hot entities, daily for cold):

```sql
-- Recompute actual count from individual records
SELECT entity_id, reaction_type, COUNT(*) AS actual_count
FROM reactions
WHERE entity_id = ?
GROUP BY entity_id, reaction_type;

-- Compare with pre-aggregated count
SELECT entity_id, reaction_type, count AS cached_count
FROM reaction_counts
WHERE entity_id = ?;

-- If drift detected, correct it
UPDATE reaction_counts
SET count = <actual_count>
WHERE entity_id = ? AND reaction_type = ?
  AND count != <actual_count>;
```

**Monitoring for drift:**
- Alert if `|actual_count - cached_count| / actual_count > 0.01` (more than 1% drift).
- Track drift magnitude over time as a metric.
- For entities with sharded counters, reconciliation sums all shards and compares to
  a `SELECT COUNT(*)` on the reaction records.

---

## 7. TAO's Role in the Write Path

TAO is not just a database -- it is the unified storage + caching layer that
Facebook's reaction write path is built on.

### TAO's Write Flow for a Reaction

```
┌──────────────┐
│ Reaction     │
│ Service      │
└──────┬───────┘
       │  assoc_add(REACTED_TO, userId, entityId, {type: 'love'})
       ▼
┌──────────────────┐
│ TAO Follower     │   (in the user's nearest region)
│ (Cache Layer)    │
│                  │──── Cache MISS for write ────>  Forward to Leader
└──────────────────┘
       │
       ▼
┌──────────────────┐
│ TAO Leader       │   (in the entity's home region)
│ (Cache + DB)     │
│                  │
│  1. Write to     │
│     MySQL        │
│  2. Update local │
│     Memcache     │
│     (write-      │
│      through)    │
│  3. Return       │
│     success      │
└──────┬───────────┘
       │
       ▼
┌──────────────────────────────────────────────────────────┐
│ Async Replication                                         │
│                                                           │
│  MySQL binlog --> mcsqueal daemons --> invalidate caches  │
│  in Follower regions (lease-based invalidation)           │
│                                                           │
│  The lease mechanism:                                     │
│  - When a cache entry is invalidated, a short lease is    │
│    set (typically 10 seconds).                            │
│  - During the lease period, only one thread can refill    │
│    the cache (prevents thundering herd on a popular post  │
│    whose cache was just invalidated).                     │
│  - Other threads either wait or serve stale data          │
│    (configurable per use case).                           │
└──────────────────────────────────────────────────────────┘
```

### Why TAO Instead of Raw MySQL + Memcache

| Concern | Raw MySQL + Memcache | TAO |
|---------|---------------------|-----|
| Cache invalidation | Application code must manually delete/update cache on every write. Error-prone, easy to miss a key. | Automatic. TAO's write-through ensures cache is always consistent with DB after a successful write. |
| Thundering herd | No built-in protection. A popular entity's cache expiring causes N threads to all query DB simultaneously. | Lease-based invalidation. Only one thread refills the cache; others wait or get stale data. |
| Cross-region consistency | Application must implement its own replication + invalidation protocol. | Built-in. `mcsqueal` tails binlog and invalidates follower caches. |
| Association counts | Application maintains a separate counts table and keeps it in sync. | First-class feature. TAO maintains association counts automatically. |
| Graph semantics | Application must model graph queries (e.g., "friends who reacted") on top of relational tables. | Native graph queries. `assoc_range`, `assoc_count` are primitives. |

---

## 8. Contrast with Twitter and Reddit

### Twitter/X: Binary Like/Unlike

```
Twitter write path (simplified):

  User taps heart --> POST /2/users/{userId}/likes
                  --> INSERT into likes(userId, tweetId, timestamp)
                  --> INCREMENT like_count on tweets table
                  --> Publish event

Key differences from Facebook:
  - No reaction types. Binary like/unlike. No CASE 3 (type change).
  - Simpler upsert: INSERT or DELETE, never UPDATE.
  - Same hot-spot problem for viral tweets (celebrity with 100M+ followers).
  - Twitter uses Manhattan (distributed KV store) instead of TAO.
```

### Reddit: Upvote/Downvote with Net Score

```
Reddit write path (simplified):

  User taps upvote --> POST /api/vote
                   --> INSERT/UPDATE votes(userId, thingId, direction)
                   --> UPDATE things SET score = score + delta

Key differences from Facebook:
  - Two vote types: up (+1) and down (-1). Net score = SUM(votes).
  - Vote changes need similar decrement/increment logic:
    - Change from upvote to downvote: score delta = -2 (remove +1, add -1).
    - Same atomicity concerns as Facebook's type change.
  - Reddit uses "vote fuzzing" -- intentionally adds noise to displayed
    scores. This reduces the precision requirement, allowing more
    aggressive caching and eventual consistency.
  - Reddit scores directly determine post ranking (score-based sorting).
    Facebook reactions are one signal among many in an ML-based News Feed
    ranking model.
```

### Summary Comparison Table

| Aspect | Facebook Reactions | Twitter Likes | Reddit Votes |
|--------|-------------------|---------------|-------------|
| Types | 6 (like, love, haha, wow, sad, angry) | 1 (like) | 2 (up, down) |
| Upsert complexity | High (type change = 3 writes) | Low (toggle on/off) | Medium (direction change = score delta) |
| Count model | Per-type counts + total | Single count | Net score (up - down) |
| Hot-spot severity | Same | Same | Same |
| Count accuracy | Eventually consistent, ~5-10s lag | Eventually consistent | Intentionally fuzzy |
| Storage | TAO (MySQL + Memcache) | Manhattan (distributed KV) | PostgreSQL + Redis |
| Async pipeline | Kafka (notifications, feed ranking, analytics) | Kafka-like (notifications, timeline ranking) | RabbitMQ/Kafka (notifications, ranking) |

---

## 9. End-to-End Latency Budget

Every millisecond matters for a "like" button. Users expect near-instant feedback.

```
┌─────────────────────────────────────────────────────────────────┐
│ Write Path Latency Budget (p99 target: < 200ms)                  │
│                                                                  │
│ Step                              │ p50    │ p99    │ Budget     │
│ ─────────────────────────────────│────────│────────│──────────  │
│ Network: client -> API gateway   │  20ms  │  80ms  │  80ms      │
│ Rate limit check (Redis)         │   1ms  │   3ms  │   5ms      │
│ Existing reaction lookup (cache) │   1ms  │   5ms  │  10ms      │
│ TAO write (MySQL + cache update) │   5ms  │  20ms  │  30ms      │
│ Kafka publish                    │   2ms  │  10ms  │  15ms      │
│ Response serialization           │  <1ms  │   1ms  │   2ms      │
│ Network: API gateway -> client   │  20ms  │  80ms  │  80ms      │
│ ─────────────────────────────────│────────│────────│──────────  │
│ TOTAL                            │ ~50ms  │ ~200ms │ ~222ms     │
│                                                                  │
│ Note: Kafka publish is fire-and-forget (async ack). The API does │
│ not wait for Kafka broker acknowledgment before returning to the │
│ client. If the publish fails, a retry mechanism re-publishes.    │
└─────────────────────────────────────────────────────────────────┘
```

The async path (notifications, analytics, feed ranking) adds seconds to minutes
of additional processing, but the user does not wait for any of it.

---

## 10. Failure Modes and Recovery

### What Happens When Things Break

| Failure | Impact | Recovery |
|---------|--------|----------|
| MySQL shard down | Reactions for entities on that shard cannot be written. Read path serves stale cached data. | Failover to MySQL replica (promoted to leader). TAO handles this transparently. Writes are unavailable for ~30 seconds during failover. |
| Memcache (TAO cache) down | All reads for cached entities fall through to MySQL. DB load spikes 100x. | TAO's lease mechanism prevents thundering herd. Cache rebuilds gradually. If multiple cache nodes fail, TAO can route to other cache replicas (TAO uses cache pools). |
| Kafka broker down | Async events (notifications, analytics) are delayed. Reaction records are still written (Kafka is not in the synchronous path). | Kafka replication (3 replicas per partition). Producer retries to a different broker. Consumer lag increases temporarily but catches up after recovery. |
| Redis (rate limiter / write buffer) down | Rate limiting is bypassed (fail-open policy -- allow all reactions rather than block all). Write buffering falls back to direct DB writes. | Redis Sentinel or Cluster handles failover. During failover, the system degrades gracefully (no rate limiting, no write buffering) rather than failing hard. |
| Count drift detected | Displayed reaction counts are slightly wrong (e.g., shows 1,200 but actual is 1,203). | Periodic reconciliation job corrects the drift. For high-profile entities (flagged by monitoring), reconciliation runs immediately. |
| Kafka consumer lag spike | Notifications delayed by minutes. Analytics counters are stale. News Feed ranking uses outdated reaction signals. | Auto-scale consumer instances. If lag exceeds threshold, alert on-call. Consumers are idempotent, so replaying events after recovery is safe. |

### Graceful Degradation Priority

When under extreme load (e.g., a global event causing reaction spikes across all
regions), the system sheds load in this order:

```
PRIORITY 1 (NEVER sacrifice): Individual reaction record persistence
  - The user's reaction must be durably stored.
  - If this fails, the user sees an error.

PRIORITY 2 (degrade gracefully): Pre-aggregated counts
  - Counts can be eventually consistent.
  - Under extreme load, batch count updates or defer to reconciliation.

PRIORITY 3 (delay acceptable): Notifications
  - Notifications can be delayed by minutes during spikes.
  - The coalescing window naturally absorbs the delay.

PRIORITY 4 (best effort): Analytics and trending
  - Analytics counters can be hours behind during an incident.
  - Trending detection may miss short-lived spikes -- acceptable.
```

---

## Key Takeaways for Interview

1. **Upsert semantics are the core complexity.** The type-change case (decrement old +
   increment new) is where atomicity matters. Explain CASE 3 clearly.

2. **The write path is two halves:** synchronous (DB write + cache update + return to
   client) and asynchronous (notifications, analytics, feed ranking via Kafka).

3. **Celebrity posts are THE scaling problem.** Know the math: 100M followers, 10%
   react in an hour, 167K reactions/sec peak vs 10K writes/sec per shard. Know the
   solutions: write buffering, sharded counters, async count propagation.

4. **Idempotency comes free from upsert semantics.** The `UNIQUE(userId, entityId)`
   constraint means retries are safe.

5. **Eventual consistency is the pragmatic choice.** Counts lag by seconds. The
   reconciliation job fixes drift. Strong consistency for counts would require
   distributed transactions -- unacceptable latency for a "like" button.

6. **TAO is why Facebook can do this at scale.** Write-through caching, lease-based
   invalidation, automatic association counts, cross-region replication -- TAO
   provides all of these as platform primitives so the reaction service does not have
   to build them from scratch.
