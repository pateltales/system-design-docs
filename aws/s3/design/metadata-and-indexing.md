# Amazon S3 — Metadata & Indexing Deep Dive

> Companion deep dive to the [interview simulation](interview-simulation.md). This document explores how S3 manages the metadata layer that maps bucket+key to object location across trillions of objects.

---

## Table of Contents

1. [The Metadata Problem at S3 Scale](#1-the-metadata-problem-at-s3-scale)
2. [Metadata Schema](#2-metadata-schema)
3. [Evolution of Partitioning Strategies](#3-evolution-of-partitioning-strategies)
4. [Auto-Partitioning Mechanics (Deep Dive)](#4-auto-partitioning-mechanics-deep-dive)
5. [The Partition Map](#5-the-partition-map)
6. [LIST Operation — How Prefix Scans Work](#6-list-operation--how-prefix-scans-work)
7. [Versioning in the Metadata Layer](#7-versioning-in-the-metadata-layer)
8. [Bucket Metadata](#8-bucket-metadata)
9. [Metadata for Multipart Uploads](#9-metadata-for-multipart-uploads)
10. [Consistency in the Metadata Layer](#10-consistency-in-the-metadata-layer)
11. [Metadata Compaction & Garbage Collection](#11-metadata-compaction--garbage-collection)
12. [Operational Concerns](#12-operational-concerns)
13. [Interview Cheat Sheet](#13-interview-cheat-sheet)

---

## 1. The Metadata Problem at S3 Scale

### 1.1 The Numbers

S3 stores over **100 trillion objects** as of 2024. Every single one of those objects
has a metadata record that the system must be able to locate in single-digit
milliseconds. Let's quantify the problem:

```
Objects:                  ~100 trillion  (10^14)
Avg metadata per object:  ~1 KB
Total metadata:           ~100 PB

Peak metadata lookups:    ~100 million / second (across all regions)
Write rate:               ~tens of millions of new objects / second

Uptime requirement:       99.99% (four nines)
Consistency:              Strong read-after-write (since Dec 2020)
```

### 1.2 What the Metadata Layer Must Support

The metadata layer is not a simple key-value store. It must support multiple
access patterns simultaneously:

| Access Pattern       | Operation          | Requirement                           |
|----------------------|--------------------|---------------------------------------|
| Point lookup         | GET object         | O(1) by (bucket, key)                 |
| Range scan           | LIST objects        | Sorted iteration by key prefix        |
| Version lookup       | GET ?versionId=X   | O(1) by (bucket, key, version)        |
| Version listing      | LIST versions       | Sorted iteration over version chain   |
| Existence check      | HEAD object        | Same as point lookup, metadata only   |
| Conditional read     | If-None-Match      | ETag comparison in metadata           |
| Bulk delete          | DELETE multi-object | Batch point updates                   |

### 1.3 Why This Is Hard

The challenge is the combination of requirements, not any single one in isolation:

```
Point lookups at 100M/sec                     --> hash-based sharding
   + Range scans (LIST) on sorted keys        --> range-based sharding
   + Strong consistency                       --> replication with consensus
   + Auto-scaling without manual intervention --> auto-partitioning
   + 99.99% availability                      --> no single point of failure
   + Multi-region                             --> cross-region replication

Each requirement pulls the design in a different direction.
The metadata system must satisfy ALL of them simultaneously.
```

### 1.4 The Core Insight

S3 solves this with a custom distributed index that uses:

1. **Range-based partitioning** (for LIST support)
2. **Automatic partition splitting** (for auto-scaling)
3. **Replicated state machines per partition** (for strong consistency)
4. **A cached partition map** (for fast routing)

The rest of this document explains each piece in detail.

---

## 2. Metadata Schema

### 2.1 Full Object Metadata Record

Every object stored in S3 has a metadata entry roughly like the following.
This is a logical representation; the physical encoding is a compact binary
format, not JSON.

```json
{
  "bucket_name": "my-bucket",
  "object_key": "photos/2024/vacation/img001.jpg",

  "version_id": "v3abc",
  "is_latest": true,
  "is_delete_marker": false,

  "etag": "d41d8cd98f00b204e9800998ecf8427e",
  "size": 4194304,
  "storage_class": "STANDARD",

  "owner_id": "account-12345",
  "acl": {
    "grants": [
      { "grantee": "account-12345", "permission": "FULL_CONTROL" },
      { "grantee": "AllUsers", "permission": "READ" }
    ]
  },

  "user_metadata": {
    "x-amz-meta-camera": "Canon EOS R5",
    "x-amz-meta-location": "Santorini, Greece"
  },

  "server_side_encryption": {
    "algorithm": "aws:kms",
    "key_id": "arn:aws:kms:us-east-1:123456789012:key/abcd-1234"
  },

  "content_type": "image/jpeg",
  "content_encoding": null,
  "cache_control": "max-age=86400",

  "chunk_map": [
    { "chunk_id": "c1a2b3", "node": "az-a/rack-3/node-17/disk-4", "offset": 0, "length": 4194304 },
    { "chunk_id": "c4d5e6", "node": "az-b/rack-7/node-22/disk-1", "offset": 0, "length": 4194304 },
    { "chunk_id": "c7e8f9", "node": "az-c/rack-1/node-05/disk-7", "offset": 0, "length": 4194304 },
    { "chunk_id": "ca1b2c", "node": "az-a/rack-9/node-31/disk-2", "offset": 0, "length": 4194304 },
    { "chunk_id": "cd3e4f", "node": "az-b/rack-2/node-11/disk-5", "offset": 0, "length": 4194304 },
    { "chunk_id": "cg5h6i", "node": "az-c/rack-5/node-28/disk-3", "offset": 0, "length": 4194304 },
    { "chunk_id": "cj7k8l", "node": "az-a/rack-6/node-09/disk-8", "offset": 0, "length": 4194304 },
    { "chunk_id": "cm9n0o", "node": "az-b/rack-4/node-15/disk-6", "offset": 0, "length": 4194304 },
    { "chunk_id": "cp1q2r", "node": "az-c/rack-8/node-33/disk-1", "offset": 0, "length": 2097152 },
    { "chunk_id": "cs3t4u", "node": "az-a/rack-1/node-02/disk-9", "offset": 0, "length": 2097152 },
    { "chunk_id": "cv5w6x", "node": "az-b/rack-3/node-19/disk-4", "offset": 0, "length": 2097152 }
  ],

  "created_at": "2024-07-15T10:30:00Z",
  "last_modified": "2024-07-15T10:30:00Z",

  "tags": {
    "department": "marketing",
    "project": "summer-campaign"
  },

  "object_lock": {
    "mode": null,
    "retain_until": null,
    "legal_hold": false
  },

  "checksum_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  "replication_status": "COMPLETED"
}
```

### 2.2 Schema Breakdown by Category

| Category           | Fields                                              | Approx Size |
|--------------------|-----------------------------------------------------|-------------|
| Identity           | bucket_name, object_key, version_id                 | ~200 B      |
| State flags        | is_latest, is_delete_marker                         | ~2 B        |
| Object properties  | etag, size, storage_class, content_type, encoding   | ~100 B      |
| Ownership & ACL    | owner_id, acl                                       | ~150 B      |
| User metadata      | x-amz-meta-* headers                                | ~200 B      |
| Encryption         | algorithm, key_id                                   | ~100 B      |
| Chunk map          | 11 entries (8+3 erasure coding)                     | ~500 B      |
| Timestamps         | created_at, last_modified                           | ~20 B       |
| Tags               | up to 10 key-value pairs                            | ~200 B      |
| Lock & compliance  | object_lock fields                                  | ~50 B       |
| Integrity          | checksum_sha256                                     | ~64 B       |
| Replication        | replication_status                                  | ~20 B       |
| **Total**          |                                                     | **~1.0 KB** |

### 2.3 The Chunk Map — Why It Matters

The `chunk_map` is the critical link between metadata and data. It tells S3
exactly which physical node holds each piece of the object.

```
Object: photos/vacation/img001.jpg  (32 MB)

  Chunk layout (8+3 erasure coding, 4 MB chunks):

  Data chunks (8):
  +---------+---------+---------+---------+---------+---------+---------+---------+
  | Chunk 0 | Chunk 1 | Chunk 2 | Chunk 3 | Chunk 4 | Chunk 5 | Chunk 6 | Chunk 7 |
  | 4 MB    | 4 MB    | 4 MB    | 4 MB    | 4 MB    | 4 MB    | 4 MB    | 4 MB    |
  | az-a    | az-b    | az-c    | az-a    | az-b    | az-c    | az-a    | az-b    |
  +---------+---------+---------+---------+---------+---------+---------+---------+

  Parity chunks (3):
  +-----------+-----------+-----------+
  | Parity P0 | Parity P1 | Parity P2 |
  | 4 MB      | 4 MB      | 4 MB      |
  | az-c      | az-a      | az-b      |
  +-----------+-----------+-----------+

  Any 3 chunks can be lost and the object is still recoverable.
  Each chunk_map entry stores: chunk_id, physical node path, offset, length.
```

Without the metadata (and specifically the chunk map), the data chunks on disk
are meaningless. This is why metadata is **more critical than data** — data can
be reconstructed from erasure coding; metadata cannot.

---

## 3. Evolution of Partitioning Strategies

This section walks through four increasingly sophisticated approaches to
partitioning the metadata index. The progression mirrors how a real engineering
team might iterate on the problem, and it is an excellent way to structure
your interview answer.

### 3.1 Attempt 1: Single Database

The simplest approach. Store all metadata in one big database.

```
                    +----------------------------+
  All requests ---->|     Single Database        |
                    |  (bucket, key) --> metadata |
                    +----------------------------+

  Problems:
  - Max ~50K queries/sec on a single node         (need 100M+)
  - Single point of failure                        (need 99.99%)
  - No horizontal scalability                      (need to grow with traffic)
  - Storage limited to single machine              (need 100 PB)
```

**Verdict:** Non-starter. Useful only to establish the baseline.

### 3.2 Attempt 2: Hash-Based Sharding

Distribute metadata across N partitions using a hash function.

```
  partition_id = hash(bucket_name + "/" + object_key) % N

  Client request for "my-bucket/photos/img001.jpg":
    hash("my-bucket/photos/img001.jpg") % 1000 = 347
    --> Route to Partition 347
```

**Point lookups:** Excellent. O(1) to find the partition, O(1) within the partition.

**But LIST is catastrophic:**

```
  Request: LIST my-bucket/?prefix=photos/

  Which partition has keys starting with "photos/"?
  Answer: ALL of them. Keys are scattered by hash.

  +------+  +------+  +------+  +------+       +------+
  | P0   |  | P1   |  | P2   |  | P3   |  ...  | P999 |
  | scan |  | scan |  | scan |  | scan |       | scan |
  +------+  +------+  +------+  +------+       +------+
     |         |         |         |               |
     +----+----+---------+----+----+-------...-----+
          |                   |
          v                   v
    merge & sort all results from 1000 partitions
          |
          v
    return first 1000 keys

  Fan-out: 1000 partitions scanned for a single LIST request
  Latency: determined by the slowest partition (tail latency)
  Cost:    1000x the work of a single-partition scan
```

**Verdict:** Good for point lookups, terrible for LIST. Since LIST is a core
S3 API, this design is unacceptable.

### 3.3 Attempt 3: Range-Based Partitioning (Manual)

Partition the key space into sorted ranges. Each partition owns a contiguous
key range.

```
  Key space: [all bucket/key combinations, sorted lexicographically]

  Partition P0: [a*, c*)     --> Node Group 0
  Partition P1: [c*, f*)     --> Node Group 1
  Partition P2: [f*, m*)     --> Node Group 2
  Partition P3: [m*, s*)     --> Node Group 3
  Partition P4: [s*, z*]     --> Node Group 4
```

**Point lookups:** Binary search on partition boundaries. O(log P) where P is
the number of partitions. Then O(1) within the partition.

**LIST:** Since keys are sorted within each partition, a prefix scan touches
only the partitions that cover the prefix range.

```
  Request: LIST my-bucket/?prefix=photos/

  Partition map lookup: "my-bucket/photos/" falls in P3 [m*, s*)
  --> Only scan P3 (or P3 + P4 if prefix spans boundary)

  +------+  +------+  +------+  +------+  +------+
  | P0   |  | P1   |  | P2   |  | P3   |  | P4   |
  | skip |  | skip |  | skip |  | SCAN |  | skip |
  +------+  +------+  +------+  +------+  +------+

  Fan-out: 1 partition (or a small number if prefix spans boundaries)
```

**The problem: manual range management.**

- Who decides the initial range boundaries?
- What happens when one prefix gets 100x more traffic than others?
- How do you split a hot partition without downtime?
- Who monitors partition load and triggers rebalancing?

```
  Example hot partition problem:

  Partition P2: [f*, m*)  owns the popular prefix "images/"
  All image uploads go to P2 --> P2 is overloaded

  +------+  +------+  +------+  +------+  +------+
  | P0   |  | P1   |  | P2   |  | P3   |  | P4   |
  | 1K/s |  | 2K/s |  |200K/s|  | 3K/s |  | 1K/s |
  +------+  +------+  +------+  +------+  +------+
                        ^^^^^^
                     HOT PARTITION
                     (manual intervention needed)
```

**Verdict:** LIST works. But manual management is operationally infeasible at
S3 scale with millions of buckets and unpredictable traffic patterns.

### 3.4 Attempt 4: Prefix-Based Auto-Partitioning (S3's Approach)

This is what S3 actually does. It combines range-based partitioning with
automatic splitting and merging.

**Key principles:**

1. Each bucket starts with **one partition** covering the full key range
2. S3 continuously monitors request rate per partition
3. When a partition exceeds a threshold, S3 **automatically splits** it
4. The split is transparent to clients — the routing table updates atomically
5. Under-utilized partitions can be **merged** back together

```
  New bucket "my-bucket" created:

  Time T0: 1 partition
  +--------------------------------------------------+
  |  P0: [my-bucket/*, my-bucket/~]                  |
  |  Handles ALL keys in the bucket                   |
  +--------------------------------------------------+

  Traffic grows on prefix "images/" ...

  Time T1: S3 auto-splits at median key
  +-------------------------+-------------------------+
  |  P0: [my-bucket/*,      |  P1: [my-bucket/m*,    |
  |       my-bucket/m*)     |       my-bucket/~]     |
  +-------------------------+-------------------------+

  Traffic concentrates on "images/2024/" ...

  Time T2: P0 splits again
  +------------+------------+-------------------------+
  |  P0: [*,   |  P2: [i*,  |  P1: [m*, ~]           |
  |      i*)   |      m*)   |                         |
  +------------+------------+-------------------------+

  Time T3: P2 splits further
  +------+------+------+------+-----------------------+
  | P0   | P2   | P3   | ...  |  P1                   |
  | [*,  | [i*, | [im*,|      |  [m*, ~]              |
  |  i*) |  im*)| in*) |      |                       |
  +------+------+------+------+-----------------------+
```

**Why this works:**

| Property             | How auto-partitioning delivers it                   |
|----------------------|-----------------------------------------------------|
| Auto-scaling         | Partitions split automatically when load grows       |
| No hot partitions    | Hot ranges keep splitting until load is balanced     |
| LIST efficiency      | Keys remain sorted within each range partition       |
| Point lookup speed   | Binary search on partition map + index within part.  |
| No manual work       | S3 decides when and where to split                  |
| Graceful cold start  | New bucket starts cheap (1 partition), grows on-demand|

---

## 4. Auto-Partitioning Mechanics (Deep Dive)

### 4.1 Split Detection

Each metadata partition reports metrics to a centralized monitoring plane:

```
  Metrics collected per partition:
  +------------------------------------------------------+
  | Metric                  | Sample Rate | Threshold    |
  +------------------------------------------------------+
  | Requests/sec (reads)    | Every 10s   | 5,500 req/s  |
  | Requests/sec (writes)   | Every 10s   | 3,500 req/s  |
  | Bytes scanned/sec       | Every 10s   | 50 MB/s      |
  | P99 latency             | Every 10s   | 100 ms       |
  | Hot key concentration   | Every 60s   | >20% on 1 key|
  +------------------------------------------------------+

  Split trigger:
  IF (requests/sec > threshold) for sustained_duration (e.g., 5 minutes)
  THEN initiate split
```

The sustained duration requirement prevents splits caused by brief spikes.
The system avoids unnecessary splits because each split has a small cost
(partition map update, brief routing uncertainty).

### 4.2 Split Point Selection

The split point determines where the key range is divided. S3 uses a strategy
that aims to balance load, not just key count:

```
  Strategy 1: Median Key
  -----------------------
  Pick the key that divides the partition into two halves by key count.

  Keys: [a, b, c, d, e, f, g, h, i, j]
  Median: e
  Left:  [a, b, c, d, e]   (5 keys)
  Right: [f, g, h, i, j]   (5 keys)

  Strategy 2: Median Request Weight (preferred)
  -----------------------------------------------
  Pick the key that divides the partition into two halves by request volume.

  Keys and their request counts:
  a:10, b:5, c:100, d:3, e:200, f:8, g:2, h:1, i:50, j:20
  Total: 399 requests
  Half:  ~200 requests

  Cumulative: a:10, b:15, c:115, d:118, e:318, f:326, ...
  Split at 'e' gives: Left=318 (80%), Right=81 (20%)   -- not balanced
  Split at 'd' gives: Left=118 (30%), Right=281 (70%)   -- better

  The algorithm picks the key where cumulative request weight ~ 50%.
```

### 4.3 Split Execution — Step by Step

```
  BEFORE SPLIT:
  =============

  Partition P0: key range [my-bucket/a*, my-bucket/z*]
  Served by: Node Group {N1, N2, N3}  (3-way replication)

  Partition Map (in coordination service):
  +-------+----------------------------+----------------+
  | Part  | Key Range                  | Node Group     |
  +-------+----------------------------+----------------+
  | P0    | [my-bucket/a*, my-bucket/~]| {N1, N2, N3}  |
  +-------+----------------------------+----------------+


  SPLIT DECISION:
  ===============

  Monitoring detects P0 at 8,000 req/s (threshold: 5,500)
  Duration: sustained for 5 minutes
  Median key (by request weight): "my-bucket/m"
  New partition ID: P7 (globally unique)
  Target node group for P7: {N4, N5, N6} (selected by placement algorithm)


  SPLIT EXECUTION SEQUENCE:
  =========================

  Step 1: Prepare new partition P7 on {N4, N5, N6}
          - Create empty partition state
          - Copy metadata entries for keys [my-bucket/m*, my-bucket/~] from P0
          - P0 continues serving all requests during copy

  Step 2: Catch-up replication
          - Any writes to P0 in range [m*, ~] during Step 1 are replayed to P7
          - Continue until P7 is within milliseconds of P0

  Step 3: Brief write pause on range [m*, ~] in P0  (tens of milliseconds)
          - Final catch-up: replicate last few writes to P7
          - P7 is now identical to P0 for range [m*, ~]

  Step 4: Atomic partition map update (coordination service)
          +-------+----------------------------+----------------+
          | Part  | Key Range                  | Node Group     |
          +-------+----------------------------+----------------+
          | P0    | [my-bucket/a*, my-bucket/m)| {N1, N2, N3}  |
          | P7    | [my-bucket/m*, my-bucket/~]| {N4, N5, N6}  |
          +-------+----------------------------+----------------+

  Step 5: Front-end cache invalidation
          - Coordination service notifies front-ends of map change
          - Front-ends refresh their cached partition map
          - Requests for [m*, ~] now route to P7

  Step 6: Cleanup
          - P0 deletes metadata entries for keys [m*, ~]
          - These entries are now owned by P7
          - P0's key range is now [a*, m)


  AFTER SPLIT:
  ============

  +-----------------------------+-----------------------------+
  | Partition P0                | Partition P7                |
  | Range: [a*, m)              | Range: [m*, ~]              |
  | Nodes: {N1, N2, N3}        | Nodes: {N4, N5, N6}        |
  | Load:  ~4,000 req/s        | Load:  ~4,000 req/s         |
  +-----------------------------+-----------------------------+
```

### 4.4 Handling Requests During Split

```
  Timeline during split:

  T0          T1          T2          T3          T4
  |-----------|-----------|-----------|-----------|
  | Copy data | Catch-up  | Pause+    | Map       |
  | to P7     | replay    | finalize  | update    |
  |           |           | (~50ms)   |           |

  Requests during T0-T2:
    All requests still go to P0 (map hasn't changed).
    P0 serves them normally.

  Requests during T2 (brief pause):
    Writes to keys in [m*, ~] are queued for ~50ms.
    Reads continue from P0's existing data.

  Requests at T3 (map update):
    Front-ends with OLD map: route [m*, ~] to P0
      --> P0 returns redirect to P7
      --> Front-end refreshes map and retries to P7

    Front-ends with NEW map: route directly to P7

    Within seconds, all front-ends have the new map.

  Requests after T4:
    All requests route correctly. Split is invisible.
```

### 4.5 Progressive Splitting Example

A bucket named `analytics` receives increasing traffic over several hours.
Here is how its partition count evolves:

```
  Hour 0: Bucket created
  +---------------------------------------------------------+
  |  P0: [analytics/*, analytics/~]                         |
  |  Traffic: 100 req/s                                     |
  +---------------------------------------------------------+


  Hour 1: Data ingestion pipeline starts writing events/2024/07/...
  +---------------------------------------------------------+
  |  P0: [analytics/*, analytics/~]                         |
  |  Traffic: 3,000 req/s  (below threshold)                |
  +---------------------------------------------------------+


  Hour 2: Traffic ramps up
  +---------------------------+-----------------------------+
  |  P0: [*, events/)         |  P1: [events/, ~]           |
  |  500 req/s                |  5,500 req/s  --> SPLITTING |
  +---------------------------+-----------------------------+


  Hour 3: P1 has split; traffic continues to grow
  +------------+-------------+-------------+----------------+
  |  P0        |  P1         |  P2         |  P3            |
  |  [*, e/)   |  [e/, ev/)  |  [ev/, f/)  |  [f/, ~]       |
  |  500 req/s |  2K req/s   |  6K req/s   |  500 req/s     |
  |            |             |  SPLITTING  |                |
  +------------+-------------+-------------+----------------+


  Hour 4: Further splits on the hot range
  +------+------+------+------+------+------+------+--------+
  | P0   | P1   | P4   | P5   | P2   | P6   | P7   | P3    |
  | [*,  | [e/, | [ev/,| [ev/,| [ev/,| [ev/,| [ev/,| [f/,  |
  |  e/) |  ev/)| 2024/| 2024/| 2024/| 2024/| 2024/|  ~]   |
  |      |      | 07/0)| 07/1)| 07/2)| 07/3)| 07/~)|       |
  +------+------+------+------+------+------+------+--------+
    500    500   3.5K   3.5K   3.5K   3.5K   3.5K    500
    req/s  req/s req/s  req/s  req/s  req/s  req/s  req/s

  Total: 8 partitions
  All partitions below threshold
  System is balanced
```

### 4.6 Partition Merging

When traffic decreases, partitions that were previously split can be merged
back together to reduce overhead:

```
  Merge trigger:
  IF two adjacent partitions have combined traffic < 30% of split threshold
  AND this has been sustained for 30+ minutes
  THEN merge them back into one partition

  This prevents oscillation (split-merge-split-merge cycles).
  The hysteresis (split at 100%, merge at 30%) ensures stability.
```

---

## 5. The Partition Map

### 5.1 What the Partition Map Stores

The partition map is the routing table that tells front-end servers which
metadata partition owns which key range.

```
  Partition Map Structure:
  +-------+---------------------+--------------------+---------+----------+
  | Part  | Key Range Start     | Key Range End      | Node    | Status   |
  | ID    | (inclusive)         | (exclusive)        | Group   |          |
  +-------+---------------------+--------------------+---------+----------+
  | P0    | analytics/          | analytics/events/  | {1,2,3} | ACTIVE   |
  | P1    | analytics/events/   | analytics/events/m | {4,5,6} | ACTIVE   |
  | P2    | analytics/events/m  | analytics/f        | {7,8,9} | ACTIVE   |
  | P3    | analytics/f         | analytics/~        | {1,5,8} | ACTIVE   |
  | P10   | photos/             | photos/2024/       | {2,4,7} | ACTIVE   |
  | P11   | photos/2024/        | photos/~           | {3,6,9} | ACTIVE   |
  | ...   | ...                 | ...                | ...     | ...      |
  +-------+---------------------+--------------------+---------+----------+
```

### 5.2 How Front-Ends Use the Partition Map

```
  Request: GET my-bucket/photos/2024/vacation/img001.jpg

  Step 1: Construct lookup key = "my-bucket/photos/2024/vacation/img001.jpg"

  Step 2: Binary search on partition map
          - Sorted by Key Range Start
          - Find the partition where:
            Key Range Start <= lookup key < Key Range End

  Step 3: Route request to the node group for that partition

  +------------------------------------------------------------------+
  |  Front-End Server (cached partition map)                         |
  |                                                                  |
  |  lookup_key = "my-bucket/photos/2024/vacation/img001.jpg"        |
  |                                                                  |
  |  Binary search:                                                  |
  |    photos/ <= photos/2024/vacation/... < photos/2024/ ?  NO      |
  |    photos/2024/ <= photos/2024/vacation/... < photos/~ ?  YES    |
  |    --> Partition P11, Node Group {3, 6, 9}                       |
  |                                                                  |
  |  Route to Node 3 (primary) in Group {3, 6, 9}                   |
  +------------------------------------------------------------------+
```

### 5.3 Partition Map Caching

```
  Partition Map Flow:

  +-------------------+          +----------------------+
  |  Coordination     |  push/   |  Front-End Server    |
  |  Service          |  pull    |  (cached map)        |
  |  (source of truth)|--------->|                      |
  +-------------------+          +------+---------------+
                                        |
                                        | binary search
                                        | on cached map
                                        v
                                 +------+---------------+
                                 |  Route to correct    |
                                 |  metadata partition  |
                                 +----------------------+

  Cache invalidation strategies:

  1. Push-based:  Coordination service pushes map updates to front-ends
                  via long-poll or watch mechanism.
                  Latency: sub-second.

  2. Pull-on-miss: If a front-end routes to a partition that says
                   "I don't own that key range anymore," the front-end
                   pulls the latest map and retries.
                   Handles stale caches gracefully.

  3. Periodic refresh: Every N seconds, front-ends pull the latest map
                       as a safety net.
```

### 5.4 Partition Map Size

```
  Per-partition entry: ~100 bytes (key range, partition ID, node list)

  Region with 10 million partitions:
    10,000,000 * 100 bytes = 1 GB

  This fits comfortably in memory on a front-end server.

  For extremely large regions, the map can be sharded:
    - First level: map by bucket name --> partition map shard
    - Second level: map by key range within bucket

  This two-level approach keeps the in-memory map per front-end
  to a manageable size (~100 MB).
```

---

## 6. LIST Operation — How Prefix Scans Work

### 6.1 Full Walkthrough

```
  Request: LIST my-bucket/?prefix=photos/2024/&delimiter=/&max-keys=1000


  Step 1: Front-end computes the key range for the prefix
  ========================================================

  Prefix: "photos/2024/"
  Range:  ["my-bucket/photos/2024/", "my-bucket/photos/2024/~")

  (Where "~" represents the highest possible character, meaning
   all keys starting with "photos/2024/" fall in this range.)


  Step 2: Partition map lookup
  ========================================================

  Front-end binary searches its cached partition map:

  +-------+--------------------------------+---------------------------+
  | Part  | Range                          | Covers our prefix?        |
  +-------+--------------------------------+---------------------------+
  | P8    | [my-bucket/photos/,            | YES (partially)           |
  |       |  my-bucket/photos/2024/07/)    |                           |
  +-------+--------------------------------+---------------------------+
  | P9    | [my-bucket/photos/2024/07/,    | YES (partially)           |
  |       |  my-bucket/photos/2025/)       |                           |
  +-------+--------------------------------+---------------------------+
  | P10   | [my-bucket/photos/2025/,       | NO                        |
  |       |  my-bucket/q)                  |                           |
  +-------+--------------------------------+---------------------------+

  Result: Prefix spans partitions P8 and P9.


  Step 3: Parallel range scans
  ========================================================

  +------------------------------------------------------------------+
  |                     Front-End Server                              |
  |                          |                                        |
  |              +-----------+-----------+                            |
  |              |                       |                            |
  |              v                       v                            |
  |  +-----------------------+  +-----------------------+            |
  |  | Partition P8          |  | Partition P9          |            |
  |  | Scan: keys matching   |  | Scan: keys matching   |            |
  |  | prefix "photos/2024/" |  | prefix "photos/2024/" |            |
  |  | Limit: 1000           |  | Limit: 1000           |            |
  |  +-----------------------+  +-----------------------+            |
  |              |                       |                            |
  |              v                       v                            |
  |  Results from P8:          Results from P9:                       |
  |  - photos/2024/01/img1    - photos/2024/07/img50                 |
  |  - photos/2024/01/img2    - photos/2024/07/img51                 |
  |  - photos/2024/02/img3    - photos/2024/08/img52                 |
  |  - ...                    - ...                                   |
  +------------------------------------------------------------------+


  Step 4: Merge sort
  ========================================================

  Front-end merges results from P8 and P9 in sorted order:

  Merged stream:
    photos/2024/01/img1.jpg
    photos/2024/01/img2.jpg
    photos/2024/02/img3.jpg
    photos/2024/02/img4.jpg
    ...
    photos/2024/07/img50.jpg
    photos/2024/07/img51.jpg
    photos/2024/08/img52.jpg
    ...


  Step 5: Delimiter processing
  ========================================================

  Delimiter is "/", prefix is "photos/2024/".
  For each key, extract the part after the prefix up to the next "/":

  Key: photos/2024/01/img1.jpg
       ^^^^^^^^^^^^             prefix (stripped)
                    ^^^         "01/" <-- text up to next delimiter
                       ^^^^^^^^ (rest ignored for CommonPrefixes)

  This key contributes CommonPrefix: "photos/2024/01/"

  Processing all keys:
  +-----------------------------------+--------------------------+
  | Key                               | CommonPrefix             |
  +-----------------------------------+--------------------------+
  | photos/2024/01/img1.jpg           | photos/2024/01/          |
  | photos/2024/01/img2.jpg           | photos/2024/01/          |
  | photos/2024/02/img3.jpg           | photos/2024/02/          |
  | photos/2024/07/img50.jpg          | photos/2024/07/          |
  | photos/2024/08/img52.jpg          | photos/2024/08/          |
  | photos/2024/readme.txt            | (no delimiter after      |
  |                                   |  prefix -> listed as     |
  |                                   |  Contents, not prefix)   |
  +-----------------------------------+--------------------------+

  Result:
  - CommonPrefixes: [photos/2024/01/, photos/2024/02/, photos/2024/07/, photos/2024/08/]
  - Contents: [photos/2024/readme.txt]

  (This simulates the "directory listing" experience: you see
   subdirectories as CommonPrefixes and files as Contents.)


  Step 6: Apply max-keys and build continuation token
  ========================================================

  If total results (CommonPrefixes + Contents) > 1000:
    Return first 1000 entries.
    IsTruncated: true
    NextContinuationToken: encode(last_key_returned, partition_state)

  If total results <= 1000:
    Return all entries.
    IsTruncated: false
```

### 6.2 Continuation Token Structure

```
  Continuation Token (opaque to client, meaningful to S3):

  {
    "last_key": "photos/2024/07/img999.jpg",
    "partition_hint": "P9",
    "scan_version": 42
  }

  - last_key:       Resume scanning from keys AFTER this one
  - partition_hint:  Which partition to start scanning from
  - scan_version:    Helps detect if partitions split since last page

  On next request:
  1. Decode token
  2. Look up partition for last_key in current partition map
     (may differ from partition_hint if a split happened)
  3. Resume scan from key > last_key
  4. Handle concurrent writes:
     - New keys inserted BEFORE last_key: already returned (or missed)
     - New keys inserted AFTER last_key: will be returned
     - No offset-based pagination: no skipped or duplicated results
```

### 6.3 Performance Characteristics of LIST

```
  Scenario                    Partitions Scanned   Notes
  ---------------------------------------------------------------
  Small bucket (1 partition)  1                    Fast

  Narrow prefix, 1 partition  1                    Best case

  Wide prefix spanning 5      5                    Parallel, merge sort
  partitions

  No prefix (list all keys)   All partitions       Worst case, but S3
                              in the bucket        handles it

  Deeply nested prefix        1 (usually)          Auto-partitioning puts
  with high traffic                                hot prefixes in their
                                                   own partitions
```

---

## 7. Versioning in the Metadata Layer

### 7.1 Version Chain Structure

When versioning is enabled on a bucket, S3 maintains a chain of versions for
each object key. Each version has its own metadata entry.

```
  Key: (my-bucket, config.json)
  Versioning: ENABLED

  Version chain (newest first):

  +------+-------------------+--------+------+-------------------+
  | Ver  | Version ID        | Type   | Size | Created           |
  +------+-------------------+--------+------+-------------------+
  | v4   | 4AaBbCc           | DELETE  | -    | 2024-07-15 14:00  |
  |      |                   | MARKER |      |                   |
  +------+-------------------+--------+------+-------------------+
  | v3   | 3DdEeFf           | OBJECT | 2048 | 2024-07-15 12:00  |
  +------+-------------------+--------+------+-------------------+
  | v2   | 2GgHhIi           | OBJECT | 1024 | 2024-07-14 09:00  |
  +------+-------------------+--------+------+-------------------+
  | v1   | 1JjKkLl           | OBJECT | 512  | 2024-07-13 16:00  |
  +------+-------------------+--------+------+-------------------+
```

### 7.2 How Version Operations Work

```
  Operation: GET config.json  (no version ID specified)
  -------------------------------------------------------
  1. Look up key "config.json" in partition
  2. Find latest version: v4
  3. v4 is a DELETE MARKER
  4. Return 404 Not Found

  Note: The object data for v1, v2, v3 still exists!
  The delete marker only hides the object from unversioned GETs.


  Operation: GET config.json?versionId=3DdEeFf
  -------------------------------------------------------
  1. Look up key "config.json", version "3DdEeFf"
  2. Find v3 (it is an OBJECT, not a delete marker)
  3. Use v3's chunk_map to retrieve data
  4. Return the 2048-byte object


  Operation: DELETE config.json  (no version ID)
  -------------------------------------------------------
  1. Do NOT delete anything
  2. Instead, INSERT a new delete marker as v5:
     +------+-------------------+--------+------+-------------------+
     | v5   | 5MmNnOo           | DELETE  | -    | 2024-07-15 15:00  |
     |      |                   | MARKER |      |                   |
     +------+-------------------+--------+------+-------------------+
  3. v5 becomes the new "latest"
  4. Subsequent GET config.json still returns 404


  Operation: DELETE config.json?versionId=2GgHhIi
  -------------------------------------------------------
  1. This is a PERMANENT DELETE of a specific version
  2. Remove v2's metadata entry from the index
  3. Schedule v2's data chunks for garbage collection
  4. Version chain becomes: v5, v4, v3, v1
  5. This operation requires special permissions
```

### 7.3 Storage Layout Options for Versions

```
  Option A: Single Row with Version Array
  ========================================

  Primary Key: (bucket, object_key)
  Value: {
    latest: v4,
    versions: [
      { version_id: v4, is_delete_marker: true, ... },
      { version_id: v3, size: 2048, chunk_map: [...], ... },
      { version_id: v2, size: 1024, chunk_map: [...], ... },
      { version_id: v1, size: 512, chunk_map: [...], ... }
    ]
  }

  Pros:
  - Single read to get all versions
  - Atomic update of "latest" pointer

  Cons:
  - Row size grows with version count
  - Objects with thousands of versions become large rows
  - Updating one version requires rewriting the entire row


  Option B: Separate Rows per Version (S3's likely approach)
  ==========================================================

  Partition Key: (bucket, object_key)
  Sort Key:      version_id (or timestamp, reverse-ordered)

  Row 1: (my-bucket, config.json, v4) -> { is_delete_marker: true, ... }
  Row 2: (my-bucket, config.json, v3) -> { size: 2048, chunk_map: [...] }
  Row 3: (my-bucket, config.json, v2) -> { size: 1024, chunk_map: [...] }
  Row 4: (my-bucket, config.json, v1) -> { size: 512,  chunk_map: [...] }

  Separate entry for "latest" pointer:
  Row 0: (my-bucket, config.json, LATEST) -> { version_id: v4 }

  Pros:
  - Each version is an independent row (bounded size)
  - Can efficiently fetch a specific version
  - Handles objects with millions of versions

  Cons:
  - "Get latest" requires two reads (LATEST pointer, then version row)
  - Must maintain the LATEST pointer atomically with version writes


  Comparison Table:
  +---------------------------+------------------+------------------+
  | Criterion                 | Option A (Array) | Option B (Rows)  |
  +---------------------------+------------------+------------------+
  | Get latest version        | 1 read           | 2 reads          |
  | Get specific version      | 1 read + scan    | 1 read           |
  | List all versions         | 1 read           | Range scan       |
  | Add new version           | Read-modify-write| Insert + update  |
  | Max versions per object   | ~1000 (row size) | Unlimited        |
  | Delete specific version   | Read-modify-write| Delete row       |
  | Storage per version       | Compact (shared) | Slight overhead  |
  +---------------------------+------------------+------------------+

  At S3 scale, Option B is almost certainly used because some objects
  can have millions of versions (e.g., frequently-updated config files
  in versioned buckets).
```

---

## 8. Bucket Metadata

### 8.1 Bucket Registry

Bucket metadata is stored separately from object metadata. It is a much smaller
dataset but has unique consistency requirements.

```
  Bucket Registry Entry:
  {
    "bucket_name": "my-bucket",
    "owner_account_id": "123456789012",
    "region": "us-east-1",
    "creation_date": "2024-01-15T08:00:00Z",
    "versioning": "Enabled",
    "lifecycle_rules": [
      {
        "id": "archive-old-logs",
        "prefix": "logs/",
        "transitions": [
          { "days": 30, "storage_class": "GLACIER" }
        ],
        "expiration": { "days": 365 }
      }
    ],
    "replication_config": { ... },
    "encryption_default": { "algorithm": "aws:kms", "key_id": "..." },
    "public_access_block": {
      "block_public_acls": true,
      "block_public_policy": true,
      "ignore_public_acls": true,
      "restrict_public_buckets": true
    },
    "logging": { "target_bucket": "access-logs-bucket", "prefix": "my-bucket/" },
    "tags": { "environment": "production", "team": "platform" }
  }
```

### 8.2 Global Bucket Name Uniqueness

S3 bucket names are globally unique across ALL AWS accounts and ALL regions.
This is a surprisingly hard problem.

```
  Challenge: Two users in different regions try to create "my-cool-bucket"
             at the same time.

  Solution: Global Bucket Name Registry

  +-------------------+     +-------------------+     +-------------------+
  | us-east-1         |     | eu-west-1         |     | ap-southeast-1    |
  | Regional S3       |     | Regional S3       |     | Regional S3       |
  +--------+----------+     +--------+----------+     +--------+----------+
           |                         |                         |
           +------------+------------+------------+------------+
                        |                         |
                        v                         v
               +----------------------------------+
               |  Global Bucket Name Registry     |
               |                                  |
               |  "my-cool-bucket" -> us-east-1   |
               |  "other-bucket"   -> eu-west-1   |
               |  ...                             |
               +----------------------------------+

  CreateBucket flow:
  1. Client sends CreateBucket("my-cool-bucket") to us-east-1
  2. us-east-1 sends claim to Global Registry
  3. Registry checks: name already taken?
     - YES: return BucketAlreadyExists error
     - NO:  atomically register name -> us-east-1, return success
  4. us-east-1 creates the bucket's regional metadata

  The Global Registry uses a consensus protocol (likely Paxos/Raft)
  to ensure that concurrent claims from different regions are serialized.
```

### 8.3 Bucket vs. Object Metadata Comparison

```
  +---------------------------+-----------------------+-----------------------+
  | Property                  | Bucket Metadata       | Object Metadata       |
  +---------------------------+-----------------------+-----------------------+
  | Count                     | ~Billions             | ~100+ Trillion        |
  | Total size                | ~TBs                  | ~100 PB               |
  | Access pattern            | Read-heavy, rare write| Read and write heavy  |
  | Uniqueness scope          | Global                | Per-bucket            |
  | Update frequency          | Rare (config changes) | Every PUT/DELETE      |
  | Partitioning needed       | Minimal               | Extensive             |
  | Consistency requirement   | Strong                | Strong                |
  +---------------------------+-----------------------+-----------------------+
```

---

## 9. Metadata for Multipart Uploads

### 9.1 Multipart Upload Lifecycle

Large objects (up to 5 TB) are uploaded in parts. Each multipart upload has
its own metadata that tracks the in-progress state.

```
  Multipart Upload Lifecycle:

  1. InitiateMultipartUpload
     Client: POST /my-bucket/large-file.zip?uploads
     S3: Creates upload metadata, returns upload_id

  2. UploadPart (repeated for each part)
     Client: PUT /my-bucket/large-file.zip?partNumber=1&uploadId=abc123
     S3: Stores part data, records part metadata

  3. CompleteMultipartUpload
     Client: POST /my-bucket/large-file.zip?uploadId=abc123
             Body: list of (partNumber, ETag) pairs
     S3: Composes parts into final object, creates object metadata

  OR

  3. AbortMultipartUpload
     Client: DELETE /my-bucket/large-file.zip?uploadId=abc123
     S3: Marks upload for cleanup, schedules garbage collection
```

### 9.2 In-Progress Upload Metadata

```
  Upload Metadata Entry:
  {
    "upload_id": "abc123",
    "bucket": "my-bucket",
    "key": "large-file.zip",
    "initiated_by": "account-12345",
    "initiated_at": "2024-07-15T10:00:00Z",
    "storage_class": "STANDARD",
    "encryption": { "algorithm": "aws:kms", "key_id": "..." },
    "parts": [
      {
        "part_number": 1,
        "etag": "a54357aff0632cce46d942af68356b38",
        "size": 104857600,
        "chunk_map": [
          { "chunk_id": "tmp-p1-c1", "node": "az-a/rack-2/node-5/disk-3", ... },
          ...
        ],
        "uploaded_at": "2024-07-15T10:01:00Z"
      },
      {
        "part_number": 2,
        "etag": "b64468bgg1743ddf57e053bg79467c49",
        "size": 104857600,
        "chunk_map": [ ... ],
        "uploaded_at": "2024-07-15T10:02:00Z"
      },
      ...
    ],
    "status": "IN_PROGRESS"
  }
```

### 9.3 Completion: Parts to Object

```
  CompleteMultipartUpload Processing:

  Step 1: Validate
  - Check all part numbers are present and contiguous
  - Verify ETags match what was uploaded
  - Ensure each part >= 5 MB (except the last)

  Step 2: Compose chunk map
  - Concatenate chunk_maps from all parts in order
  - This becomes the final object's chunk_map

  Step 3: Create object metadata
  - Compute composite ETag: MD5(part1_etag + part2_etag + ...)-N
    (The "-N" suffix indicates it was a multipart upload)
  - Set size = sum of all part sizes
  - Set chunk_map = composed chunk_map
  - Insert into object metadata index

  Step 4: Clean up upload metadata
  - Mark upload as COMPLETED
  - Delete temporary part metadata
  - The part data itself is already in the right place
    (no data copying needed!)


  Before completion:                    After completion:

  Upload Metadata:                      Object Metadata:
  +------------------+                  +---------------------------+
  | upload_id: abc123|                  | key: large-file.zip       |
  | parts:           |                  | etag: abcd1234-3          |
  |   Part 1: 100MB  |   COMPOSE       | size: 250MB               |
  |   Part 2: 100MB  |  --------->     | chunk_map:                |
  |   Part 3: 50MB   |                 |   [part1 chunks...        |
  +------------------+                  |    part2 chunks...        |
                                        |    part3 chunks...]       |
  (temporary, deleted                   +---------------------------+
   after completion)                    (permanent, in object index)
```

### 9.4 Lifecycle Policy for Abandoned Uploads

Multipart uploads that are never completed (or aborted) leave orphaned parts
consuming storage. S3 provides a lifecycle rule to handle this:

```
  Lifecycle Rule:
  {
    "id": "abort-incomplete-uploads",
    "status": "Enabled",
    "abort_incomplete_multipart_upload": {
      "days_after_initiation": 7
    }
  }

  Background Process (daily):
  1. Scan upload metadata for uploads older than 7 days
  2. For each expired upload:
     a. Mark status as ABORTING
     b. Schedule all part data chunks for garbage collection
     c. Delete upload metadata
     d. Mark status as ABORTED (then remove entry)

  This prevents storage leaks from abandoned uploads.
```

---

## 10. Consistency in the Metadata Layer

### 10.1 The Consistency Challenge

Before December 2020, S3 provided **eventual consistency** for overwrite PUTs
and DELETEs. This meant:

```
  BEFORE (eventual consistency):

  Time T0: PUT my-key (version 2)        --> Writes to primary
  Time T1: GET my-key                     --> Might return version 1!
  Time T2: GET my-key                     --> Might return version 2
  Time T3: GET my-key                     --> Returns version 2 (replicated)

  The window T0-T3 could be seconds to minutes.
  This caused subtle bugs in applications that expected read-after-write.
```

After December 2020, S3 provides **strong read-after-write consistency** for
all operations, at no additional cost or performance penalty.

```
  AFTER (strong consistency):

  Time T0: PUT my-key (version 2)        --> Writes to primary, ACK
  Time T1: GET my-key                     --> ALWAYS returns version 2

  No stale reads. No exceptions. No extra cost.
```

### 10.2 The Witness Protocol

S3 achieves strong consistency using a mechanism called the **witness protocol**
(referenced in AWS publications). Here is how it works:

```
  Components:

  +------------------+     +------------------+     +------------------+
  |  Primary         |     |  Witness         |     |  Read Replicas   |
  |  (metadata store)|     |  (lightweight)   |     |  (cached copies) |
  +------------------+     +------------------+     +------------------+


  WRITE PATH:
  ============

  Client: PUT my-key (new metadata)

  Step 1: Front-end routes to correct metadata partition
  Step 2: Primary node writes the new metadata
  Step 3: Primary sends write notification to Witness
          Witness records: "my-key was written at timestamp T"
  Step 4: Primary ACKs the write to the client

  +--------+          +---------+          +---------+
  | Client | --PUT--> | Primary | --notify-> | Witness |
  |        | <--ACK-- |  (write)|          | (record)|
  +--------+          +---------+          +---------+

  Note: The write is NOT replicated to read replicas synchronously.
  Read replicas receive updates asynchronously (eventually).


  READ PATH:
  ============

  Client: GET my-key

  Step 1: Front-end routes to read replica (for performance)
  Step 2: Read replica returns its cached metadata for my-key
          along with the replica's "freshness timestamp"
  Step 3: Front-end checks with Witness:
          "Is this replica's data for my-key fresh enough?"
          Witness compares replica timestamp vs. last write timestamp
  Step 4a: If fresh (replica timestamp >= last write timestamp):
           Return the data from the replica. DONE.
  Step 4b: If stale (replica timestamp < last write timestamp):
           Read directly from Primary. Return that result.

  +--------+          +---------+          +---------+
  | Client | --GET--> | Replica | --check-> | Witness |
  |        |          | (read)  |          |         |
  |        |          |         | <-fresh-- |         |
  |        | <--data- | (return)|          |         |
  +--------+          +---------+          +---------+

  OR (if stale):

  +--------+          +---------+          +---------+          +---------+
  | Client | --GET--> | Replica | --check-> | Witness |          |         |
  |        |          | (read)  |          |         |          |         |
  |        |          |         | <-stale-- |         |          |         |
  |        |          |    redirect to primary        |          |         |
  |        | <--data--+---------------------------read---------> | Primary |
  +--------+          +---------+          +---------+          +---------+
```

### 10.3 Why the Witness Protocol Is Efficient

```
  Key insight: The Witness is LIGHTWEIGHT.

  The Witness does NOT store the actual metadata.
  It only stores: { key -> last_write_timestamp }

  This is tiny compared to the full metadata.
  A Witness node can track billions of keys in memory.

  Performance characteristics:
  +---------------------------+-----------------------------------+
  | Operation                 | Cost                              |
  +---------------------------+-----------------------------------+
  | Write (Primary + Witness) | 2 synchronous writes (fast)       |
  | Read (cache hit, fresh)   | 1 read + 1 Witness check (fast)  |
  | Read (cache miss / stale) | 1 read from Primary (same as old) |
  +---------------------------+-----------------------------------+

  In steady state, most reads hit the replica cache and the Witness
  confirms freshness. Only recently-written keys trigger a primary read.

  This means strong consistency adds near-zero latency overhead
  for the common case (read a key that hasn't been recently written).
```

### 10.4 Cache Coherence for Front-End Servers

Front-end servers cache partition maps and sometimes hot metadata entries.
Cache coherence is maintained through:

```
  Level 1: Partition Map Cache
  - Updated via push from coordination service on split/merge
  - Fallback: redirect from partition that no longer owns a key
  - TTL: minutes (but usually refreshed by push before TTL expires)

  Level 2: Hot Metadata Cache (optional, per front-end)
  - Short TTL (seconds)
  - Invalidated by Witness protocol (stale check prevents serving old data)
  - Used to reduce load on metadata partitions for extremely hot objects

  Level 3: CDN / CloudFront Integration
  - CloudFront caches object data, NOT metadata
  - Metadata is always resolved by S3 front-ends
  - This ensures consistency: CloudFront may serve stale data, but only
    within the Cache-Control window set by the user
```

---

## 11. Metadata Compaction & Garbage Collection

### 11.1 Tombstones

When an object is deleted (or a version is removed), the metadata entry is not
immediately erased. Instead, a **tombstone** is written.

```
  DELETE my-key (non-versioned bucket):

  Before:
  +----------------------------+
  | key: my-key                |
  | size: 4096                 |
  | chunk_map: [...]           |
  | last_modified: T1          |
  +----------------------------+

  After:
  +----------------------------+
  | key: my-key                |
  | TOMBSTONE                  |
  | deleted_at: T2             |
  | retain_until: T2 + 7 days |
  +----------------------------+

  Why not delete immediately?

  1. Replication: Other replicas need to learn about the deletion.
     If we delete the entry, replicas won't know to delete their copies.
     The tombstone replicates just like any other write.

  2. Consistency: The Witness needs to know the key was deleted
     so it can correctly handle reads (return 404, not stale data).

  3. Conflict resolution: In rare split-brain scenarios, the tombstone
     with a newer timestamp wins over an older version of the data.
```

### 11.2 Compaction Process

```
  Background Compaction (runs continuously):

  +-------------------------------------------------------------------+
  |  Metadata Partition P5                                            |
  |                                                                   |
  |  Live entries:                                                    |
  |    key-a -> { size: 100, ... }                                   |
  |    key-b -> { size: 200, ... }                                   |
  |    key-c -> TOMBSTONE (deleted 8 days ago)    <-- eligible        |
  |    key-d -> { size: 300, ... }                                   |
  |    key-e -> TOMBSTONE (deleted 2 days ago)    <-- NOT eligible    |
  |    key-f -> { size: 400, ... }                                   |
  |    key-g -> TOMBSTONE (deleted 10 days ago)   <-- eligible        |
  |                                                                   |
  |  Compaction:                                                      |
  |  1. Scan for tombstones older than retention period (7 days)      |
  |  2. Verify tombstone has been replicated to all replicas          |
  |  3. Remove tombstone from index                                   |
  |  4. Free the space in the storage engine (LSM compaction / etc.)  |
  |                                                                   |
  |  After compaction:                                                |
  |    key-a -> { size: 100, ... }                                   |
  |    key-b -> { size: 200, ... }                                   |
  |    key-d -> { size: 300, ... }                                   |
  |    key-e -> TOMBSTONE (deleted 2 days ago)    <-- keep for now    |
  |    key-f -> { size: 400, ... }                                   |
  +-------------------------------------------------------------------+
```

### 11.3 Version Expiration via Lifecycle Policies

```
  Lifecycle Rule:
  {
    "id": "expire-old-versions",
    "status": "Enabled",
    "noncurrent_version_expiration": {
      "noncurrent_days": 90,
      "newer_noncurrent_versions": 3
    }
  }

  This means: Keep the latest 3 noncurrent versions. Delete any
  noncurrent version older than 90 days (beyond the 3 kept).

  Processing:

  Key: config.json
  Versions:
    v10 (current, latest)       --> KEEP (it's the current version)
    v9  (noncurrent, 10 days)   --> KEEP (within top 3 noncurrent)
    v8  (noncurrent, 30 days)   --> KEEP (within top 3 noncurrent)
    v7  (noncurrent, 60 days)   --> KEEP (within top 3 noncurrent)
    v6  (noncurrent, 95 days)   --> DELETE (beyond top 3, older than 90 days)
    v5  (noncurrent, 120 days)  --> DELETE
    v4  (noncurrent, 180 days)  --> DELETE
    ...

  Deletion process:
  1. Lifecycle scanner identifies expired versions
  2. For each expired version:
     a. Delete metadata entry (or write tombstone)
     b. Schedule data chunks referenced by that version for GC
  3. Background GC process deletes the actual data chunks
```

### 11.4 Orphaned Data Chunk Garbage Collection

Sometimes metadata is deleted but the referenced data chunks remain on disk.
This can happen due to:
- Crashes during deletion
- Metadata compaction completing before data deletion
- Split-brain recovery scenarios

```
  Garbage Collection Process:

  +------------------+                    +------------------+
  | Metadata Index   |                    | Data Storage     |
  | (source of truth)|                    | (chunk servers)  |
  +--------+---------+                    +--------+---------+
           |                                       |
           | 1. Enumerate all referenced chunk_ids |
           +-------------------------------------->|
           |                                       |
           | 2. Chunk server checks:               |
           |    "Which of my chunks are NOT in the |
           |     referenced set?"                  |
           |<--------------------------------------+
           |                                       |
           | 3. Unreferenced chunks = orphans       |
           |    Schedule for deletion after grace    |
           |    period (e.g., 24 hours)             |
           |                                       |
           | 4. Delete orphans after grace period    |
           +-------------------------------------->|
           |                                       |

  Grace Period:
  - An orphaned chunk might actually be referenced by an in-flight
    write that hasn't committed its metadata yet.
  - The 24-hour grace period ensures we don't delete chunks that
    are still being written.
  - Chunks older than grace period with no metadata reference are
    safely deletable.


  Frequency and Cost:
  +---------------------------+-----------------------------------+
  | Aspect                    | Detail                            |
  +---------------------------+-----------------------------------+
  | Scan frequency            | Weekly per partition              |
  | Scan cost                 | Full metadata scan + chunk list   |
  | Typical orphan rate       | < 0.001% of chunks               |
  | Storage recovered         | Significant at S3 scale          |
  +---------------------------+-----------------------------------+
```

---

## 12. Operational Concerns

### 12.1 Monitoring Dashboard

```
  S3 Metadata Operations Dashboard
  =====================================

  PARTITION HEALTH
  +---------------------------------+----------------------------------+
  | Metric                          | Current Value | Alarm Threshold  |
  +---------------------------------+----------------------------------+
  | Total partitions (region)       | 12,345,678    | N/A (info)       |
  | Partitions > 80% capacity       | 234           | > 500            |
  | Partitions > 90% request rate   | 56            | > 100            |
  | Splits in last hour             | 89            | > 500 / hour     |
  | Merges in last hour             | 12            | N/A (info)       |
  | Failed splits                   | 0             | > 0              |
  +---------------------------------+----------------------------------+

  CONSISTENCY
  +---------------------------------+----------------------------------+
  | Metric                          | Current Value | Alarm Threshold  |
  +---------------------------------+----------------------------------+
  | Witness check latency P50       | 0.3 ms        | > 5 ms           |
  | Witness check latency P99       | 2.1 ms        | > 50 ms          |
  | Stale read rate (redirects)     | 0.02%         | > 1%             |
  | Replication lag (max)           | 45 ms         | > 1000 ms        |
  +---------------------------------+----------------------------------+

  PERFORMANCE
  +---------------------------------+----------------------------------+
  | Metric                          | Current Value | Alarm Threshold  |
  +---------------------------------+----------------------------------+
  | Metadata GET latency P50        | 1.2 ms        | > 10 ms          |
  | Metadata GET latency P99        | 8.5 ms        | > 100 ms         |
  | Metadata PUT latency P50        | 3.1 ms        | > 20 ms          |
  | Metadata PUT latency P99        | 15.2 ms       | > 200 ms         |
  | LIST latency P50                | 12 ms         | > 100 ms         |
  | LIST latency P99                | 85 ms         | > 1000 ms        |
  +---------------------------------+----------------------------------+

  GARBAGE COLLECTION
  +---------------------------------+----------------------------------+
  | Metric                          | Current Value | Alarm Threshold  |
  +---------------------------------+----------------------------------+
  | Tombstones pending compaction   | 45,678,901    | > 1 billion      |
  | Orphaned chunks detected        | 12,345        | > 1 million      |
  | GC backlog age (oldest)         | 3 days        | > 14 days        |
  +---------------------------------+----------------------------------+
```

### 12.2 Common Operational Alarms

```
  ALARM: Hot Partition Not Splitting
  ===================================
  Condition: Partition has request rate > 2x threshold for > 15 minutes
             AND no split has been initiated.

  Likely causes:
  1. All keys in the partition have the same prefix (can't find a good split point)
  2. Split target nodes are unavailable or overloaded
  3. Bug in split detection logic

  Runbook:
  1. Check if the partition has a single hot key (can't be split further)
     --> If yes: rate-limit the key, contact the customer
  2. Check if target node groups have capacity
     --> If no: provision more nodes or redirect to different node group
  3. Manual split: force a split at a specified key


  ALARM: Metadata Replication Lag > 1 second
  ==========================================
  Condition: At least one partition's replicas are > 1 second behind primary.

  Likely causes:
  1. Network partition between primary and replica
  2. Replica node overloaded (disk I/O, CPU)
  3. Large batch write saturating replication bandwidth

  Impact:
  - Witness protocol will redirect reads to primary (higher latency)
  - If sustained: partition may become unavailable if primary fails

  Runbook:
  1. Check network connectivity between primary and replica nodes
  2. Check replica node health (CPU, disk, memory)
  3. If node is unhealthy: initiate replica replacement


  ALARM: GC Backlog Growing
  ==========================
  Condition: Orphaned chunks or tombstones accumulating faster than being cleaned.

  Impact:
  - Wasted storage (paying for data that's logically deleted)
  - Eventual metadata bloat (tombstones slow down scans)

  Runbook:
  1. Check GC worker health and throughput
  2. Increase GC worker count if needed
  3. Check for pathological deletion patterns (e.g., mass lifecycle expiration)
```

### 12.3 Capacity Planning

```
  Metadata Storage Growth Model:

  Current:
    Objects:        100 trillion
    Metadata size:  ~100 PB
    Metadata nodes: ~50,000 (estimated)

  Growth rate:
    New objects/day:      ~1 trillion (net, after deletions)
    Metadata growth/day:  ~1 PB
    Node additions/month: ~500

  Planning horizons:
  +------------------+------------------+------------------+
  | Timeframe        | Projected Objects| Metadata Size    |
  +------------------+------------------+------------------+
  | Current          | 100 trillion     | 100 PB           |
  | +6 months        | 280 trillion     | 280 PB           |
  | +1 year          | 460 trillion     | 460 PB           |
  | +2 years         | 1 quadrillion    | 1 EB             |
  +------------------+------------------+------------------+

  Capacity actions:
  - 6 months out: Order hardware, provision data centers
  - 3 months out: Deploy and burn-in new nodes
  - 1 month out:  Start routing new partitions to new nodes
  - Continuous:   Auto-partitioning handles load distribution
```

### 12.4 Metadata Backup and Disaster Recovery

```
  Why metadata backup is critical:

  Data chunks on disk:    Can be reconstructed from erasure coding (8+3)
                          Losing 3 of 11 chunks is survivable.

  Metadata in the index:  CANNOT be reconstructed.
                          If metadata is lost, we don't know which chunks
                          belong to which object, or where they are stored.
                          The data becomes meaningless bits on disk.

  Backup strategy:

  +------------------------------------------------------------------+
  | Layer           | Mechanism                | RPO        | RTO     |
  +------------------------------------------------------------------+
  | In-region       | 3-way synchronous        | 0          | Seconds |
  | replication     | replication per partition |            |         |
  +------------------------------------------------------------------+
  | Cross-AZ        | Replicas spread across   | 0          | Seconds |
  | (within region) | 3 Availability Zones     |            |         |
  +------------------------------------------------------------------+
  | Point-in-time   | Continuous WAL shipping   | Seconds    | Minutes |
  | recovery        | to backup storage         |            |         |
  +------------------------------------------------------------------+
  | Cross-region    | Asynchronous replication  | Minutes    | Hours   |
  | (for CRR)      | for cross-region buckets  |            |         |
  +------------------------------------------------------------------+
  | Cold backup     | Periodic full snapshots   | Hours      | Hours   |
  |                 | to archival storage       |            |         |
  +------------------------------------------------------------------+

  RPO = Recovery Point Objective (how much data could be lost)
  RTO = Recovery Time Objective (how long to recover)
```

---

## 13. Interview Cheat Sheet

### Quick Reference: How to Explain S3 Metadata in 2 Minutes

```
  "S3 stores metadata for 100+ trillion objects. Each metadata entry is
   about 1 KB and maps (bucket, key) to the physical chunk locations.

   The index is range-partitioned so that LIST operations (prefix scans)
   are efficient -- keys are sorted within each partition.

   Partitions auto-split when they get hot: S3 monitors request rates
   and splits at the median key, transparently updating the routing table.
   A new bucket starts with one partition and grows as needed.

   Front-end servers cache the partition map (which is just a sorted list
   of key ranges to partition IDs) and do a binary search on each request.

   Strong consistency is achieved through a Witness protocol: writes go
   to the primary and a lightweight Witness node. Reads check the Witness
   to see if their cached copy is fresh; if stale, they read from primary.

   Versioning stores each version as a separate row, with a LATEST pointer.
   Multipart uploads track parts in temporary metadata until completion.
   Garbage collection handles tombstones, orphaned chunks, and expired
   versions via background processes."
```

### Key Numbers to Remember

```
  +-------------------------------------+------------------------+
  | Metric                              | Value                  |
  +-------------------------------------+------------------------+
  | Total objects                        | 100+ trillion          |
  | Metadata per object                  | ~1 KB                  |
  | Total metadata                       | ~100 PB                |
  | Metadata lookups/sec                 | 100M+                  |
  | Partition split threshold            | ~5,500 req/s           |
  | Erasure coding ratio                 | 8+3 (8 data, 3 parity) |
  | Chunk map entries per object         | 11 (for 8+3)           |
  | Witness check latency               | < 1 ms (P50)           |
  | Metadata GET latency                 | ~1-2 ms (P50)          |
  | Metadata PUT latency                 | ~3-5 ms (P50)          |
  | Partition map size (per region)      | ~100 MB - 1 GB         |
  | Tombstone retention                  | ~7 days                |
  | Orphan GC grace period              | ~24 hours              |
  +-------------------------------------+------------------------+
```

### Common Interview Follow-Up Questions

```
  Q: "Why not just use DynamoDB for metadata?"
  A: S3 predates DynamoDB. And at S3's scale, a general-purpose database
     would add unnecessary overhead. S3's custom index is optimized for
     exactly its access patterns: point lookups + range scans + versioning.
     Also, S3 would become a circular dependency if it depended on DynamoDB.

  Q: "How does LIST handle pagination with concurrent writes?"
  A: Continuation tokens encode the last key returned, not an offset.
     New keys inserted AFTER the last key will appear in subsequent pages.
     New keys inserted BEFORE the last key may be missed for that listing
     but will appear in a new listing. This is consistent with S3's
     documented behavior.

  Q: "What happens if the Witness is unavailable?"
  A: The system falls back to reading from the primary directly.
     This adds latency but maintains correctness. The Witness is itself
     replicated (multiple Witness nodes per partition) so this is rare.

  Q: "How do you handle a key that gets millions of requests per second?"
  A: A single key cannot be split further. Options:
     1. Caching at the front-end layer for reads
     2. Rate limiting for writes
     3. The customer can use CloudFront for read-heavy patterns
     4. S3 Intelligent-Tiering can optimize the storage class
     This is a fundamental limitation: a single key is a single entity.

  Q: "How does auto-partitioning know when to STOP splitting?"
  A: Splitting stops when all partitions are below the threshold.
     The system converges because each split halves the load per partition.
     After log2(peak_rate / threshold) splits, every partition is below
     the threshold. For 1M req/s with a 5K threshold, that's about 8 splits
     (2^8 = 256 partitions, each handling ~4K req/s).
```

---

## Footer

### Cross-References

| Document                                                                 | Topic                                     |
|--------------------------------------------------------------------------|-------------------------------------------|
| [Interview Simulation](interview-simulation.md)                          | Full S3 system design walkthrough         |
| [Consistency & Replication](consistency-and-replication.md)              | Witness protocol, quorum writes           |
| [Storage Engine Deep Dive](storage-engine-deep-dive.md)                 | Erasure coding, chunk placement, data path|
| [Networking & Request Routing](networking-and-request-routing.md)       | DNS, load balancing, front-end fleet      |
| [Security & Access Control](security-and-access-control.md)             | IAM, bucket policies, encryption at rest  |

---

*This document is part of the Amazon S3 System Design series. It focuses exclusively on the metadata and indexing layer. For the full system design including data storage, networking, and security, see the interview simulation.*
