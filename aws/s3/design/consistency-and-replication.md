# Amazon S3 — Consistency & Replication Deep Dive

> Companion deep dive to the [interview simulation](interview-simulation.md). This document explores how S3 achieved strong read-after-write consistency and how cross-region/same-region replication works.

---

## Table of Contents

1. [The Consistency Story: A Historical Timeline](#1-the-consistency-story-a-historical-timeline)
2. [Why S3 Was Eventually Consistent Originally](#2-why-s3-was-eventually-consistent-originally)
3. [The Witness Protocol — How S3 Achieved Strong Consistency](#3-the-witness-protocol--how-s3-achieved-strong-consistency)
4. [Why the Witness Works Without Performance Penalty](#4-why-the-witness-works-without-performance-penalty)
5. [Witness Implementation Details](#5-witness-implementation-details)
6. [Handling Edge Cases in the Consistency Protocol](#6-handling-edge-cases-in-the-consistency-protocol)
7. [Cross-Region Replication (CRR)](#7-cross-region-replication-crr)
8. [Same-Region Replication (SRR)](#8-same-region-replication-srr)
9. [Replication Conflict Resolution](#9-replication-conflict-resolution)
10. [Consistency of Replicated Data](#10-consistency-of-replicated-data)
11. [Comparison: S3 Consistency vs Other Systems](#11-comparison-s3-consistency-vs-other-systems)
12. [The Consistency Migration — Rolling Out to a Live System](#12-the-consistency-migration--rolling-out-to-a-live-system)
13. [Operational Concerns](#13-operational-concerns)
14. [Interview Quick-Reference Cheat Sheet](#14-interview-quick-reference-cheat-sheet)

---

## 1. The Consistency Story: A Historical Timeline

Understanding S3's consistency model requires walking through its evolution. This is
one of the most frequently asked topics in Amazon system design interviews because
it illustrates a fundamental distributed systems trade-off: **performance vs correctness**.

### 1.1 The Timeline

```
2006 ──── S3 launches with eventual consistency
  │
  │       - New object PUTs: read-after-write consistent
  │         (a brand-new key never returns 404 after successful PUT)
  │
  │       - Overwrite PUTs: eventually consistent
  │         (GET might return the OLD version after overwrite)
  │
  │       - DELETEs: eventually consistent
  │         (GET might still return a deleted object briefly)
  │
  │       Why? Front-end metadata caching for performance.
  │       Caches could serve stale entries for overwrite/delete.
  │
  ▼
2020 ──── December: S3 announces strong read-after-write
  │       consistency for ALL operations
  │
  │       - Overwrite PUTs: immediately consistent
  │       - DELETEs: immediately consistent
  │       - LIST after PUT: object appears immediately
  │       - LIST after DELETE: object disappears immediately
  │
  │       No performance penalty. No additional cost.
  │       Enabled by the "witness" protocol.
  │
  ▼
2023+ ─── Conditional writes (If-None-Match) added
          Building on the strong consistency foundation.
```

### 1.2 What Changed for Developers

**Before December 2020:**

```
# This code was UNSAFE before 2020
s3.put_object(Bucket='my-bucket', Key='config.json', Body=new_config)
response = s3.get_object(Bucket='my-bucket', Key='config.json')
# response might return OLD config.json!

# Developers had to add:
#   - Sleep/retry loops
#   - Read-your-writes through sticky sessions
#   - Use DynamoDB as a consistency layer on top of S3
```

**After December 2020:**

```
# This code is now SAFE — guaranteed to return the new version
s3.put_object(Bucket='my-bucket', Key='config.json', Body=new_config)
response = s3.get_object(Bucket='my-bucket', Key='config.json')
# response ALWAYS returns new_config
```

### 1.3 The Significance

The 2020 announcement was remarkable because:

1. **No trade-offs exposed to the user** — no "consistent read" flag, no pricing tier
2. **No performance regression** — p50 and p99 latencies stayed the same
3. **Retroactive** — every existing bucket got strong consistency automatically
4. **Scale** — this was done on a system handling 100+ million requests per second

This is the kind of engineering achievement that interviewers love to discuss because
it violates the naive reading of the CAP theorem that says you must sacrifice something.

---

## 2. Why S3 Was Eventually Consistent Originally

### 2.1 The Architecture That Caused Eventual Consistency

S3's front-end layer had a fleet of hundreds (possibly thousands) of servers, each
handling client requests. To serve reads at low latency, each front-end server
maintained a **local metadata cache**.

```
┌─────────────────────────────────────────────────────────────────┐
│                        Client Fleet                             │
│    Client A          Client B          Client C                 │
└──────┬───────────────────┬───────────────────┬──────────────────┘
       │                   │                   │
       ▼                   ▼                   ▼
┌──────────────┐   ┌──────────────┐   ┌──────────────┐
│ Front-End 1  │   │ Front-End 2  │   │ Front-End 3  │
│              │   │              │   │              │
│ ┌──────────┐ │   │ ┌──────────┐ │   │ ┌──────────┐ │
│ │  Cache   │ │   │ │  Cache   │ │   │ │  Cache   │ │
│ │key→v1    │ │   │ │key→v1    │ │   │ │(empty)   │ │
│ └──────────┘ │   │ └──────────┘ │   │ └──────────┘ │
└──────┬───────┘   └──────┬───────┘   └──────┬───────┘
       │                   │                   │
       └───────────────────┼───────────────────┘
                           │
                    ┌──────▼──────┐
                    │  Metadata   │
                    │  Primary    │
                    │  (source    │
                    │   of truth) │
                    └─────────────┘
```

### 2.2 The Stale Read Scenario — Step by Step

This is the classic problem that existed from 2006 to 2020:

```
Timeline:
  T0: Object "config.json" exists with version V1
      All caches have: config.json → V1

  T1: Client A → Front-End 1: PUT config.json = V2
      FE1 → Metadata Primary: write V2 → success
      FE1 updates its OWN cache: config.json → V2
      FE1 → Client A: HTTP 200 OK

  T2: Client B → Front-End 2: GET config.json
      FE2 checks its cache: config.json → V1 (STALE!)
      FE2 → Client B: returns V1 (stale read!)
      ┌──────────────────────────────────────────┐
      │  BUG: Client B sees OLD data after a     │
      │  successful PUT by Client A!             │
      └──────────────────────────────────────────┘

  T3: Client C → Front-End 3: GET config.json
      FE3 has no cache entry (empty)
      FE3 → Metadata Primary: read config.json → V2
      FE3 → Client C: returns V2 (correct, but by luck)

  T4: (some time later) FE2's cache entry expires via TTL
      Next GET from FE2 reads from primary → gets V2
      System is "eventually" consistent
```

### 2.3 Why the Cache Was Necessary

You might ask: **why not just remove the cache?** The answer is scale.

```
Without cache:
  - 100,000,000+ reads/second → all go to metadata primary
  - Metadata primary would need ~100M IOPS
  - Cross-AZ round trips for every read: +2-5ms per request
  - Cost: enormous fleet of metadata servers

With cache:
  - Cache hit rate: >90% (most objects are read-heavy, rarely updated)
  - Only ~10M reads/sec reach metadata primary
  - Local cache lookup: ~0.1ms vs ~5ms for primary
  - Massive cost and latency savings
```

The fundamental tension:

```
  ┌────────────────────────────────────────────────────────┐
  │                                                        │
  │  Caching = FAST reads but STALE data (eventually       │
  │            consistent)                                 │
  │                                                        │
  │  No cache = SLOW reads but FRESH data (strongly        │
  │             consistent)                                │
  │                                                        │
  │  Witness  = FAST reads AND FRESH data (best of both)   │
  │             This is the breakthrough.                   │
  │                                                        │
  └────────────────────────────────────────────────────────┘
```

### 2.4 Why Simple Cache Invalidation Doesn't Work

The "obvious" solution: when a write happens, invalidate caches on all front-ends.

```
Problems with broadcast invalidation:

1. SCALE: Hundreds of front-end servers × millions of keys
   - Broadcasting every write to every server is O(writes × servers)
   - At S3 scale: millions of writes/sec × hundreds of servers
   - Network bandwidth and message processing become bottlenecks

2. ORDERING: Messages arrive out of order
   - Server A gets: invalidate(key, v3), invalidate(key, v2)
   - If it processes v3 first, then v2, it might re-cache stale v2
   - Requires version ordering, which adds complexity

3. RELIABILITY: What if an invalidation message is lost?
   - Server keeps stale cache forever (until TTL expires)
   - Making invalidation reliable requires acknowledgment protocol
   - Which adds more latency and complexity

4. PARTITIONS: Network partitions between front-ends
   - Some servers get the invalidation, others don't
   - Split-brain: different servers serve different versions
```

This is why Amazon chose a fundamentally different approach: the **witness protocol**.

---

## 3. The Witness Protocol — How S3 Achieved Strong Consistency

### 3.1 Core Idea

Instead of trying to keep caches in sync (push model), add a lightweight, strongly
consistent **witness** that tracks the latest version of every key. Reads **pull** the
current version from the witness and compare it against their cache.

```
  ┌───────────────────────────────────────────────────────────┐
  │  KEY INSIGHT:                                              │
  │                                                            │
  │  Don't invalidate caches. Instead, let each front-end     │
  │  CHECK whether its cache is still valid before using it.   │
  │                                                            │
  │  The witness is the "phone call to confirm" before         │
  │  trusting your local copy.                                 │
  └───────────────────────────────────────────────────────────┘
```

### 3.2 Write Path (PUT) — Detailed

```
Client                Front-End              Data Layer       Metadata       Witness
  │                      │                      │             Primary          │
  │  PUT bucket/key      │                      │               │              │
  │  Body: <data>        │                      │               │              │
  │─────────────────────>│                      │               │              │
  │                      │                      │               │              │
  │                      │  1. Store data       │               │              │
  │                      │  (erasure-coded      │               │              │
  │                      │   chunks across AZs) │               │              │
  │                      │─────────────────────>│               │              │
  │                      │                      │               │              │
  │                      │  2. Data stored OK   │               │              │
  │                      │<─────────────────────│               │              │
  │                      │                      │               │              │
  │                      │  3. Write metadata   │               │              │
  │                      │  (key, version_id,   │               │              │
  │                      │   size, etag, etc.)  │               │              │
  │                      │──────────────────────────────────>│  │              │
  │                      │                      │               │              │
  │                      │  4. Metadata written  │              │              │
  │                      │<──────────────────────────────────│  │              │
  │                      │                      │               │              │
  │                      │  5. Update witness    │              │              │
  │                      │  witness.put(         │              │              │
  │                      │    bucket/key,        │              │              │
  │                      │    new_version_id)    │              │              │
  │                      │─────────────────────────────────────────────────>│
  │                      │                      │               │              │
  │                      │  6. Witness confirmed │              │              │
  │                      │<─────────────────────────────────────────────────│
  │                      │                      │               │              │
  │                      │  7. Update local cache│              │              │
  │                      │  cache.put(bucket/key,│              │              │
  │                      │    new_version_id)    │              │              │
  │                      │                      │               │              │
  │  8. HTTP 200 OK      │                      │               │              │
  │<─────────────────────│                      │               │              │
```

**Critical ordering constraint:**

```
  Data stored  →  Metadata written  →  Witness updated  →  Client ACK

  ALL steps must succeed before returning 200 to the client.
  If any step fails, the PUT fails (or is retried internally).

  This ordering guarantees:
  - If the witness says "version V2 exists", then:
    - The metadata for V2 exists in the primary
    - The data for V2 exists in the data layer
  - A read that sees V2 in the witness can always fetch the actual data
```

### 3.3 Read Path (GET) — Detailed

```
Client                Front-End              Witness         Metadata       Data Layer
  │                      │                      │             Primary          │
  │  GET bucket/key      │                      │               │              │
  │─────────────────────>│                      │               │              │
  │                      │                      │               │              │
  │                      │  1. Check local cache │              │              │
  │                      │  cached = cache.get(  │              │              │
  │                      │    bucket/key)         │              │              │
  │                      │  → cached_version="v1"│              │              │
  │                      │                      │               │              │
  │                      │  2. Check witness     │               │              │
  │                      │  latest = witness.get(│               │              │
  │                      │    bucket/key)         │              │              │
  │                      │─────────────────────>│               │              │
  │                      │                      │               │              │
  │                      │  3. Witness responds  │               │              │
  │                      │  latest_version="v1" │               │              │
  │                      │<─────────────────────│               │              │
  │                      │                      │               │              │
  │                      │  4. Compare versions  │              │              │
  │                      │  "v1" == "v1" → MATCH │              │              │
  │                      │                      │               │              │
  │                      │  ═══════════════════════════════════════════════    │
  │                      │  ║ FAST PATH: cache is valid                  ║    │
  │                      │  ║ Use cached metadata to fetch data          ║    │
  │                      │  ═══════════════════════════════════════════════    │
  │                      │                      │               │              │
  │                      │  5. Fetch data using cached metadata │              │
  │                      │─────────────────────────────────────────────────>│
  │                      │                      │               │              │
  │                      │  6. Data returned     │              │              │
  │                      │<─────────────────────────────────────────────────│
  │                      │                      │               │              │
  │  7. HTTP 200 + body  │                      │               │              │
  │<─────────────────────│                      │               │              │
```

**Slow path (cache is stale):**

```
Client                Front-End              Witness         Metadata       Data Layer
  │                      │                      │             Primary          │
  │  GET bucket/key      │                      │               │              │
  │─────────────────────>│                      │               │              │
  │                      │                      │               │              │
  │                      │  1. Check local cache │              │              │
  │                      │  cached_version="v1" │               │              │
  │                      │                      │               │              │
  │                      │  2. Check witness     │               │              │
  │                      │─────────────────────>│               │              │
  │                      │                      │               │              │
  │                      │  3. Witness responds  │               │              │
  │                      │  latest_version="v2" │               │              │
  │                      │<─────────────────────│               │              │
  │                      │                      │               │              │
  │                      │  4. Compare versions  │              │              │
  │                      │  "v1" != "v2" → STALE│              │              │
  │                      │                      │               │              │
  │                      │  ═══════════════════════════════════════════════    │
  │                      │  ║ SLOW PATH: cache is stale                  ║    │
  │                      │  ║ Must read fresh metadata from primary       ║    │
  │                      │  ═══════════════════════════════════════════════    │
  │                      │                      │               │              │
  │                      │  5. Read fresh metadata from primary │              │
  │                      │──────────────────────────────────>│  │              │
  │                      │                      │               │              │
  │                      │  6. Metadata for V2   │              │              │
  │                      │<──────────────────────────────────│  │              │
  │                      │                      │               │              │
  │                      │  7. Update local cache│              │              │
  │                      │  cache.put(bucket/key,│              │              │
  │                      │    "v2", metadata_v2) │              │              │
  │                      │                      │               │              │
  │                      │  8. Fetch data using V2 metadata    │              │
  │                      │─────────────────────────────────────────────────>│
  │                      │                      │               │              │
  │                      │  9. Data returned     │              │              │
  │                      │<─────────────────────────────────────────────────│
  │                      │                      │               │              │
  │  10. HTTP 200 + body │                      │               │              │
  │<─────────────────────│                      │               │              │
```

### 3.4 Architecture Diagram — Full Picture

```
                           ┌──────────────────────────────────────┐
                           │          Load Balancer               │
                           └──────────────┬───────────────────────┘
                                          │
              ┌───────────────────────────┼───────────────────────────┐
              │                           │                           │
   ┌──────────▼──────────┐   ┌───────────▼─────────┐   ┌────────────▼─────────┐
   │    Front-End 1      │   │    Front-End 2      │   │    Front-End 3       │
   │  ┌───────────────┐  │   │  ┌───────────────┐  │   │  ┌───────────────┐   │
   │  │ Metadata      │  │   │  │ Metadata      │  │   │  │ Metadata      │   │
   │  │ Cache         │  │   │  │ Cache         │  │   │  │ Cache         │   │
   │  │               │  │   │  │               │  │   │  │               │   │
   │  │ key → ver     │  │   │  │ key → ver     │  │   │  │ key → ver     │   │
   │  │ + full meta   │  │   │  │ + full meta   │  │   │  │ + full meta   │   │
   │  └───────────────┘  │   │  └───────────────┘  │   │  └───────────────┘   │
   └──────────┬──────────┘   └───────────┬─────────┘   └────────────┬─────────┘
              │                          │                           │
              │          ┌───────────────┼───────────────┐          │
              │          │               │               │          │
              │   ┌──────▼──────┐ ┌──────▼──────┐ ┌─────▼───────┐ │
              │   │  Witness    │ │  Witness    │ │  Witness     │ │
              │   │  Node (AZ-a)│ │  Node (AZ-b)│ │  Node (AZ-c) │ │
              │   │             │ │             │ │              │ │
              │   │ key → ver   │ │ key → ver   │ │ key → ver    │ │
              │   │ (version    │ │ (version    │ │ (version     │ │
              │   │  only, no   │ │  only, no   │ │  only, no    │ │
              │   │  full meta) │ │  full meta) │ │  full meta)  │ │
              │   └─────────────┘ └─────────────┘ └──────────────┘ │
              │          ▲  Paxos / Raft consensus  ▲              │
              │          └──────────────────────────┘              │
              │                                                    │
              │          ┌──────────────────────────┐              │
              └──────────┤   Metadata Primary       ├──────────────┘
                         │   (full metadata store)  │
                         │                          │
                         │   key → {version_id,     │
                         │          size, etag,     │
                         │          content_type,   │
                         │          storage_class,  │
                         │          ACL, ...}       │
                         └─────────────┬────────────┘
                                       │
                         ┌─────────────▼────────────┐
                         │   Data Layer             │
                         │   (erasure-coded chunks  │
                         │    across AZs)           │
                         │                          │
                         │   chunk_id → bytes       │
                         └──────────────────────────┘

   LEGEND:
   ───── Write path: Data Layer → Metadata Primary → Witness → Client ACK
   ───── Read path:  Cache → Witness (check) → [Primary if stale] → Data Layer
```

### 3.5 Why This Guarantees Strong Consistency

The proof is straightforward:

```
Given:
  - Write W completes at time T_w (meaning witness has been updated)
  - Read R starts at time T_r where T_r > T_w

Then:
  1. R checks the witness, which returns version >= W's version
     (because witness was updated at T_w, and witness is linearizable)
  2. If R's cache has an older version, R goes to the primary
     (which has W's data, because primary was updated before witness)
  3. R returns W's data or newer

Therefore: every read that starts after a write completes
           sees that write (or a newer one).

This is the definition of strong read-after-write consistency.
```

---

## 4. Why the Witness Works Without Performance Penalty

### 4.1 The Key Insight — Witness Entries Are Tiny

The witness stores **only version IDs**, not full metadata.

```
Full metadata entry (Metadata Primary):
  {
    "bucket": "my-bucket",
    "key": "photos/vacation/IMG_2847.jpg",
    "version_id": "3HL4kqCxf3vjVBH40Nqjfkd",
    "size": 4582912,
    "etag": "\"d41d8cd98f00b204e9800998ecf8427e\"",
    "content_type": "image/jpeg",
    "content_encoding": null,
    "storage_class": "STANDARD",
    "last_modified": "2024-01-15T10:30:00Z",
    "acl": { ... },
    "user_metadata": { ... },
    "sse_algorithm": "AES256",
    "parts_info": { ... },
    "replication_status": "COMPLETED"
  }
  Size: ~500 bytes - 2 KB per entry

Witness entry:
  {
    "key_hash": "a1b2c3d4e5f6",    // hash of bucket+key
    "version_id": "3HL4kqCxf3vjVBH40Nqjfkd"
  }
  Size: ~50-100 bytes per entry
```

### 4.2 Performance Breakdown

```
┌─────────────────────────────────────────────────────────────┐
│                   FAST PATH (>95% of reads)                 │
│                                                             │
│  Step                           Latency      Notes          │
│  ─────────────────────────────  ──────────   ────────────── │
│  Local cache lookup             ~0.1 ms      In-memory      │
│  Witness check                  ~0.3 ms      Cross-AZ net   │
│  Data fetch (using cached meta) ~5-20 ms     Depends on     │
│                                               object size   │
│  ─────────────────────────────  ──────────                  │
│  Total overhead from witness:   ~0.3 ms      Negligible     │
│                                                             │
│  Old path (no witness):                                     │
│  Cache lookup + data fetch      ~5-20 ms                    │
│                                                             │
│  New path (with witness):                                   │
│  Cache lookup + witness + data  ~5.3-20.3 ms                │
│                                                             │
│  Overhead: <1 ms on a 5-20 ms operation → <5% increase      │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│                   SLOW PATH (<5% of reads)                  │
│                                                             │
│  Step                           Latency      Notes          │
│  ─────────────────────────────  ──────────   ────────────── │
│  Local cache lookup             ~0.1 ms      In-memory      │
│  Witness check                  ~0.3 ms      Cross-AZ       │
│  Metadata primary read          ~2-5 ms      Cross-AZ SSD   │
│  Cache update                   ~0.1 ms      In-memory      │
│  Data fetch (using fresh meta)  ~5-20 ms     Object size    │
│  ─────────────────────────────  ──────────                  │
│  Total:                         ~7-25 ms                    │
│                                                             │
│  Old path (cache miss):                                     │
│  Primary read + data fetch      ~7-25 ms     Same!          │
│                                                             │
│  No additional cost on slow path — you would have read      │
│  from primary anyway on a cache miss.                       │
└─────────────────────────────────────────────────────────────┘
```

### 4.3 Why the Fast Path Dominates

```
Object access patterns in S3:

  ┌────────────────────────────────────────────────────┐
  │  Category               % of reads    Cache valid? │
  │  ────────────────────   ──────────    ──────────── │
  │  Read-only objects      ~70%          Always yes   │
  │  (never updated)                                   │
  │                                                    │
  │  Rarely updated objects ~20%          Almost always │
  │  (updated < 1x/hour)                  yes          │
  │                                                    │
  │  Frequently updated     ~8%           Usually yes  │
  │  (updated < 1x/min)                               │
  │                                                    │
  │  Hot-updated objects    ~2%           Sometimes no │
  │  (updated > 1x/min)                               │
  └────────────────────────────────────────────────────┘

  Weighted cache hit rate: 70% × 1.0 + 20% × 0.99 + 8% × 0.95 + 2% × 0.7
                         = 70 + 19.8 + 7.6 + 1.4
                         = 98.8%

  So ~99% of reads hit the fast path. The witness check adds <0.3ms
  to those reads. The net impact on p50 latency is negligible.
```

### 4.4 Storage Requirements for the Witness

```
Back-of-the-envelope calculation:

  Total objects in S3: ~100 trillion (10^14) as of ~2023
  Witness entry size:  ~100 bytes

  Raw storage: 100 × 10^12 × 100 bytes = 10 PB

  But:
  - Witness is replicated 3x (Paxos): 30 PB total
  - Most entries are cold (objects rarely accessed)
  - Active working set (recently accessed objects) is much smaller
  - Witness can be partitioned and tiered

  Hot tier (in-memory):  objects accessed in last hour
  Warm tier (SSD):       objects accessed in last day
  Cold tier (disk):      everything else

  The hot tier is what matters for latency, and it fits in memory
  across the distributed witness fleet.
```

---

## 5. Witness Implementation Details

### 5.1 Requirements for the Witness System

The witness must satisfy all of these simultaneously:

```
┌────────────────────────────────────────────────────────────────┐
│  Property          │ Requirement       │ Why                    │
│  ─────────────────────────────────────────────────────────────  │
│  Consistency       │ Linearizable      │ Otherwise reads can    │
│                    │                   │ see stale versions     │
│                    │                   │                        │
│  Availability      │ 99.999%+          │ If witness is down,    │
│                    │                   │ all reads degrade      │
│                    │                   │                        │
│  Latency           │ Sub-millisecond   │ Added to every read    │
│                    │                   │                        │
│  Throughput        │ 100M+ ops/sec     │ Matches S3 read rate   │
│                    │                   │                        │
│  Partitioned       │ Must scale        │ Can't be single node   │
│                    │ horizontally      │ for 100T objects       │
└────────────────────────────────────────────────────────────────┘
```

### 5.2 Likely Architecture — Paxos-Based Partitioned Store

```
                    ┌──────────────────────────────────┐
                    │         Witness Router            │
                    │  (routes to correct partition     │
                    │   based on hash(bucket+key))      │
                    └──────────────┬───────────────────┘
                                   │
         ┌─────────────────────────┼─────────────────────────┐
         │                         │                         │
         ▼                         ▼                         ▼
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  Partition 0    │     │  Partition 1    │     │  Partition N    │
│  Range: [0,1M)  │     │  Range: [1M,2M) │     │  Range: [xM,∞) │
│                 │     │                 │     │                 │
│  ┌───┐┌───┐┌───┐│     │  ┌───┐┌───┐┌───┐│     │  ┌───┐┌───┐┌───┐│
│  │AZa││AZb││AZc││     │  │AZa││AZb││AZc││     │  │AZa││AZb││AZc││
│  │ W ││ W ││ W ││     │  │ W ││ W ││ W ││     │  │ W ││ W ││ W ││
│  └───┘└───┘└───┘│     │  └───┘└───┘└───┘│     │  └───┘└───┘└───┘│
│    Paxos group  │     │    Paxos group  │     │    Paxos group  │
└─────────────────┘     └─────────────────┘     └─────────────────┘

Each partition:
  - 3 replicas, one per AZ (Availability Zone)
  - Uses Paxos (or Raft) for consensus
  - Write: quorum write to 2 of 3 nodes
  - Read:  quorum read from 2 of 3 nodes → guaranteed to see latest write
```

### 5.3 Quorum Reads Guarantee Linearizability

```
Write quorum: W = 2 (out of 3)
Read quorum:  R = 2 (out of 3)

W + R = 2 + 2 = 4 > 3 (total replicas)

This means any read quorum overlaps with any write quorum
by at least one node. That node has the latest version.

Example:
  Write to nodes {A, B}     → quorum met (2/3)
  Read from nodes {B, C}    → quorum met (2/3)
  Overlap: {B}              → B has the latest version
  Read returns max version from {B, C} → sees the write

  ┌──────┐     ┌──────┐     ┌──────┐
  │Node A│     │Node B│     │Node C│
  │  v2  │     │  v2  │     │  v1  │
  └──────┘     └──────┘     └──────┘
    ▲  write     ▲  write      ▲
    │  quorum    │  quorum     │
    │            │             │
    │            ▼  read       ▼  read
    │          quorum        quorum
    │
    │  Read returns max(v2, v1) = v2 ✓
```

### 5.4 Partition Strategy

```
Partitioning by hash(bucket_name + "/" + key):

  hash("my-bucket/photos/img1.jpg") = 0x3A7F...
  partition_id = hash_value % num_partitions

  Number of partitions: likely thousands to tens of thousands
  - Each partition handles a manageable subset of the key space
  - Partitions can be split/merged as load changes
  - Consistent hashing or range-based partitioning

  Advantages of hash partitioning:
  - Even load distribution (hot buckets don't create hot partitions)
  - Simple routing

  Disadvantages:
  - Range scans across keys are not efficient
  - But witness only needs point lookups, not range scans
```

---

## 6. Handling Edge Cases in the Consistency Protocol

### 6.1 Case 1: Concurrent Writes to the Same Key

```
Client A: PUT key=X, value=V1 → starts at T1
Client B: PUT key=X, value=V2 → starts at T2 (T2 > T1)

Scenario: Both writes are in-flight simultaneously.

Step-by-step:

  T1: Client A's PUT arrives at Front-End 1
      FE1 → Data Layer: store V1 chunks → success
      FE1 → Metadata Primary: write (X → V1)

  T2: Client B's PUT arrives at Front-End 2
      FE2 → Data Layer: store V2 chunks → success
      FE2 → Metadata Primary: write (X → V2)

  Metadata Primary serializes the writes:
  ┌─────────────────────────────────────────────────────┐
  │  The primary processes writes sequentially.          │
  │  If A's write arrives first:                        │
  │    Slot 1: X → V1 (version_id = ver_1)             │
  │    Slot 2: X → V2 (version_id = ver_2)             │
  │  Final state: X → V2                               │
  │                                                     │
  │  Witness is updated AFTER each metadata write:      │
  │    After A's write: witness says X → ver_1          │
  │    After B's write: witness says X → ver_2          │
  │                                                     │
  │  S3 uses LAST-WRITER-WINS. No CAS by default.      │
  └─────────────────────────────────────────────────────┘

  Any GET after both PUTs complete will see V2.
  A GET between the two PUTs will see V1 (which is correct —
  V2 hasn't been committed yet).

  For conditional writes: use If-Match / If-None-Match headers
  (available since 2024) to implement compare-and-swap:

    s3.put_object(
        Bucket='my-bucket',
        Key='config.json',
        Body=new_data,
        IfMatch='"etag-of-version-i-expect"'
    )
    # Returns 412 Precondition Failed if etag doesn't match
```

### 6.2 Case 2: GET During In-Flight PUT

```
Timeline:

  T1: Client A starts PUT key=X, value=V2
      Step 1: Data stored ✓
      Step 2: Metadata written ✓
      Step 3: Witness update IN PROGRESS...

  T2: Client B does GET key=X (while witness update is in progress)

  ┌────────────────────────────────────────────────────────────┐
  │  Scenario A: Witness hasn't been updated yet               │
  │                                                            │
  │  Client B → Witness: what's the latest version of X?      │
  │  Witness: ver_1 (old version — update hasn't arrived)      │
  │  Client B's cache: ver_1                                   │
  │  Match → fast path → returns V1                            │
  │                                                            │
  │  This is CORRECT: Client A's PUT hasn't completed yet      │
  │  (step 3 hasn't finished, 200 OK hasn't been sent).        │
  │  Client B is not obligated to see an uncommitted write.    │
  └────────────────────────────────────────────────────────────┘

  ┌────────────────────────────────────────────────────────────┐
  │  Scenario B: Witness was just updated but data isn't       │
  │  stored yet?                                               │
  │                                                            │
  │  THIS CANNOT HAPPEN. The write order is:                   │
  │    data → metadata → witness                               │
  │                                                            │
  │  If the witness says ver_2, the data and metadata for      │
  │  ver_2 MUST already exist. The write ordering guarantees   │
  │  this invariant.                                           │
  └────────────────────────────────────────────────────────────┘
```

### 6.3 Case 3: Witness Temporarily Unavailable

```
What happens when the witness is unreachable?

  Client → Front-End: GET key=X
  Front-End → Witness: what's the latest version?
  Witness: ❌ TIMEOUT / UNREACHABLE

  Three possible strategies:

  ┌────────────────────────────────────────────────────────────┐
  │  Option A: Block until witness recovers                    │
  │                                                            │
  │  + Maintains strong consistency                            │
  │  - Sacrifices availability (reads fail during outage)      │
  │  - Violates S3's "eleven 9s availability" goal             │
  │                                                            │
  │  Verdict: Unlikely. S3 values availability highly.         │
  └────────────────────────────────────────────────────────────┘

  ┌────────────────────────────────────────────────────────────┐
  │  Option B: Return cached data with a warning               │
  │                                                            │
  │  + Maintains availability                                  │
  │  - Sacrifices consistency (might return stale data)        │
  │  - Breaks the "strong consistency" guarantee               │
  │                                                            │
  │  Verdict: Unlikely. Breaks the core promise.               │
  └────────────────────────────────────────────────────────────┘

  ┌────────────────────────────────────────────────────────────┐
  │  Option C: Fall back to reading from Metadata Primary      │
  │                                                            │
  │  + Maintains strong consistency (primary is always fresh)  │
  │  + Maintains availability (primary is replicated)          │
  │  - Higher latency (cross-AZ primary read vs local cache)  │
  │                                                            │
  │  Verdict: MOST LIKELY. Consistency + availability at the   │
  │  cost of temporary latency increase.                       │
  └────────────────────────────────────────────────────────────┘

  Fallback path during witness outage:

  Client → Front-End: GET key=X
  Front-End → Witness: UNREACHABLE
  Front-End → Metadata Primary: read key=X → ver_2 (always fresh)
  Front-End → Data Layer: fetch data for ver_2
  Front-End → Client: return data (correct, just slower)

  Front-End → Update local cache: X → ver_2
```

### 6.4 Case 4: DELETE Consistency

```
Without versioning:

  T1: Client A: DELETE key=X
      → Metadata Primary: mark X as deleted
      → Witness: update X → "DELETED" (or tombstone version)
      → Client A: 204 No Content

  T2: Client B: GET key=X
      → Cache: X → ver_1
      → Witness: X → "DELETED" (or tombstone ver_2)
      → Mismatch → Slow path → Read primary → X is deleted
      → Client B: 404 Not Found ✓

With versioning:

  T1: Client A: DELETE key=X
      → Metadata Primary: insert delete marker (version=ver_dm)
      → Witness: update X → ver_dm
      → Client A: 204 No Content (+ x-amz-delete-marker: true)

  T2: Client B: GET key=X (no version specified)
      → Cache: X → ver_1
      → Witness: X → ver_dm
      → Mismatch → Slow path → Read primary → latest is delete marker
      → Client B: 404 Not Found ✓

  T3: Client C: GET key=X?versionId=ver_1
      → Specific version request bypasses latest-version logic
      → Reads metadata for ver_1 directly → still exists
      → Client C: 200 OK with original data ✓
```

### 6.5 Case 5: LIST Consistency After PUT

```
Before the witness protocol, LIST had a separate consistency issue:

  T1: Client A: PUT my-bucket/new-file.txt
  T2: Client B: LIST my-bucket/

  The LIST might not include new-file.txt because the LIST index
  hadn't been updated yet.

After the witness protocol:

  The witness (or a similar mechanism) ensures that LIST operations
  are also strongly consistent. The LIST index is updated as part of
  the write path, and reads against the LIST index also check freshness.

  T1: Client A: PUT my-bucket/new-file.txt → success
  T2: Client B: LIST my-bucket/ → includes new-file.txt ✓

  T3: Client A: DELETE my-bucket/new-file.txt → success
  T4: Client B: LIST my-bucket/ → does NOT include new-file.txt ✓
```

---

## 7. Cross-Region Replication (CRR)

### 7.1 Overview

Cross-Region Replication asynchronously copies objects from a source bucket in one
AWS region to a destination bucket in a different AWS region.

```
┌─────────────────────────────────────────────────────────────────────┐
│                      CROSS-REGION REPLICATION                       │
│                                                                     │
│   Source Region                        Destination Region           │
│   (us-east-1)                          (eu-west-1)                  │
│                                                                     │
│   ┌─────────────┐    Async copy       ┌─────────────┐              │
│   │   Source     │ ──────────────────> │ Destination  │              │
│   │   Bucket     │    (minutes,        │ Bucket       │              │
│   │             │     best-effort)    │              │              │
│   └─────────────┘                     └─────────────┘              │
│                                                                     │
│   Strong consistency                   Strong consistency           │
│   WITHIN this region                   WITHIN this region           │
│                                                                     │
│   But: cross-region replication is EVENTUALLY consistent            │
│   (destination may lag behind source by seconds to minutes)         │
└─────────────────────────────────────────────────────────────────────┘
```

### 7.2 CRR Architecture — Detailed

```
Source Region (us-east-1)
┌────────────────────────────────────────────────────────────┐
│                                                            │
│  Client                                                    │
│    │                                                       │
│    │ PUT my-bucket/report.pdf                              │
│    ▼                                                       │
│  ┌──────────────┐                                          │
│  │ S3 Front-End │                                          │
│  └──────┬───────┘                                          │
│         │                                                  │
│    ┌────▼────────────┐                                     │
│    │ Write succeeds  │                                     │
│    │ (data + meta +  │                                     │
│    │  witness)       │                                     │
│    └────┬────────────┘                                     │
│         │                                                  │
│    ┌────▼────────────────────────────────────┐             │
│    │ S3 Replication Event Stream              │             │
│    │ (internal — not the user-facing          │             │
│    │  S3 Event Notifications)                 │             │
│    │                                          │             │
│    │ Event: {                                 │             │
│    │   type: "ObjectCreated:Put",             │             │
│    │   bucket: "my-bucket",                   │             │
│    │   key: "report.pdf",                     │             │
│    │   version_id: "ver_abc123",              │             │
│    │   size: 1048576                          │             │
│    │ }                                        │             │
│    └────────────────┬────────────────────────┘             │
│                     │                                      │
│    ┌────────────────▼────────────────────────┐             │
│    │ Replication Controller                   │             │
│    │                                          │             │
│    │ 1. Check replication rules:              │             │
│    │    - Prefix filter matches?              │             │
│    │    - Tag filter matches?                 │             │
│    │    - Destination bucket configured?      │             │
│    │                                          │             │
│    │ 2. Read object from source bucket        │             │
│    │    (internal read, uses source storage)   │             │
│    │                                          │             │
│    │ 3. PUT object to destination bucket      │────────────────┐
│    │    via cross-region transfer              │             │  │
│    │                                          │             │  │
│    │ 4. Update replication status:            │             │  │
│    │    source: COMPLETED                     │             │  │
│    │    dest:   REPLICA                       │             │  │
│    └─────────────────────────────────────────┘             │  │
│                                                            │  │
└────────────────────────────────────────────────────────────┘  │
                                                                │
                    Cross-region transfer                        │
                    (AWS backbone network)                       │
                                                                │
Destination Region (eu-west-1)                                  │
┌────────────────────────────────────────────────────────────┐  │
│                                                            │  │
│    ┌─────────────────────────────────────────┐             │  │
│    │ Replication Worker                       │<────────────┘
│    │                                          │             │
│    │ 1. Receive object data + metadata        │             │
│    │                                          │             │
│    │ 2. PUT to destination bucket:            │             │
│    │    Key: report.pdf                       │             │
│    │    Metadata: (copied from source)        │             │
│    │    x-amz-replication-status: REPLICA     │             │
│    │    Storage class: (same or overridden)   │             │
│    │                                          │             │
│    │ 3. Object is now available in eu-west-1  │             │
│    └─────────────────────────────────────────┘             │
│                                                            │
│  ┌──────────────┐                                          │
│  │ Destination  │  Clients in EU can now read              │
│  │ Bucket       │  report.pdf with low latency             │
│  └──────────────┘                                          │
│                                                            │
└────────────────────────────────────────────────────────────┘
```

### 7.3 CRR Configuration

```
Replication Rule Configuration:

{
  "Rules": [
    {
      "ID": "replicate-all-to-eu",
      "Status": "Enabled",
      "Priority": 1,
      "Filter": {
        "Prefix": ""           // All objects (empty prefix = everything)
      },
      "Destination": {
        "Bucket": "arn:aws:s3:::my-backup-eu-west-1",
        "StorageClass": "STANDARD_IA",     // Override storage class
        "Account": "111122223333",          // Cross-account
        "AccessControlTranslation": {
          "Owner": "Destination"            // Change ownership
        },
        "Metrics": {
          "Status": "Enabled",
          "EventThreshold": {
            "Minutes": 15
          }
        },
        "ReplicationTime": {
          "Status": "Enabled",              // Enable RTC
          "Time": {
            "Minutes": 15
          }
        }
      },
      "DeleteMarkerReplication": {
        "Status": "Enabled"                 // Replicate deletes too
      }
    },
    {
      "ID": "replicate-logs-to-archive",
      "Status": "Enabled",
      "Priority": 2,
      "Filter": {
        "And": {
          "Prefix": "logs/",
          "Tags": [
            { "Key": "archive", "Value": "true" }
          ]
        }
      },
      "Destination": {
        "Bucket": "arn:aws:s3:::log-archive-ap-southeast-1",
        "StorageClass": "GLACIER"
      }
    }
  ]
}

Prerequisites:
  - Versioning MUST be enabled on both source and destination buckets
  - IAM role with s3:ReplicateObject, s3:GetReplicationConfiguration
  - If cross-account: destination bucket policy must allow the role
```

### 7.4 What Gets Replicated (and What Doesn't)

```
┌────────────────────────────────────────────────────────────┐
│                     REPLICATED                              │
│                                                            │
│  ✓ Object data (the bytes)                                 │
│  ✓ Object metadata (user metadata + system metadata)       │
│  ✓ Object ACL (access control list)                        │
│  ✓ Object tags                                             │
│  ✓ Object lock retention settings                          │
│  ✓ Delete markers (if configured)                          │
│  ✓ New objects created after replication is enabled         │
└────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────┐
│                   NOT REPLICATED                            │
│                                                            │
│  ✗ Objects that existed BEFORE replication was enabled      │
│    (use S3 Batch Replication for existing objects)          │
│  ✗ Objects in GLACIER or DEEP_ARCHIVE storage class        │
│    (must restore first)                                    │
│  ✗ Objects encrypted with customer-managed SSE-C keys      │
│    (unless destination has the same key)                   │
│  ✗ Replicas of replicas (no chaining by default)           │
│    Client→A→B works, but B→C does NOT auto-replicate       │
│    (unless B has its own replication rule)                  │
│  ✗ Lifecycle transition actions                            │
│    (lifecycle policies are independent per bucket)         │
│  ✗ Bucket-level configurations (policies, CORS, etc.)     │
└────────────────────────────────────────────────────────────┘
```

### 7.5 Replication Time Control (RTC)

```
Without RTC (default):
  - Best-effort replication
  - Most objects replicate in seconds to minutes
  - No SLA on replication time
  - Some objects may take hours during heavy load

With RTC enabled:
  - SLA: 99.99% of objects replicated within 15 minutes
  - S3 Replication Metrics available:
    - Operations pending replication (count)
    - Bytes pending replication (size)
    - Replication latency (time since source PUT)
  - Amazon CloudWatch alarms can trigger on these metrics
  - Additional cost for RTC

Monitoring dashboard example:

  Replication Lag (minutes)
  25 │
     │ *
  20 │  *
     │   *
  15 │────────────────── SLA threshold (15 min) ──────────
     │      *
  10 │       * *
     │          *
   5 │           *  *  *
     │                    * *  *  *  *  *
   0 │──────────────────────────────────────────────────
     T0   T1   T2   T3   T4   T5   T6   T7   T8   T9

  Most objects replicate well under the 15-minute SLA.
  Spikes can occur during large batch uploads.
```

---

## 8. Same-Region Replication (SRR)

### 8.1 Overview

Same-Region Replication copies objects between buckets in the **same** AWS region.

```
Same Region (us-east-1)
┌────────────────────────────────────────────────────────────┐
│                                                            │
│  ┌─────────────┐   SRR (async)    ┌─────────────┐        │
│  │ Source       │ ───────────────> │ Destination  │        │
│  │ Bucket       │                  │ Bucket       │        │
│  │ (Account A)  │                  │ (Account B)  │        │
│  └─────────────┘                  └─────────────┘        │
│                                                            │
│  Same AZs, same region — lower latency than CRR           │
│  Still asynchronous (not synchronous replication)          │
└────────────────────────────────────────────────────────────┘
```

### 8.2 SRR Use Cases

```
1. LOG AGGREGATION
   ┌───────────┐
   │ App Bucket│──┐
   │ (logs/)   │  │
   └───────────┘  │     ┌──────────────────┐
                  ├────>│ Central Log       │
   ┌───────────┐  │     │ Bucket           │
   │ API Bucket│──┤     │ (all logs in one  │
   │ (logs/)   │  │     │  place for        │
   └───────────┘  │     │  analytics)       │
                  │     └──────────────────┘
   ┌───────────┐  │
   │ Web Bucket│──┘
   │ (logs/)   │
   └───────────┘

2. COMPLIANCE COPY
   ┌───────────┐  SRR   ┌──────────────────┐
   │ Production│──────> │ Compliance       │
   │ Bucket    │         │ Bucket           │
   │           │         │ (Object Lock,    │
   └───────────┘         │  different       │
                         │  account,        │
                         │  WORM)           │
                         └──────────────────┘

3. TEST DATA REFRESH
   ┌───────────┐  SRR   ┌──────────────────┐
   │ Production│──────> │ Staging/Test     │
   │ Data      │         │ Bucket           │
   │ Bucket    │         │ (latest prod     │
   └───────────┘         │  data for        │
                         │  testing)        │
                         └──────────────────┘
```

### 8.3 SRR vs CRR Comparison

```
┌─────────────────────┬─────────────────────┬─────────────────────┐
│  Aspect             │  SRR                │  CRR                │
├─────────────────────┼─────────────────────┼─────────────────────┤
│  Source & Dest      │  Same region        │  Different regions  │
│  Network latency    │  Low (same region)  │  Higher (cross-reg) │
│  Replication speed  │  Faster typically   │  Slower (distance)  │
│  Data transfer cost │  No cross-region    │  Cross-region       │
│                     │  charges            │  transfer fees      │
│  Use case           │  Compliance, logs,  │  DR, latency,       │
│                     │  test data          │  compliance         │
│  RTC available?     │  Yes                │  Yes                │
│  Cross-account?     │  Yes                │  Yes                │
│  Versioning req'd?  │  Yes                │  Yes                │
└─────────────────────┴─────────────────────┴─────────────────────┘
```

---

## 9. Replication Conflict Resolution

### 9.1 One-Directional Replication (Default)

```
Source (us-east-1)                   Destination (eu-west-1)
┌─────────────────────┐             ┌─────────────────────┐
│                     │    CRR      │                     │
│  config.json = V1   │ ─────────> │  config.json = V1   │
│                     │             │                     │
│  T1: PUT V2         │             │                     │
│  config.json = V2   │ ─────────> │  config.json = V2   │
│                     │             │                     │
│                     │             │  T2: PUT V3         │
│                     │    ╳       │  config.json = V3   │
│                     │  (no       │  (only in eu-west-1) │
│                     │   reverse) │                     │
│                     │             │                     │
│  T3: PUT V4         │             │                     │
│  config.json = V4   │ ─────────> │  config.json = V4   │
│                     │             │  (V3 overwritten!)  │
└─────────────────────┘             └─────────────────────┘

PROBLEM: V3 written directly to destination is lost when V4 replicates.
In one-directional replication, the destination is a REPLICA.
Direct writes to the destination will be overwritten.
```

### 9.2 Bi-Directional Replication

```
Starting in 2021, S3 added "replica modification sync" to enable
bi-directional replication without infinite loops.

Region A (us-east-1)                Region B (eu-west-1)
┌─────────────────────┐             ┌─────────────────────┐
│                     │   Rule 1    │                     │
│  Source Bucket A    │ ─────────> │  Source Bucket B    │
│                     │             │                     │
│                     │ <───────── │                     │
│                     │   Rule 2    │                     │
└─────────────────────┘             └─────────────────────┘

How infinite loops are prevented:
  - S3 marks replicated objects with x-amz-replication-status: REPLICA
  - By default, replication rules do NOT replicate REPLICAs
  - "Replica modification sync" DOES replicate changes to replicas
    but only metadata changes (tags, ACLs), not the object data
  - This prevents: A→B→A→B→... infinite loop

For data changes:
  - Original writes at A replicate to B
  - Original writes at B replicate to A
  - Replicated copies at B do NOT re-replicate back to A
  - Replicated copies at A do NOT re-replicate back to B

Conflict scenario with bi-directional:

  T1: Client in us-east-1 writes config.json = V_A
  T2: Client in eu-west-1 writes config.json = V_B (simultaneously)

  Result:
  - us-east-1 has V_A, then receives V_B from replication
  - eu-west-1 has V_B, then receives V_A from replication
  - Final state depends on arrival order: LAST-WRITER-WINS
  - Both regions may temporarily have different versions
  - Eventually they converge, but the "winning" version depends
    on the order replication completes

  ┌────────────────────────────────────────────────────────────┐
  │  WARNING: Bi-directional replication does NOT provide      │
  │  conflict resolution. If both sides write simultaneously,  │
  │  you get last-writer-wins with no guarantees about which   │
  │  writer "wins" in each region.                             │
  │                                                            │
  │  For true conflict resolution, use application-level       │
  │  strategies (e.g., write to one region, read from both).   │
  └────────────────────────────────────────────────────────────┘
```

### 9.3 Replication with Versioning

```
With versioning enabled (required for replication):

Source (us-east-1)                   Destination (eu-west-1)
┌─────────────────────┐             ┌─────────────────────┐
│  config.json        │             │  config.json        │
│  ┌───────────────┐  │             │  ┌───────────────┐  │
│  │ ver_3 (latest)│  │    CRR     │  │ ver_3 (latest)│  │
│  │ ver_2         │  │ ─────────> │  │ ver_2         │  │
│  │ ver_1         │  │             │  │ ver_1         │  │
│  └───────────────┘  │             │  └───────────────┘  │
└─────────────────────┘             └─────────────────────┘

  Version IDs are preserved during replication.
  The same version ID (ver_1, ver_2, ver_3) exists in both buckets.
  This enables consistent cross-region reads for specific versions.
```

---

## 10. Consistency of Replicated Data

### 10.1 Within-Region vs Cross-Region Consistency

This is a critical distinction for interviews:

```
┌──────────────────────────────────────────────────────────────────┐
│                                                                  │
│  WITHIN A SINGLE REGION:                                         │
│  ═══════════════════════                                         │
│  Consistency model: STRONG read-after-write                      │
│  Mechanism: Witness protocol (synchronous version check)         │
│  Latency overhead: Sub-millisecond                               │
│  Guarantee: After PUT returns 200, all subsequent reads           │
│             return the new version                               │
│                                                                  │
│  ACROSS REGIONS (CRR):                                           │
│  ════════════════════                                            │
│  Consistency model: EVENTUAL                                     │
│  Mechanism: Asynchronous copy via replication workers            │
│  Latency: Seconds to minutes (15 min SLA with RTC)              │
│  Guarantee: Destination WILL eventually have the object,         │
│             but reads in destination may see stale data           │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

### 10.2 Why CRR Can't Be Strongly Consistent

```
Physics constraint:

  us-east-1 (Virginia) ←→ eu-west-1 (Ireland)
  Distance: ~5,500 km
  Speed of light in fiber: ~200,000 km/s
  One-way latency (physics minimum): ~27 ms
  Actual network latency: ~70-100 ms round trip

  To make CRR strongly consistent, every PUT would need to:
  1. Write data in source region          ~10 ms
  2. Transfer data across Atlantic        ~50 ms
  3. Write data in destination region     ~10 ms
  4. Confirm back to source               ~50 ms
  5. Source returns 200 to client         ───────
                                    Total: ~120 ms minimum

  vs. current (async):
  1. Write data in source region          ~10 ms
  2. Return 200 to client                ───────
                                    Total: ~10 ms

  12x latency increase is unacceptable for most workloads.
  That's why CRR is async: availability and latency > cross-region consistency.
```

### 10.3 Application Patterns for Cross-Region Reads

```
Pattern 1: PRIMARY REGION reads, SECONDARY for DR only
  ┌─────────────────────────────────────────────────┐
  │  Normal operation:                               │
  │    All reads/writes → us-east-1 (primary)       │
  │    CRR → eu-west-1 (standby)                    │
  │                                                  │
  │  During failover:                                │
  │    Switch reads/writes → eu-west-1              │
  │    Accept that some recent writes may be lost    │
  │    (RPO = replication lag)                       │
  └─────────────────────────────────────────────────┘

Pattern 2: READ-LOCAL, WRITE-PRIMARY
  ┌─────────────────────────────────────────────────┐
  │  Writes: always to us-east-1                    │
  │  Reads:  from nearest region                    │
  │                                                  │
  │  Acceptable for:                                │
  │    - Static assets (images, videos)             │
  │    - Data that changes infrequently             │
  │    - Applications tolerant of stale reads       │
  │                                                  │
  │  NOT acceptable for:                            │
  │    - Financial data                             │
  │    - Inventory counts                           │
  │    - Anything requiring read-your-writes        │
  └─────────────────────────────────────────────────┘

Pattern 3: MULTI-REGION ACTIVE with external coordination
  ┌─────────────────────────────────────────────────┐
  │  Use DynamoDB Global Tables or similar for      │
  │  strongly consistent metadata coordination      │
  │  across regions. S3 for bulk data only.         │
  │                                                  │
  │  DynamoDB GT: "object X version V3 is canonical"│
  │  S3 CRR: carries the actual bytes               │
  │                                                  │
  │  Read: check DynamoDB for latest version →      │
  │        read that version from local S3           │
  │        (retry from other region if not yet       │
  │         replicated)                              │
  └─────────────────────────────────────────────────┘
```

### 10.4 Detailed Comparison Table

| Aspect | Witness (within-region) | CRR (cross-region) |
|---|---|---|
| **Consistency** | Strong (read-after-write) | Eventual |
| **Mechanism** | Synchronous version check | Asynchronous object copy |
| **Latency overhead** | Sub-millisecond | Seconds to minutes |
| **Purpose** | Correctness for all reads | DR, compliance, latency |
| **Data transferred** | Version ID only (~100 bytes) | Full object (KBs to GBs) |
| **Failure mode** | Fall back to primary read | Replication queue backs up |
| **SLA** | Same as S3 read SLA | 15 min with RTC |
| **Cost** | Included (no extra charge) | Data transfer + request fees |
| **Scope** | All objects automatically | Per replication rule |

---

## 11. Comparison: S3 Consistency vs Other Systems

### 11.1 Storage System Consistency Models

| System | Consistency Model | Mechanism | Trade-offs |
|---|---|---|---|
| **S3 (post-2020)** | Strong read-after-write | Witness protocol (sync version check) | Sub-ms overhead on reads |
| **S3 (pre-2020)** | Eventual for overwrites/deletes | Metadata caching without validation | Stale reads possible |
| **DynamoDB** | Strong or eventual (per-read) | Leader-based replication with quorum | Strong reads cost 2x RCU |
| **Cassandra** | Tunable (QUORUM, ONE, ALL) | Leaderless with configurable quorum | Flexible but complex |
| **GCS (Google)** | Strong | Spanner-based metadata with TrueTime | Atomic clocks, higher cost |
| **Azure Blob** | Strong | Paxos-based metadata replication | Similar to S3 approach |
| **HDFS** | Strong | Single NameNode (metadata) | Single point of failure |
| **CockroachDB** | Serializable | Raft + hybrid logical clocks | Higher write latency |

### 11.2 Deep Comparison: S3 vs GCS Consistency

```
Amazon S3 approach:
  - Cache + Witness (version register)
  - Witness stores version IDs only
  - Reads: cache → witness check → primary (if stale)
  - No special hardware required
  - Retrofit onto existing architecture

Google Cloud Storage approach:
  - Built on Spanner (globally distributed SQL database)
  - Spanner uses TrueTime (atomic clocks + GPS in every datacenter)
  - Metadata reads go through Spanner → always fresh
  - No separate "witness" — Spanner IS the consistent store
  - Designed for consistency from the start

  ┌────────────────────────────────────────────────────────────┐
  │  S3's approach is more pragmatic: retrofit strong          │
  │  consistency onto an eventually consistent system          │
  │  without changing the fundamental architecture.            │
  │                                                            │
  │  GCS's approach is more elegant: build on a globally       │
  │  consistent foundation (Spanner) from the beginning.       │
  │                                                            │
  │  Both achieve the same result for the user: strong         │
  │  read-after-write consistency.                             │
  └────────────────────────────────────────────────────────────┘
```

### 11.3 Deep Comparison: S3 vs DynamoDB Consistency

```
DynamoDB:
  - Each partition has a leader replica and two follower replicas
  - Strongly consistent reads go to the leader (guaranteed latest)
  - Eventually consistent reads go to any replica (might be stale)
  - User chooses per-read: ConsistentRead=true/false

S3 (post-2020):
  - No user choice needed — ALL reads are strongly consistent
  - The witness protocol makes this transparent
  - No "consistent read" flag or pricing premium

Why DynamoDB needs a flag but S3 doesn't:
  - DynamoDB's strong reads go to the leader → higher latency + cost
  - S3's witness check is so cheap (~0.3ms) that it's always-on
  - The witness stores only version IDs, not full data
  - DynamoDB's leader must serve the full item, which is more expensive
```

---

## 12. The Consistency Migration — Rolling Out to a Live System

### 12.1 The Challenge

```
┌──────────────────────────────────────────────────────────────┐
│  Deploying strong consistency to S3 is like replacing the    │
│  engine of a Boeing 747 while it's flying at 40,000 feet    │
│  carrying 3 billion passengers.                              │
│                                                              │
│  - S3 serves 100+ million requests per second                │
│  - Trillions of objects across millions of buckets           │
│  - Zero downtime tolerance (S3 is foundational AWS infra)    │
│  - Any bug could cause data loss or corruption               │
│  - Must be backwards compatible (no API changes)             │
└──────────────────────────────────────────────────────────────┘
```

### 12.2 Likely Rollout Strategy

```
Phase 1: SHADOW MODE (months)
┌────────────────────────────────────────────────────────────┐
│  - Deploy witness infrastructure alongside existing system │
│  - Write to witness on every PUT/DELETE (dual-write)       │
│  - But do NOT check witness on reads                       │
│  - Monitor witness health, latency, accuracy               │
│                                                            │
│  Reads still use old path (cache only, eventual).          │
│  Witness is "warming up" and being validated.              │
│                                                            │
│  Write path:                                               │
│    data → metadata → witness (NEW) → client ACK           │
│                                                            │
│  Read path (unchanged):                                    │
│    cache → [primary on miss] → data → client               │
└────────────────────────────────────────────────────────────┘

Phase 2: VALIDATION (weeks)
┌────────────────────────────────────────────────────────────┐
│  - On each read, check witness IN ADDITION to normal path  │
│  - Compare: does the witness agree with the cache?         │
│  - Log mismatches but DON'T change read behavior           │
│  - Track metrics:                                          │
│    - What % of reads have stale cache (witness disagrees)? │
│    - Does witness always agree with metadata primary?      │
│    - Are there any cases where witness is BEHIND primary?  │
│                                                            │
│  This validates correctness without affecting users.       │
└────────────────────────────────────────────────────────────┘

Phase 3: GRADUAL ENABLEMENT (weeks)
┌────────────────────────────────────────────────────────────┐
│  - Enable witness-checked reads for a small % of traffic   │
│    (e.g., 0.1% → 1% → 5% → 25% → 50% → 100%)            │
│  - Or enable per-region, per-bucket-prefix                 │
│  - Monitor:                                                │
│    - p50 / p99 latency changes                             │
│    - Error rates                                           │
│    - Witness availability                                  │
│    - Witness latency percentiles                           │
│  - Feature flags to instantly disable if issues arise      │
│                                                            │
│  Traffic ramp:                                             │
│    Day 1:  0.1% → monitor 24 hours                        │
│    Day 2:  1%   → monitor 24 hours                        │
│    Day 4:  5%   → monitor 48 hours                        │
│    Day 7:  25%  → monitor 48 hours                        │
│    Day 10: 50%  → monitor 72 hours                        │
│    Day 14: 100% → announce to customers                   │
└────────────────────────────────────────────────────────────┘

Phase 4: FULL ROLLOUT
┌────────────────────────────────────────────────────────────┐
│  - All reads check witness                                 │
│  - Remove the old eventually consistent code path          │
│  - December 2020: Public announcement                      │
│  - Maintain kill switch for emergencies                    │
└────────────────────────────────────────────────────────────┘
```

### 12.3 Testing Strategy

```
Correctness Tests:
  - Linearizability checker (Jepsen-style):
    Multiple clients do concurrent reads/writes
    Record all operations with timestamps
    Verify the history is linearizable

  - Specific scenario tests:
    PUT → immediate GET returns new value
    PUT → PUT → GET returns second value
    DELETE → GET returns 404
    PUT → LIST includes new key
    DELETE → LIST excludes deleted key

Chaos Engineering:
  - Kill witness nodes → verify fallback to primary works
  - Network partition between witness and front-end → verify reads succeed
  - Slow witness responses → verify timeout and fallback
  - Data corruption in witness → verify detection and recovery
  - Simultaneously fail witness + primary → verify graceful degradation

Load Tests:
  - Replay production traffic against the new system
  - Synthetic load at 2x-5x peak traffic
  - Measure latency impact at various witness hit/miss ratios
  - Measure witness cluster behavior under extreme write rates

Canary Deployments:
  - Deploy to a single AZ first
  - Then a single region
  - Then gradually expand
  - Compare error rates and latency against non-canary regions
```

---

## 13. Operational Concerns

### 13.1 Witness Monitoring

```
┌──────────────────────────────────────────────────────────────┐
│                   WITNESS HEALTH DASHBOARD                    │
├──────────────────────────────────────────────────────────────┤
│                                                              │
│  Availability:                                               │
│  ┌──────────────────────────────────────┐                    │
│  │ AZ-a: ████████████████████████ 99.99%│                    │
│  │ AZ-b: ████████████████████████ 99.99%│                    │
│  │ AZ-c: ████████████████████████ 99.98%│                    │
│  │ Quorum: ██████████████████████ 100%   │ (any 2 of 3 up)  │
│  └──────────────────────────────────────┘                    │
│                                                              │
│  Latency (witness read, p50/p99):                            │
│  ┌──────────────────────────────────────┐                    │
│  │ p50:  0.2 ms  ██                     │                    │
│  │ p90:  0.4 ms  ████                   │                    │
│  │ p99:  1.1 ms  ███████████            │                    │
│  │ p999: 3.2 ms  ████████████████████████│                   │
│  └──────────────────────────────────────┘                    │
│                                                              │
│  Witness vs Primary Agreement:                               │
│  ┌──────────────────────────────────────┐                    │
│  │ Matching versions:    99.9999%       │                    │
│  │ Witness behind primary: 0.0001%      │ ← expected during │
│  │ Witness ahead of primary: 0.0000%    │    write race      │
│  └──────────────────────────────────────┘                    │
│                                                              │
│  Cache Efficiency (with witness):                            │
│  ┌──────────────────────────────────────┐                    │
│  │ Fast path (cache valid):     95.3%   │                    │
│  │ Slow path (cache stale):      4.2%   │                    │
│  │ Witness fallback (to primary): 0.5%  │                    │
│  └──────────────────────────────────────┘                    │
│                                                              │
└──────────────────────────────────────────────────────────────┘

Key alerts:
  - Witness availability < 99.9% → page on-call
  - Witness p99 latency > 5ms → investigate
  - Cache stale rate > 10% → unusual write pattern, investigate
  - Witness-primary disagreement > 0.01% → potential bug, escalate
```

### 13.2 Witness Storage Growth and Compaction

```
As objects are created and deleted, the witness store grows:

  New object created: witness entry added
  Object deleted: witness entry updated to "deleted" / tombstone
  Object overwritten: witness entry updated to new version

  Over time, deleted objects accumulate tombstone entries.
  Compaction removes tombstones for objects that no longer exist
  in the metadata primary.

  Compaction process:
  ┌────────────────────────────────────────────────────────────┐
  │  1. Background scanner iterates through witness entries     │
  │  2. For each tombstone entry, check metadata primary:       │
  │     - If primary confirms deletion → remove from witness    │
  │     - If primary still has the object → keep in witness     │
  │  3. Rate-limited to avoid impacting read/write performance  │
  │  4. Runs continuously, prioritizing oldest tombstones       │
  └────────────────────────────────────────────────────────────┘

  Storage growth estimate:
  - New objects/day: ~1 billion (estimate)
  - Deletions/day: ~500 million (estimate)
  - Net growth: ~500M × 100 bytes = 50 GB/day
  - With compaction keeping tombstones < 7 days:
    Total witness storage is bounded and manageable
```

### 13.3 CRR Operational Monitoring

```
┌──────────────────────────────────────────────────────────────┐
│              CRR OPERATIONAL DASHBOARD                        │
├──────────────────────────────────────────────────────────────┤
│                                                              │
│  Replication Status (us-east-1 → eu-west-1):                │
│  ┌──────────────────────────────────────┐                    │
│  │ Objects pending: 12,847              │                    │
│  │ Bytes pending:   4.2 GB              │                    │
│  │ Avg latency:     42 seconds          │                    │
│  │ p99 latency:     8.3 minutes         │                    │
│  │ Failed (last hr): 3                  │                    │
│  └──────────────────────────────────────┘                    │
│                                                              │
│  Replication Lag Over Time:                                  │
│  sec                                                         │
│  300│                                                        │
│     │                                                        │
│  200│        ╭╮                                              │
│     │       ╭╯╰╮                                             │
│  100│  ╭───╯   ╰───╮                                        │
│     │╭╯             ╰───────────────────────                 │
│   0 │┴──────────────────────────────────────                 │
│     0h    2h    4h    6h    8h   10h   12h                   │
│                                                              │
│  Common Failure Reasons:                                     │
│  ┌──────────────────────────────────────┐                    │
│  │ Access denied (IAM):         45%     │                    │
│  │ Destination bucket not found: 25%    │                    │
│  │ KMS key not accessible:      15%     │                    │
│  │ Object too large:            10%     │                    │
│  │ Other:                        5%     │                    │
│  └──────────────────────────────────────┘                    │
│                                                              │
└──────────────────────────────────────────────────────────────┘

Alerts:
  - Replication lag > 15 min (with RTC) → SLA violation
  - Pending objects > 100,000 → queue backlog
  - Failed objects > 10/hour → check IAM/KMS permissions
  - Bytes pending > 100 GB → possible large upload spike
```

### 13.4 Troubleshooting Common CRR Issues

```
Issue 1: Replication not starting
  Checklist:
  □ Versioning enabled on source bucket?
  □ Versioning enabled on destination bucket?
  □ IAM role has s3:ReplicateObject permission?
  □ IAM role has s3:GetReplicationConfiguration on source?
  □ Destination bucket policy allows the IAM role? (cross-account)
  □ Replication rule status = Enabled?
  □ Object matches prefix/tag filter?

Issue 2: Replication lag increasing
  Possible causes:
  - Large object upload (multi-GB files take longer)
  - Burst of writes exceeding replication throughput
  - Cross-region network congestion
  - Destination region throttling
  Resolution:
  - Enable RTC for SLA guarantee
  - Check S3 Replication Metrics in CloudWatch
  - Contact AWS support for persistent issues

Issue 3: Objects not appearing in destination
  Check replication status on source object:
  $ aws s3api head-object --bucket source --key myfile.txt
  {
    "ReplicationStatus": "PENDING"  ← still in queue
    "ReplicationStatus": "COMPLETED" ← replicated successfully
    "ReplicationStatus": "FAILED"   ← check permissions
    "ReplicationStatus": "REPLICA"  ← this IS the replica
  }
```

---

## 14. Interview Quick-Reference Cheat Sheet

### 14.1 One-Liner Explanations

```
Consistency before 2020:
  "New PUTs were read-after-write consistent, but overwrite PUTs
   and DELETEs were eventually consistent due to front-end metadata
   caching."

Consistency after 2020:
  "All S3 operations are strongly read-after-write consistent, with
   no performance penalty, achieved via a lightweight witness protocol
   that validates cached versions before serving reads."

Witness protocol:
  "A distributed, strongly consistent version register that stores
   only key→version_id mappings. Reads check the witness before
   trusting their metadata cache. If the cache is stale, they fall
   back to the metadata primary."

Why no performance penalty:
  "The witness stores only version IDs (~100 bytes), so checks are
   sub-millisecond. The cache hit rate is >95%, so the fast path
   (cache matches witness) dominates. Net latency impact: <1ms."

CRR:
  "Asynchronous object replication across regions for DR and compliance.
   Eventually consistent — the destination may lag by seconds to minutes.
   With Replication Time Control, 99.99% of objects replicate within
   15 minutes."
```

### 14.2 Common Interview Questions

```
Q: "S3 used to be eventually consistent. What changed?"
A: In December 2020, Amazon introduced strong read-after-write
   consistency for all S3 operations. They achieved this using a
   witness protocol — a lightweight, strongly consistent version
   register. On every write, the version ID is recorded in the
   witness. On every read, the front-end checks the witness to
   validate its cache. If the cache is stale, it reads from the
   metadata primary. This adds <1ms overhead on >95% of reads.

Q: "Why was it eventually consistent in the first place?"
A: S3's front-end servers cached metadata locally for performance.
   When one server handled a write, other servers' caches weren't
   invalidated — they would serve stale data until their cache
   TTL expired. Broadcast invalidation doesn't scale (hundreds of
   servers × millions of writes/sec).

Q: "How does the witness achieve strong consistency without hurting
    performance?"
A: Three key insights:
   1. The witness stores ONLY version IDs, not full metadata
      (~100 bytes per entry vs ~1KB for full metadata)
   2. The witness is checked INSTEAD of invalidating caches
      (pull model vs push model)
   3. >95% of reads hit the "fast path" where the cache matches
      the witness, adding only ~0.3ms of overhead

Q: "What if the witness goes down?"
A: S3 likely falls back to reading directly from the metadata
   primary, which is always up-to-date. This maintains consistency
   at the cost of higher latency during the outage. The witness
   is a Paxos/Raft group across 3 AZs, so it tolerates single-AZ
   failures.

Q: "Is cross-region replication strongly consistent?"
A: No. CRR is asynchronous and eventually consistent. Strong
   consistency across regions would require synchronous cross-region
   writes, adding 70-100ms of latency per PUT (cross-Atlantic
   round trip). For most workloads, this latency is unacceptable.
   CRR is designed for DR and compliance, not real-time consistency.

Q: "How would you design a system that needs strong consistency
    across regions using S3?"
A: Use S3 for bulk data storage with CRR for replication, but add
   a strongly consistent metadata layer (like DynamoDB Global
   Tables) to track the latest version in each region. Reads check
   DynamoDB for the current version, then read that version from
   local S3. If the version hasn't replicated yet, fall back to
   reading from the source region.
```

### 14.3 Key Numbers to Remember

```
┌─────────────────────────────────────────────────────────────┐
│  S3 scale:       100+ trillion objects                      │
│  S3 throughput:  100+ million requests/second               │
│  Durability:     99.999999999% (11 nines)                   │
│  Availability:   99.99% (4 nines, Standard class)           │
│                                                             │
│  Witness entry:  ~100 bytes (key hash + version ID)         │
│  Witness check:  ~0.3 ms (cross-AZ network)                │
│  Cache hit rate: >95% (fast path dominates)                 │
│  Net overhead:   <1 ms on p50 latency                       │
│                                                             │
│  CRR latency:   seconds to minutes (best-effort)           │
│  CRR with RTC:  15 minutes SLA, 99.99%                     │
│  Cross-region:   ~70-100 ms network RTT                     │
│                                                             │
│  Paxos quorum:  Write 2/3, Read 2/3 → overlap guaranteed   │
└─────────────────────────────────────────────────────────────┘
```

---

## Cross-References

- [Interview Simulation](interview-simulation.md) — Full S3 system design interview walkthrough
- [Metadata & Indexing](metadata-and-indexing.md) — How S3 indexes and retrieves object metadata
- [Data Storage & Durability](data-storage-and-durability.md) — Erasure coding, AZ placement, 11 nines
- [Storage Classes & Lifecycle](storage-classes-and-lifecycle.md) — Tiered storage and transitions
- [System Flows](flow.md) — PUT, GET, DELETE, LIST request flows end-to-end
- [Scaling & Performance](scaling-and-performance.md) — Partitioning, request routing, prefix scaling
- [Security & Access Control](security-and-access-control.md) — IAM policies, bucket policies, encryption
- [API Contracts](api-contracts.md) — REST API design, headers, error codes

---

*This document is part of the Amazon S3 System Design series for interview preparation.*
