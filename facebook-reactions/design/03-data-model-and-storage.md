# Data Model & Storage Layer — Facebook Reactions

> The data model is deceptively simple on the surface — a five-column tuple — but the uniqueness
> constraint, pre-aggregated counts, and Facebook-scale sharding make it one of the most
> interesting storage problems in social networking.

---

## Table of Contents

1. [Reaction Record Schema](#1-reaction-record-schema)
2. [Pre-Aggregated Counts Table](#2-pre-aggregated-counts-table)
3. [Storage Options Analysis](#3-storage-options-analysis)
4. [Sharding Strategy](#4-sharding-strategy)
5. [Denormalization & Caching](#5-denormalization--caching)
6. [Contrasts with Other Platforms](#6-contrasts-with-other-platforms)

---

## 1. Reaction Record Schema

Every reaction in the system is captured as a single record — one row per user per entity. This is
the source of truth for "who reacted to what."

### Core Tuple

```
(userId, entityId, entityType, reactionType, timestamp)
```

| Column         | Type        | Description                                                      |
|----------------|-------------|------------------------------------------------------------------|
| `userId`       | BIGINT      | The user who reacted. References the users table/object.         |
| `entityId`     | BIGINT      | The post, comment, message, or story being reacted to.           |
| `entityType`   | TINYINT     | Discriminator: 1=post, 2=comment, 3=message, 4=story.           |
| `reactionType` | TINYINT     | Reaction kind: 1=like, 2=love, 3=haha, 4=wow, 5=sad, 6=angry.  |
| `timestamp`    | BIGINT      | Epoch milliseconds when the reaction was created/last changed.   |

### Primary Key: `(userId, entityId)`

This is the most important design decision in the entire schema. The composite primary key
`(userId, entityId)` enforces a **uniqueness constraint**: one reaction per user per entity.

```
┌──────────────────────────────────────────────────────────────────────┐
│                    reactions table                                    │
├──────────┬──────────┬────────────┬──────────────┬───────────────────┤
│ userId   │ entityId │ entityType │ reactionType │ timestamp         │
│ (PK)     │ (PK)     │            │              │                   │
├──────────┼──────────┼────────────┼──────────────┼───────────────────┤
│ 42       │ 90001    │ 1 (post)   │ 1 (like)     │ 1708300000000     │
│ 42       │ 90002    │ 1 (post)   │ 2 (love)     │ 1708300100000     │
│ 73       │ 90001    │ 1 (post)   │ 3 (haha)     │ 1708300200000     │
│ 73       │ 90003    │ 2 (comment)│ 1 (like)     │ 1708300300000     │
└──────────┴──────────┴────────────┴──────────────┴───────────────────┘
```

**Why this primary key?**

- User 42 taps "like" on post 90001 --> INSERT row (42, 90001, 1, 1, now).
- User 42 changes to "love" on post 90001 --> UPDATE row SET reactionType=2, timestamp=now WHERE userId=42 AND entityId=90001.
- User 42 tries to add a second reaction to post 90001 --> **rejected by unique constraint**.
- The UPSERT operation (`INSERT ... ON DUPLICATE KEY UPDATE`) handles all three cases atomically.

### Why TINYINT for reactionType (not ENUM)

Using `ENUM('like','love','haha','wow','sad','angry')` is tempting but dangerous at scale:

| Approach          | Add a new reaction type?                                         |
|-------------------|------------------------------------------------------------------|
| `ENUM` column     | `ALTER TABLE` on a trillion-row table. Days/weeks of migration.  |
| `TINYINT` column  | Add entry to application-level registry. Zero schema change.     |
| `VARCHAR` column  | Wastes storage at trillions of rows. Typos cause silent bugs.    |

Facebook uses integer type IDs with an application-level **type registry** — a configuration
mapping that maps `typeId --> (name, emoji, active, addedDate)`. Adding the "Care" reaction in
2020 was a registry update + client code push, not a schema migration.

```
┌──────────────────────────────────────────────┐
│         reaction_type_registry               │
├────────┬──────────┬────────┬────────────────┤
│ typeId │ name     │ emoji  │ active         │
├────────┼──────────┼────────┼────────────────┤
│ 1      │ like     │ thumb  │ true           │
│ 2      │ love     │ heart  │ true           │
│ 3      │ haha     │ laugh  │ true           │
│ 4      │ wow      │ gasp   │ true           │
│ 5      │ sad      │ tear   │ true           │
│ 6      │ angry    │ steam  │ true           │
│ 7      │ care     │ hug    │ true (2020+)   │
└────────┴──────────┴────────┴────────────────┘
```

### Secondary Indexes

The primary key `(userId, entityId)` optimizes lookups by user ("did I react to this post?").
But the read-hot path is the opposite direction: "who reacted to this entity, grouped by type?"

**Secondary index: `(entityId, reactionType)`**

This index serves two query patterns:

1. **List who reacted**: `SELECT userId FROM reactions WHERE entityId = ? AND reactionType = ? ORDER BY timestamp DESC LIMIT 20` -- paginated list of users who "loved" a post.
2. **Reconciliation**: `SELECT reactionType, COUNT(*) FROM reactions WHERE entityId = ? GROUP BY reactionType` -- recount from raw records to verify pre-aggregated counts.

```
Query Patterns and Which Index Serves Them
───────────────────────────────────────────

"Did user 42 react to post 90001?"
  └── PRIMARY KEY (userId, entityId)     ← O(1) lookup

"Who loved post 90001?"
  └── INDEX (entityId, reactionType)     ← Range scan, paginated

"What are the exact counts for post 90001?"
  └── reaction_counts table              ← O(1) lookup (see Section 2)
  └── INDEX (entityId, reactionType)     ← Reconciliation fallback
```

---

## 2. Pre-Aggregated Counts Table

Computing `SELECT COUNT(*) ... GROUP BY reactionType` on every post render is infeasible at
Facebook scale. A post with 10 million reactions would take seconds to count — and this query
runs billions of times per day (every News Feed render for every visible post).

### Schema

```
┌──────────────────────────────────────────────┐
│           reaction_counts                     │
├────────────┬──────────────┬──────────────────┤
│ entityId   │ reactionType │ count            │
│ (PK)       │ (PK)        │                  │
├────────────┼──────────────┼──────────────────┤
│ 90001      │ 1 (like)     │ 1,247,892        │
│ 90001      │ 2 (love)     │ 340,215          │
│ 90001      │ 3 (haha)     │ 56,003           │
│ 90001      │ 4 (wow)      │ 12,891           │
│ 90001      │ 5 (sad)      │ 3,402            │
│ 90001      │ 6 (angry)    │ 1,117            │
│ 90002      │ 1 (like)     │ 42               │
│ 90002      │ 2 (love)     │ 7                │
└────────────┴──────────────┴──────────────────┘
```

**Primary key: `(entityId, reactionType)`** -- fetching all counts for a post is a single
partition scan returning at most 6-7 rows (one per reaction type).

### Update Mechanics

Every react/unreact operation updates the counts table:

```
User adds "like" to post 90001:
  INSERT INTO reactions (userId, entityId, entityType, reactionType, timestamp)
    VALUES (42, 90001, 1, 1, NOW())
    ON DUPLICATE KEY UPDATE reactionType = 1, timestamp = NOW();
  UPDATE reaction_counts SET count = count + 1
    WHERE entityId = 90001 AND reactionType = 1;

User changes from "like" to "love" on post 90001:
  UPDATE reactions SET reactionType = 2, timestamp = NOW()
    WHERE userId = 42 AND entityId = 90001;
  UPDATE reaction_counts SET count = count - 1
    WHERE entityId = 90001 AND reactionType = 1;    -- decrement old
  UPDATE reaction_counts SET count = count + 1
    WHERE entityId = 90001 AND reactionType = 2;    -- increment new

User removes reaction from post 90001:
  DELETE FROM reactions WHERE userId = 42 AND entityId = 90001;
  UPDATE reaction_counts SET count = count - 1
    WHERE entityId = 90001 AND reactionType = 1;    -- decrement
```

### The Count Consistency Challenge

The reaction record write and the count update are **two separate operations**. If one succeeds
and the other fails, counts drift from reality.

```
Failure Scenario: Count Drift
─────────────────────────────

  1. User reacts "like" to post 90001
  2. INSERT into reactions table     ──── SUCCESS
  3. UPDATE reaction_counts +1       ──── FAILS (timeout, shard down, etc.)
                                          │
                                          ▼
  Result: Reaction record exists, but count is one less than reality.
  Over millions of operations, small errors accumulate.
```

**Why not a single transaction?** At Facebook's scale — hundreds of thousands of shards, cross-
region replication, millions of writes per second — wrapping both writes in a distributed
transaction adds unacceptable latency (10-50ms per reaction). For a "like" button that users
expect to respond in < 100ms, this overhead is too high.

### Solution: Periodic Reconciliation

A background job periodically recomputes the true count from individual reaction records and
corrects any drift:

```
Reconciliation Job (runs hourly/daily per entity):
──────────────────────────────────────────────────

  1. SELECT reactionType, COUNT(*) as trueCount
     FROM reactions
     WHERE entityId = ?
     GROUP BY reactionType;

  2. Compare trueCount with reaction_counts.count for each type.

  3. If |trueCount - storedCount| > 0:
       UPDATE reaction_counts SET count = trueCount
       WHERE entityId = ? AND reactionType = ?;
       LOG drift amount for monitoring.

  4. Prioritize reconciliation for popular entities (more writes = more drift risk).
```

This approach accepts eventual consistency for counts (seconds to hours of possible drift) in
exchange for low-latency writes. The individual reaction records remain the authoritative source
of truth — the counts table is a **materialized aggregate** that trades accuracy for read speed.

---

## 3. Storage Options Analysis

Four storage systems are relevant, each with different strengths. The right choice depends on
scale, consistency requirements, and operational maturity.

### 3.1 MySQL / PostgreSQL (Relational)

```
CREATE TABLE reactions (
    user_id     BIGINT NOT NULL,
    entity_id   BIGINT NOT NULL,
    entity_type TINYINT NOT NULL,
    reaction_type TINYINT NOT NULL,
    created_at  BIGINT NOT NULL,
    PRIMARY KEY (user_id, entity_id),
    INDEX idx_entity_type (entity_id, reaction_type)
);

-- Atomic upsert:
INSERT INTO reactions (user_id, entity_id, entity_type, reaction_type, created_at)
VALUES (42, 90001, 1, 2, UNIX_TIMESTAMP())
ON DUPLICATE KEY UPDATE
    reaction_type = VALUES(reaction_type),
    created_at = VALUES(created_at);
```

| Strength                                  | Weakness                                      |
|-------------------------------------------|-----------------------------------------------|
| `UNIQUE INDEX` enforces one-reaction-per-user natively | Single instance caps at ~10K writes/sec   |
| `UPSERT` via `INSERT ON DUPLICATE KEY UPDATE` is atomic | Must shard manually (no built-in horizontal scale) |
| ACID transactions for count + record in one txn (single shard) | Cross-shard transactions are expensive    |
| Mature tooling, well-understood operations | JOIN-heavy queries across shards are impractical |

**Verdict:** Excellent for the underlying storage engine when sharded. Facebook's TAO is built
on top of MySQL — the relational engine provides the durability and consistency guarantees, while
TAO adds caching and distribution on top.

### 3.2 Cassandra (Wide-Column)

```
CREATE TABLE reactions (
    entity_id    BIGINT,
    user_id      BIGINT,
    entity_type  TINYINT,
    reaction_type TINYINT,
    created_at   TIMESTAMP,
    PRIMARY KEY (entity_id, user_id)
);
-- Partition key: entity_id  (all reactions for a post on same node)
-- Clustering key: user_id   (unique per user within partition)
```

| Strength                                  | Weakness                                      |
|-------------------------------------------|-----------------------------------------------|
| Partition key = entityId gives natural grouping for "all reactions on post X" | No native UPSERT with conditional logic |
| Built-in horizontal scaling and replication | Lightweight transactions (LWT) are expensive (~4x latency) |
| Tunable consistency (ONE, QUORUM, ALL)     | Last-writer-wins can cause silent overwrites   |
| Handles high write throughput natively     | No secondary indexes without materialized views |

**The UPSERT problem with Cassandra:** Cassandra's write model is "last write wins" by default.
If a user changes from "like" to "love," a simple write will overwrite the old reaction — but it
will NOT atomically decrement the old count and increment the new count. Achieving this requires
either:
- **Lightweight Transactions (LWT):** `INSERT ... IF NOT EXISTS` or `UPDATE ... IF reaction_type = 'like'`. These use Paxos consensus and add 4-10x latency.
- **Accept eventual consistency:** Write the new reaction, publish an event, and let an async consumer handle the count adjustment. Simpler but counts can drift.

**Verdict:** A viable choice for companies that need horizontal scale without building a custom
storage layer. The lack of native transactional upsert is manageable with async count
reconciliation.

### 3.3 Redis (In-Memory)

```
# Store reaction counts per entity using a Hash:
HSET entity:90001:counts like 1247892
HSET entity:90001:counts love 340215
HSET entity:90001:counts haha 56003

# Atomic increment on new reaction:
HINCRBY entity:90001:counts love 1

# Atomic decrement on unreact:
HINCRBY entity:90001:counts like -1

# Read all counts for a post (single command):
HGETALL entity:90001:counts
# Returns: {like: 1247892, love: 340215, haha: 56003, ...}
```

| Strength                                  | Weakness                                      |
|-------------------------------------------|-----------------------------------------------|
| Sub-millisecond reads and writes           | Memory is expensive: trillions of records at ~100 bytes each = petabytes of RAM |
| `HINCRBY` is atomic — no race conditions on counts | Not durable by default (RDB/AOF have gaps) |
| Perfect for the hot-count read path        | Cannot store full reaction history (who reacted) at scale |
| Natural Hash structure maps to per-entity counts | Single-threaded — limited write throughput per instance |

**Verdict:** Redis is the right tool for the **caching layer** — hot reaction counts and
summaries — but not for the **primary store**. Individual reaction records (who reacted to what)
must live in a durable, disk-backed store. Redis serves as L3 cache sitting in front of MySQL/TAO.

### 3.4 TAO (Facebook's Actual Solution)

TAO (The Associations and Objects) is Facebook's distributed data store, purpose-built for the
social graph. Published at USENIX ATC 2013, TAO stores the social graph as a labeled directed
multigraph of **Objects** and **Associations**.

```
TAO Data Model (labeled directed multigraph)
═══════════════════════════════════════════

  ┌─────────┐                           ┌─────────────┐
  │  User   │──── REACTION ────────────>│    Post      │
  │ (Object)│    (Association)          │  (Object)    │
  │ id: 42  │    type: LIKE             │  id: 90001   │
  │         │    timestamp: 170830...   │              │
  └─────────┘                           └─────────────┘
       │                                      ▲
       │                                      │
       │        ┌─────────┐                   │
       └─ REACTION ──>│  User   │── AUTHORED ─────┘
            type: LOVE │ id: 73  │
                       └─────────┘
```

**Objects** are typed nodes (users, posts, comments, pages). Each object has a type-specific
set of attributes (e.g., a post object has `author_id`, `text`, `created_at`).

**Associations** are typed directed edges between objects. A reaction is an association of type
`REACTION` from a user object to a post object, with metadata including `reactionType` and
`timestamp`.

#### TAO Architecture

```
                         TAO Architecture
┌──────────────────────────────────────────────────────────────────┐
│                        Clients (API Servers)                     │
└──────────────────────────┬───────────────────────────────────────┘
                           │ read / write
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│                    TAO Cache Layer (Followers)                    │
│                                                                  │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐        │
│  │ Follower │  │ Follower │  │ Follower │  │ Follower │  ...    │
│  │ Cache 1  │  │ Cache 2  │  │ Cache 3  │  │ Cache 4  │        │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘        │
│       │              │              │              │              │
│       └──────────────┴──────┬───────┴──────────────┘              │
│                             │                                    │
│                             ▼                                    │
│              ┌──────────────────────────────┐                    │
│              │    TAO Leader Cache          │                    │
│              │    (per-shard leader)        │                    │
│              └──────────────┬───────────────┘                    │
│                             │                                    │
└─────────────────────────────┼────────────────────────────────────┘
                              │ cache miss / write
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│                    MySQL Storage Layer                            │
│                                                                  │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐        │
│  │ Shard 1  │  │ Shard 2  │  │ Shard 3  │  │ Shard N  │  ...   │
│  │ (MySQL)  │  │ (MySQL)  │  │ (MySQL)  │  │ (MySQL)  │        │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘        │
│                                                                  │
│  Hundreds of thousands of shards                                 │
└──────────────────────────────────────────────────────────────────┘
```

#### Key TAO Properties

| Property                       | Detail                                                            |
|--------------------------------|-------------------------------------------------------------------|
| **Underlying storage**         | MySQL (InnoDB) for durable persistence                            |
| **Caching layer**              | Custom memcache-based cache (not vanilla Memcached)               |
| **Read throughput**            | Billions of reads/sec across all caches (TAO paper, USENIX ATC 2013) |
| **Write throughput**           | Millions of writes/sec (TAO paper)                                |
| **Shard count**                | Hundreds of thousands of shards (TAO paper)                       |
| **Consistency model**          | Eventually consistent; leader-follower with async replication     |
| **Cache invalidation**         | Lease-based: prevents thundering herd on cache miss               |
| **Cross-region replication**   | Asynchronous; one leader region for writes, all regions serve reads |
| **Association queries**        | `assoc_get(objectId, assocType)` -- get all associations of a type for an object |
| **Association count**          | `assoc_count(objectId, assocType)` -- cached count, maintained by TAO |

#### How Reactions Map to TAO

```
TAO API for Reactions
─────────────────────

Write a reaction (user 42 "likes" post 90001):
  assoc_add(42, REACTION, 90001, {reactionType: LIKE, time: now})

Change reaction (user 42 changes to "love"):
  assoc_change(42, REACTION, 90001, {reactionType: LOVE, time: now})

Remove reaction:
  assoc_delete(42, REACTION, 90001)

Get all reactions for a post:
  assoc_get(90001, REACTION)           --> list of (userId, reactionType, time)

Count reactions for a post:
  assoc_count(90001, REACTION)         --> total count

Check if user reacted:
  assoc_get(42, REACTION, 90001)       --> single association or null
```

TAO maintains the association count as a **first-class cached value** — `assoc_count` does not
scan all associations. It is updated on every `assoc_add` and `assoc_delete`, similar to the
pre-aggregated counts table described in Section 2 but managed internally by TAO.

#### Lease-Based Cache Invalidation

When a cache miss occurs, TAO uses **leases** to prevent thundering herd:

```
Without leases (thundering herd):
─────────────────────────────────
  Cache miss for entity 90001
    ├── Thread A: query MySQL --> slow query starts
    ├── Thread B: cache miss --> query MySQL --> duplicate slow query
    ├── Thread C: cache miss --> query MySQL --> duplicate slow query
    └── ... hundreds of threads hit MySQL simultaneously

With leases (TAO's approach):
─────────────────────────────
  Cache miss for entity 90001
    ├── Thread A: acquires lease, queries MySQL, populates cache
    ├── Thread B: cache miss, no lease available --> wait / use stale value
    ├── Thread C: cache miss, no lease available --> wait / use stale value
    └── Only ONE thread hits MySQL; others wait for cache to be populated
```

This mechanism is critical for viral posts where a cache invalidation could trigger thousands
of simultaneous MySQL queries for the same entity.

#### Why TAO Over Off-the-Shelf Databases

Facebook built TAO because no existing system met all requirements simultaneously:

1. **Graph semantics**: Objects and Associations are first-class concepts, not bolted-on.
2. **Integrated caching**: Caching is part of the storage API, not a separate layer to manage.
3. **Lease-based invalidation**: Prevents thundering herd natively.
4. **Per-type cache policies**: Reaction associations can have different TTLs than friendship associations.
5. **Cross-region consistency**: Leader-follower model with async replication built into the protocol.

**For any company below Facebook's scale** (< 1B users), sharded MySQL or PostgreSQL with a
Memcached/Redis caching layer is sufficient. TAO is a solution to problems that only emerge at
billions of users and trillions of associations.

### Comparison Matrix

```
┌──────────────────┬────────────────┬──────────────┬──────────────┬──────────────────┐
│                  │ MySQL (sharded)│ Cassandra    │ Redis        │ TAO              │
├──────────────────┼────────────────┼──────────────┼──────────────┼──────────────────┤
│ Uniqueness       │ UNIQUE INDEX   │ Partition +  │ Not native   │ Association      │
│ enforcement      │ (native)       │ clustering   │ (app-level)  │ semantics        │
│                  │                │ key          │              │ (native)         │
├──────────────────┼────────────────┼──────────────┼──────────────┼──────────────────┤
│ UPSERT support   │ INSERT ON      │ LWT (slow)   │ Not native   │ assoc_change     │
│                  │ DUPLICATE KEY  │ or last-     │              │ (native)         │
│                  │ (native)       │ writer-wins  │              │                  │
├──────────────────┼────────────────┼──────────────┼──────────────┼──────────────────┤
│ Read latency     │ ~1-5ms (disk)  │ ~2-10ms      │ < 1ms        │ < 1ms (cache hit)│
│                  │                │              │ (memory)     │ ~1-5ms (miss)    │
├──────────────────┼────────────────┼──────────────┼──────────────┼──────────────────┤
│ Write latency    │ ~2-10ms        │ ~2-10ms      │ < 1ms        │ ~5-15ms (write-  │
│                  │                │              │              │ through to MySQL)│
├──────────────────┼────────────────┼──────────────┼──────────────┼──────────────────┤
│ Horizontal scale │ Manual shard   │ Built-in     │ Cluster mode │ Built-in         │
│                  │ management     │ (ring-based) │ (limited)    │ (100K+ shards)   │
├──────────────────┼────────────────┼──────────────┼──────────────┼──────────────────┤
│ Cost at trillion │ Moderate       │ Moderate     │ Extremely    │ Moderate (MySQL  │
│ records          │ (disk-based)   │ (disk-based) │ expensive    │ storage + memory │
│                  │                │              │ (all in RAM) │ cache)           │
├──────────────────┼────────────────┼──────────────┼──────────────┼──────────────────┤
│ Best role in the │ Primary store  │ Primary store│ Cache layer  │ Complete stack   │
│ reaction system  │ (under TAO)    │ (alt. to     │ (hot counts  │ (Facebook's      │
│                  │                │ MySQL+TAO)   │ only)        │ actual choice)   │
└──────────────────┴────────────────┴──────────────┴──────────────┴──────────────────┘
```

---

## 4. Sharding Strategy

At trillions of reaction records, a single database instance is impossible. The data must be
distributed across hundreds of thousands of shards. The critical question: **what is the shard key?**

### Option A: Shard by entityId

All reactions for a given entity (post, comment, story) live on the same shard.

```
Shard by entityId
──────────────────

  Shard 1:  entityId % N == 0
  ┌───────────────────────────────────────────┐
  │ (user42, post_1000, LIKE)                 │
  │ (user73, post_1000, LOVE)                 │
  │ (user99, post_1000, HAHA)                 │
  │ (user42, post_2000, LIKE)                 │  <-- all reactions for post_1000
  │ (user55, post_2000, WOW)                  │      on the same shard
  └───────────────────────────────────────────┘

  Shard 2:  entityId % N == 1
  ┌───────────────────────────────────────────┐
  │ (user42, post_1001, LIKE)                 │
  │ (user73, post_1001, SAD)                  │
  │ (user99, post_3001, ANGRY)                │
  └───────────────────────────────────────────┘
```

**Advantages:**
- `assoc_get(entityId, REACTION)` is a single-shard query -- fast.
- `assoc_count(entityId, REACTION)` is a single-shard query -- fast.
- Aggregation (count by type) happens on one shard -- no scatter-gather.
- The hot read path (News Feed render needs counts per post) is optimized.

**Disadvantages:**
- "Show me everything user 42 has liked" requires a scatter-gather across ALL shards.
- A viral post creates a **write hot spot** -- all reactions for that post hammer one shard.

### Option B: Shard by userId

All reactions by a given user live on the same shard.

```
Shard by userId
───────────────

  Shard 1:  userId % N == 0
  ┌───────────────────────────────────────────┐
  │ (user_1000, post_42, LIKE)                │
  │ (user_1000, post_99, LOVE)                │  <-- all of user_1000's reactions
  │ (user_1000, comment_7, HAHA)              │      on the same shard
  │ (user_2000, post_55, LIKE)                │
  └───────────────────────────────────────────┘
```

**Advantages:**
- "Show me everything user 42 has liked" is a single-shard query.
- User-centric operations (activity history, data export, GDPR delete) are efficient.

**Disadvantages:**
- "Get all reactions for post 90001" requires scatter-gather across ALL shards.
- Aggregating counts per entity requires reading from many shards -- unacceptable for the hot path.

### Facebook's Choice: Entity-Sharding

Facebook shards by `entityId` because the **read path dominates the write path** by 100-1000x:

```
Access Pattern Analysis
───────────────────────

                    Frequency             Sharding Winner
                    ─────────             ───────────────
Show reaction       Billions/day          Entity-sharded
counts on a post    (every News Feed      (single shard read)
                     render)

"Did I react to     Billions/day          Entity-sharded
this post?"         (every News Feed      (single shard, lookup
                     render)               by userId+entityId)

"Show everything    Millions/day          User-sharded
I've liked"         (rare: only when      (single shard read)
                     user views activity
                     history)

React to a post     Billions/day          Entity-sharded
                    (write goes to        (single shard write)
                     entity's shard)
```

The first two patterns (the hot read path) represent > 99% of traffic. Entity-sharding makes
these O(1) single-shard operations. The rare "show my activity" query uses a scatter-gather
or a separate user-indexed secondary store.

### Shard Count

From the TAO paper (USENIX ATC 2013): Facebook uses **hundreds of thousands of shards**. Each
shard is a MySQL database instance. With consistent hashing and virtual nodes, adding or removing
shards requires minimal data movement.

---

## 5. Denormalization & Caching

Raw reaction records and pre-aggregated counts are not sufficient for the read path. The data
rendered on every post requires further denormalization and aggressive caching.

### What the Client Needs per Post

For every post visible in News Feed, the client needs:

```json
{
  "entityId": 90001,
  "reactionSummary": {
    "total": 1661520,
    "breakdown": {
      "like": 1247892,
      "love": 340215,
      "haha": 56003,
      "wow": 12891,
      "sad": 3402,
      "angry": 1117
    },
    "topTypes": ["like", "love", "haha"],
    "viewerReaction": "love",
    "friendsWhoReacted": [
      {"userId": 555, "name": "Alice", "reactionType": "love"},
      {"userId": 777, "name": "Bob", "reactionType": "like"},
      {"userId": 888, "name": "Charlie", "reactionType": "haha"}
    ]
  }
}
```

This payload combines data from multiple sources:
- `breakdown` and `total` -- from the `reaction_counts` table (or TAO's `assoc_count`).
- `topTypes` -- derived from the breakdown (top 3 by count).
- `viewerReaction` -- from the `reactions` table, lookup by `(viewerUserId, entityId)`.
- `friendsWhoReacted` -- set intersection of (viewer's friends) and (users who reacted to this entity). This is the most expensive computation.

### Denormalized Reaction Summary

The full summary is **precomputed and cached** rather than assembled on every request:

```
Cache Structure
───────────────

  Key:    entity:{entityId}:reaction_summary
  Value:  serialized JSON of counts + topTypes
  TTL:    ~60 seconds for popular posts
          ~300 seconds for older/less active posts

  Key:    entity:{entityId}:reactor_friends:{viewerUserId}
  Value:  list of top 3 friend userIds who reacted
  TTL:    ~120 seconds
```

**Why the short TTL?** Reaction counts change constantly on popular posts. A 60-second TTL
means a user might see a count that is up to 60 seconds stale -- perfectly acceptable. Showing
"1.2M likes" vs "1,200,042 likes" is indistinguishable to the user.

### Cache Layering

```
Multi-Layer Cache Architecture
──────────────────────────────

  ┌─────────────────────────────────────────────────────┐
  │ L1: Client-Side Cache (app memory)                  │
  │     - Posts currently on screen                     │
  │     - Prevents redundant API calls on scroll-back   │
  │     - TTL: session duration                         │
  └───────────────────────┬─────────────────────────────┘
                          │ cache miss
                          ▼
  ┌─────────────────────────────────────────────────────┐
  │ L2: CDN / Edge Cache                                │
  │     - NOT used for reaction summaries               │
  │     - Reason: "did I react?" is user-specific       │
  │       (personalized data cannot be edge-cached)     │
  └───────────────────────┬─────────────────────────────┘
                          │ not applicable
                          ▼
  ┌─────────────────────────────────────────────────────┐
  │ L3: Application Cache (TAO cache / Memcache)        │
  │     - Per-entity reaction summaries                 │
  │     - Key: entity:{entityId}:reaction_summary       │
  │     - Cache hit rate: 99%+ for popular posts        │
  │     - Lease-based invalidation prevents             │
  │       thundering herd                               │
  └───────────────────────┬─────────────────────────────┘
                          │ cache miss (< 1% of requests)
                          ▼
  ┌─────────────────────────────────────────────────────┐
  │ L4: MySQL / TAO Storage (source of truth)           │
  │     - reaction_counts table or TAO assoc_count      │
  │     - Only hit on cold start or after invalidation  │
  └─────────────────────────────────────────────────────┘
```

### Cache Invalidation on Reaction Write

When a user reacts, the cached summary must be updated or invalidated:

```
Write-Behind Invalidation Flow
──────────────────────────────

  1. User reacts "love" to post 90001
  2. Write to MySQL/TAO (primary store)       ── synchronous
  3. Publish event to Kafka                    ── synchronous
  4. Return success to user                    ── < 100ms total
  5. Kafka consumer receives event             ── async, ~100-500ms later
  6. Consumer invalidates cache key            ── async
     entity:90001:reaction_summary
  7. Next read triggers cache miss             ── cache repopulated from DB
     (or consumer proactively repopulates)

  Why write-behind (not write-through)?
  - Write-through: update cache synchronously on write.
    Adds latency to the write path.
  - Write-behind: write to DB, invalidate cache async.
    Lower write latency. Brief window of stale reads (~100-500ms).
    Acceptable for reaction counts.
```

Facebook's Memcache infrastructure uses **mcsqueal** daemons that tail the MySQL binlog and
invalidate corresponding cache entries. This is documented in the Memcache paper (NSDI 2013).
The mechanism ensures that every database write eventually invalidates the affected cache
entries without the application needing to explicitly manage invalidation.

---

## 6. Contrasts with Other Platforms

Different products make different data model choices based on their unique requirements. These
contrasts highlight why Facebook's choices are specific to Facebook's product.

### Instagram

| Aspect            | Facebook                                | Instagram                                |
|-------------------|-----------------------------------------|------------------------------------------|
| Reaction types    | 6 types (like, love, haha, wow, sad, angry) | Binary (like / no like)               |
| Record schema     | `(userId, entityId, entityType, reactionType, timestamp)` | `(userId, entityId, timestamp)` -- no reactionType needed |
| Count storage     | Per-type breakdown: `{like: 1200, love: 340, ...}` | Single number: `42,891 likes`         |
| Storage savings   | 6 counter rows per entity (one per type) | 1 counter row per entity               |
| Count display     | Always shown with type breakdown        | Hidden in some markets (only author sees exact count) |

Instagram's simpler model means fewer counter rows, no type-change upsert logic, and a simpler
client payload. Both are Meta properties and likely share TAO infrastructure, but Instagram's
reaction data model is a strict subset of Facebook's.

### YouTube

| Aspect            | Facebook                                | YouTube                                  |
|-------------------|-----------------------------------------|------------------------------------------|
| Reaction types    | 6 positive types                        | 2 types: like and dislike                |
| Aggregation       | Per-type counts                         | Net score (likes - dislikes) + separate counts |
| Count visibility  | All counts shown to all users           | Like count shown; dislike count hidden from viewers (since Nov 2021) |
| Storage impact    | Dislike count hidden = still stored, just not returned by the read API. No storage model change. |

YouTube's decision to hide dislike counts is a **product-level** change, not a storage-level
change. The data model still stores `(userId, videoId, voteType)` with `voteType IN (LIKE, DISLIKE)`.
The read API simply omits the dislike count for non-creators.

### Reddit

| Aspect            | Facebook                                | Reddit                                   |
|-------------------|-----------------------------------------|------------------------------------------|
| Reaction types    | 6 types (all positive engagement)       | 2 types: upvote / downvote               |
| Score model       | Per-type breakdown, no net score        | Net score = upvotes - downvotes          |
| Fuzzing           | Exact counts displayed                  | Vote counts intentionally fuzzed (noise added) to deter manipulation |
| Schema            | `reactionType TINYINT`                  | `vote_direction TINYINT` (-1, 0, +1)    |
| Aggregation       | `{like: N, love: M, ...}`              | Single integer: `score = sum(vote_direction)` |

Reddit's net-score model allows a simpler aggregation: a single integer per entity instead of
a per-type breakdown. The fuzzing (adding random noise to displayed counts) also reduces
precision requirements, allowing more aggressive caching since the displayed value is
approximate by design.

### Slack

| Aspect            | Facebook                                | Slack                                    |
|-------------------|-----------------------------------------|------------------------------------------|
| Reaction types    | Fixed set of 6 (extensible via registry) | **Arbitrary emoji string** -- any emoji, including custom workspace emoji |
| Schema            | `reactionType TINYINT` (bounded)        | `emojiCode VARCHAR` (unbounded)          |
| Count aggregation | 6 counters per entity (bounded)         | Potentially hundreds of counters per message (one per unique emoji used) |
| UI impact         | Predictable layout (max 6 reaction icons) | Dynamic layout (scrollable list of emojis) |
| Analytics         | Sentiment analysis tractable on 6 types | Sentiment analysis impractical on arbitrary emojis |

Slack's unbounded reaction type space is maximally expressive but creates fundamentally different
storage trade-offs. Instead of a fixed-width `reaction_counts` row with 6 columns, Slack needs
a **sparse mapping** from emoji codes to counts. At Slack's scale (much smaller than Facebook),
this is manageable. At Facebook's scale with trillions of reactions, the bounded type set is
essential for predictable storage and aggregation costs.

```
Data Model Spectrum: Constrained ◄──────────────────────────► Unconstrained

  Instagram         Facebook          YouTube/Reddit       Slack
  (binary like)     (6 fixed types)   (2 fixed types)      (arbitrary emoji)
      │                  │                  │                    │
      ▼                  ▼                  ▼                    ▼
  1 count/entity    6 counts/entity   2 counts + net      N counts/message
                                      score/entity        (N = unique emojis)
```

---

## Summary

The reaction data model rests on a few key decisions:

1. **`(userId, entityId)` as primary key** -- enforces one-reaction-per-user at the storage level.
2. **TINYINT + registry for reaction types** -- extensible without schema migration.
3. **Pre-aggregated counts table** -- trades write complexity for O(1) read on the hot path.
4. **Periodic reconciliation** -- accepts eventual consistency for counts, corrects drift offline.
5. **Entity-sharded storage** -- optimizes the dominant access pattern (post render in News Feed).
6. **TAO as the unified storage layer** -- MySQL for durability, memcache for speed, lease-based invalidation for consistency, all integrated into one API.
7. **Denormalized, cached summaries** -- the data rendered to users is a precomputed, cached view, not a live query.
