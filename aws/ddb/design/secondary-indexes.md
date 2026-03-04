# DynamoDB Secondary Indexes — Deep Dive

## Table of Contents

1. [Overview](#1-overview)
2. [Global Secondary Indexes (GSI)](#2-global-secondary-indexes-gsi)
3. [Local Secondary Indexes (LSI)](#3-local-secondary-indexes-lsi)
4. [GSI vs LSI Comparison](#4-gsi-vs-lsi-comparison)
5. [Write Amplification](#5-write-amplification)
6. [Sparse Indexes](#6-sparse-indexes)
7. [Projection Strategies](#7-projection-strategies)
8. [GSI Throttle Cascading](#8-gsi-throttle-cascading)
9. [Design Patterns](#9-design-patterns)
10. [Interview Angles](#10-interview-angles)

---

## 1. Overview

DynamoDB supports two types of secondary indexes to enable alternative query patterns
beyond the base table's primary key:

| Feature | GSI | LSI |
|---------|-----|-----|
| **Partition key** | Can differ from base table | Same as base table |
| **Sort key** | Can differ from base table | Different from base table |
| **When created** | Anytime | Table creation only |
| **Limit per table** | 20 (default, adjustable) | 5 |
| **Size limit** | Unlimited | 10 GB per item collection |
| **Consistency** | Eventually consistent only | EC and SC |
| **Throughput** | Own RCU/WCU | Shares with base table |
| **Implementation** | Separate internal table | Co-located on same partition |

---

## 2. Global Secondary Indexes (GSI)

### 2.1 Architecture: GSI as a Separate Table

A GSI is implemented as a **separate internal table** maintained by DynamoDB:

```
┌────────────────────────────────┐     ┌────────────────────────────────┐
│         Base Table              │     │         GSI                    │
│  PK: UserId   SK: OrderId      │     │  PK: Email     SK: CreatedAt  │
├────────────────────────────────┤     ├────────────────────────────────┤
│                                │     │                                │
│  Partition by: hash(UserId)    │     │  Partition by: hash(Email)    │
│  Own B-tree per partition       │     │  Own B-tree per partition      │
│  Own Paxos replication          │     │  Own Paxos replication         │
│  3 replicas across AZs         │     │  3 replicas across AZs        │
│                                │     │                                │
│  ──── Async replication ────▶  │     │  Updated asynchronously       │
│                                │     │  from base table writes        │
└────────────────────────────────┘     └────────────────────────────────┘
```

### 2.2 Async Replication Flow

```
Client writes to base table:
  │
  ▼
  PutItem(UserId="U001", OrderId="O123", Email="a@b.com", Amount=50)
  │
  ├─ 1. Base table: Paxos commit → 200 OK to client
  │     (synchronous — client waits for this)
  │
  └─ 2. GSI: Async propagation
        ├─ DynamoDB extracts GSI key attributes (Email, CreatedAt)
        ├─ DynamoDB writes to GSI partition hash(Email)
        └─ Propagation: typically < 1 second
           (but no guarantee — could be slower under load)
```

### 2.3 Key Schema

GSI can have completely different keys from the base table:

```
Base Table:             GSI-1:                 GSI-2:
PK = UserId             PK = Email              PK = Status
SK = OrderId            SK = CreatedAt          SK = Amount

Query patterns:
  Base: "Get all orders for user U001"
  GSI-1: "Get all orders by email a@b.com, sorted by date"
  GSI-2: "Get all PENDING orders, sorted by amount"
```

### 2.4 GSI Throughput

GSIs have their **own provisioned throughput**, independent of the base table:

```json
{
  "GlobalSecondaryIndexes": [
    {
      "IndexName": "EmailIndex",
      "KeySchema": [
        {"AttributeName": "Email", "KeyType": "HASH"},
        {"AttributeName": "CreatedAt", "KeyType": "RANGE"}
      ],
      "Projection": {"ProjectionType": "ALL"},
      "ProvisionedThroughput": {
        "ReadCapacityUnits": 100,
        "WriteCapacityUnits": 50
      }
    }
  ]
}
```

**Critical:** GSI write throughput must be sufficient to handle all base table writes
that affect the GSI. If GSI write capacity is insufficient, **base table writes are
throttled** (see Section 8).

### 2.5 GSI Read Cost

GSI reads are always eventually consistent:

```
Cost: 0.5 RCU per 4 KB (same as EC reads on base table)

Example:
  Query GSI: returns 8 items × 2 KB each = 16 KB
  Cost: ceil(16 / 4) × 0.5 = 2 RCU
```

### 2.6 Supported Operations on GSI

| Operation | Supported | Notes |
|-----------|-----------|-------|
| Query | Yes | Key condition on GSI PK, optional SK condition |
| Scan | Yes | Full GSI scan with optional filter |
| GetItem | **No** | Cannot do point reads on GSI |
| BatchGetItem | **No** | Cannot batch-get from GSI |
| PutItem/UpdateItem/DeleteItem | **No** | Writes go through base table only |

---

## 3. Local Secondary Indexes (LSI)

### 3.1 Architecture: Co-located with Base Table

LSIs are stored **on the same partition** as the base table data:

```
┌──────────────────────────────────────────────────┐
│     Partition (UserId = "U001")                   │
│                                                   │
│  Base table data (sorted by OrderId):             │
│    {UserId: "U001", OrderId: "O001", ...}        │
│    {UserId: "U001", OrderId: "O002", ...}        │
│    {UserId: "U001", OrderId: "O003", ...}        │
│                                                   │
│  LSI-1 data (sorted by CreatedAt):               │
│    {UserId: "U001", CreatedAt: "2024-01-01", ...}│
│    {UserId: "U001", CreatedAt: "2024-01-15", ...}│
│    {UserId: "U001", CreatedAt: "2024-02-01", ...}│
│                                                   │
│  LSI-2 data (sorted by Amount):                   │
│    {UserId: "U001", Amount: 10.00, ...}          │
│    {UserId: "U001", Amount: 25.00, ...}          │
│    {UserId: "U001", Amount: 50.00, ...}          │
│                                                   │
│  TOTAL for this partition key ≤ 10 GB            │
│  (base + all LSI data for UserId = "U001")       │
│                                                   │
└──────────────────────────────────────────────────┘
```

### 3.2 Key Differences from GSI

| Property | LSI | GSI |
|----------|-----|-----|
| Partition key | **Same** as base table | Can differ |
| Sort key | **Different** from base table | Can differ |
| Data location | Same partition as base table | Separate internal table |
| Consistency | EC and **SC** | EC only |
| Throughput | **Shared** with base table | Own provisioned throughput |
| Size limit | **10 GB per item collection** | Unlimited |
| Creation time | **Table creation only** | Anytime |

### 3.3 Why SC Reads Work on LSI

Because LSI data lives on the **same partition** as the base table:
- Same leader handles both base table and LSI writes
- Both are updated in the same Paxos commit
- Leader's B-tree includes both base and LSI data
- SC read can read the latest from the leader

### 3.4 The 10 GB Item Collection Limit

An **item collection** = all items with the same partition key value across the base
table AND all LSIs:

```
Item collection size for PK = "U001":
  Base table items: 3 GB
  LSI-1 items:      2 GB
  LSI-2 items:      1.5 GB
  ─────────────────────
  Total:            6.5 GB  ← OK (< 10 GB)

If total exceeds 10 GB:
  → ItemCollectionSizeLimitExceededException
  → Cannot add more items or increase item sizes for PK = "U001"
  → Can still delete items or reduce item sizes
```

**Monitoring:**
```json
{
  "ReturnItemCollectionMetrics": "SIZE"
}
```

Set alarms at 8 GB to get early warning before hitting the 10 GB limit.

### 3.5 Fetch Behavior

When querying an LSI with a projection that doesn't include all requested attributes:

```
Query LSI (projection = KEYS_ONLY) requesting attributes A, B:

  1. LSI returns: PK + SK + LSI SK  (keys only)
  2. Attributes A, B not in projection
  3. DynamoDB automatically FETCHES from base table
     → Additional read cost for each item
     → Additional latency

  Cost = LSI read cost + base table fetch cost
```

**Best practice:** Project all attributes you'll need in queries to avoid fetch overhead.

### 3.6 Cannot Be Added After Table Creation

LSIs must be defined at `CreateTable` time. This is a one-time, permanent decision
because LSIs share the partition with the base table, and retrofitting the storage
layout after data exists would require migrating all data.

---

## 4. GSI vs LSI Comparison

### 4.1 Feature Matrix

| Feature | GSI | LSI |
|---------|-----|-----|
| Partition key | Any attribute | Same as base table |
| Sort key | Any attribute | Different from base table |
| Consistency | EC only | EC + SC |
| Throughput | Separate provisioned | Shared with base table |
| Size limit | Unlimited | 10 GB per item collection |
| Max per table | 20 (adjustable) | 5 (hard limit) |
| When to create | Anytime | Table creation only |
| Write cost | Separate WCU consumed | Shares base table WCU |
| Backfill on creation | Yes (async) | N/A (must be at creation) |
| Throttle isolation | Can throttle base table | Shares throttling with base |
| Sparse index support | Yes | Yes |
| Point reads (GetItem) | No | No |

### 4.2 Decision Framework

```
Need a different partition key?
  └─ YES → Must use GSI

Same partition key, different sort key?
  ├─ Need strongly consistent reads?
  │   └─ YES → LSI (if item collection < 10 GB)
  │
  ├─ Item collection might exceed 10 GB?
  │   └─ YES → GSI (no size limit)
  │
  ├─ Want isolated throughput?
  │   └─ YES → GSI
  │
  └─ Default → LSI (simpler, SC support, lower overhead)
```

### 4.3 When to Prefer GSI Over LSI

1. **Different access pattern requires a different partition key**
   - Base: PK=UserId, Query: "Find all orders by status" → GSI with PK=Status

2. **Item collections may exceed 10 GB**
   - Hot partition keys with lots of data → LSI would hit limit

3. **Need throughput isolation**
   - Don't want index reads to compete with base table reads

4. **Need to add an index to an existing table**
   - LSI can't be added after creation

### 4.4 When to Prefer LSI

1. **Need strongly consistent reads on the index**
   - Only LSI supports SC

2. **Same partition key, different sort order**
   - LSI is simpler and has less overhead

3. **Small item collections (< 10 GB per PK)**
   - No size concern

4. **Want to avoid write amplification to a separate table**
   - LSI writes are part of the base table write (no separate WCU)

---

## 5. Write Amplification

### 5.1 The Problem

Every base table write that affects an indexed attribute causes additional writes to
the affected indexes:

```
Table with 3 GSIs:

PutItem to base table:
  1 WCU to base table
  + 1 WCU to GSI-1 (if item has GSI-1 key attributes)
  + 1 WCU to GSI-2 (if item has GSI-2 key attributes)
  + 1 WCU to GSI-3 (if item has GSI-3 key attributes)
  ─────────────────
  Total: up to 4 WCU for one write
```

### 5.2 Write Scenarios

| Scenario | Base Table WCU | GSI WCU | Total |
|----------|---------------|---------|-------|
| New item, all GSI keys present | 1 | 1 per GSI | 1 + N |
| Update non-indexed attribute | 1 | 0 | 1 |
| Update GSI key attribute (old → new) | 1 | 2 per affected GSI (delete old + put new) | 1 + 2N |
| Delete item with GSI entries | 1 | 1 per GSI | 1 + N |
| Item has no GSI key attributes | 1 | 0 | 1 |

### 5.3 Cost Example

```
Table: Orders
  Base table: 1,000 writes/sec × 1 WCU = 1,000 WCU
  GSI-1 (by Email):  1,000 writes/sec × 1 WCU = 1,000 WCU
  GSI-2 (by Status): 1,000 writes/sec × 1 WCU = 1,000 WCU
  GSI-3 (by Date):   1,000 writes/sec × 1 WCU = 1,000 WCU

  Total: 4,000 WCU for 1,000 logical writes
  Write amplification factor: 4x

  With 5 GSIs: 6x write amplification
  With 10 GSIs: 11x write amplification
```

### 5.4 Minimizing Write Amplification

1. **Fewer GSIs:** Only create indexes you actually need
2. **Sparse indexes:** Use attributes that are only present on some items
3. **Single-table design:** Use overloaded GSI keys to serve multiple access patterns
   with fewer indexes
4. **Batch writes:** BatchWriteItem is slightly more efficient than individual PutItems
5. **KEYS_ONLY projection:** Reduces index item size (less WCU per write)

---

## 6. Sparse Indexes

### 6.1 How Sparse Indexes Work

If an item doesn't have the GSI/LSI key attribute, it's **not included** in the index:

```
Base Table:
  {UserId: "U001", OrderId: "O001", Premium: true, Amount: 100}
  {UserId: "U002", OrderId: "O002", Amount: 50}                  ← no Premium attribute
  {UserId: "U003", OrderId: "O003", Premium: true, Amount: 200}
  {UserId: "U004", OrderId: "O004", Amount: 25}                  ← no Premium attribute

GSI: PK = Premium, SK = Amount

GSI contents (sparse — only 2 items!):
  {Premium: true, Amount: 100, UserId: "U001", OrderId: "O001"}
  {Premium: true, Amount: 200, UserId: "U003", OrderId: "O003"}

Query GSI: "Get all premium orders sorted by amount"
  → Only scans 2 items instead of 4
  → Much more efficient than scanning base table with filter
```

### 6.2 Use Cases

| Pattern | Approach |
|---------|---------|
| Flag-based queries | Only add attribute (e.g., `IsActive`) when true |
| Optional relationships | Only index items that have the relationship |
| Status transitions | Index only items in specific states (e.g., `Pending`) |
| Rare attributes | Index items with optional metadata |

### 6.3 Cost Benefit

Sparse indexes save both storage and write costs:
- Fewer items in the index → less storage
- Writes without the index key → no GSI write cost
- Queries scan fewer items → less RCU

---

## 7. Projection Strategies

### 7.1 Three Projection Types

| Type | What's Stored | Storage Cost | Query Flexibility |
|------|--------------|-------------|-------------------|
| `KEYS_ONLY` | Base PK + SK + Index PK + SK | Lowest | Keys only, fetch for other attrs |
| `INCLUDE` | Keys + specified non-key attributes | Medium | Selected attributes, fetch for others |
| `ALL` | All base table attributes | Highest | Full flexibility, no fetch needed |

### 7.2 Storage Overhead Per Index Item

```
Index item size =
  Base table PK bytes
  + Base table SK bytes (if composite key)
  + Index PK bytes
  + Index SK bytes (if present)
  + Projected attribute bytes
  + 100 bytes overhead
```

### 7.3 Decision Framework

```
How often do you query this index?
  ├─ Rarely → KEYS_ONLY (minimize write cost)
  │
  └─ Frequently →
      │
      How many attributes do you need from the query?
        ├─ Just keys → KEYS_ONLY
        ├─ A few specific attributes → INCLUDE (list them)
        └─ Most/all attributes → ALL
```

### 7.4 INCLUDE Projection Limit

You can project up to **100 attributes total across all LSIs and GSIs** using the
INCLUDE projection type (per table).

---

## 8. GSI Throttle Cascading

### 8.1 The Problem

This is one of the most dangerous operational issues with DynamoDB GSIs:

```
┌──────────────────────────────────────────────────────┐
│          GSI Throttle Cascading                       │
├──────────────────────────────────────────────────────┤
│                                                      │
│  Base Table: 1,000 WCU provisioned                  │
│  GSI-1:       200 WCU provisioned  ← under-provisioned! │
│                                                      │
│  Scenario:                                           │
│  1. Application writes 500 items/sec to base table  │
│  2. All items have GSI-1 key attributes             │
│  3. GSI-1 needs 500 WCU but only has 200           │
│  4. GSI-1 replication falls behind                   │
│  5. DynamoDB back-pressures BASE TABLE writes       │
│  6. Base table writes start getting throttled!       │
│                                                      │
│  Result: Under-provisioned GSI throttles the ENTIRE │
│  base table, even though the base table has plenty  │
│  of capacity.                                        │
│                                                      │
└──────────────────────────────────────────────────────┘
```

### 8.2 Why This Happens

DynamoDB must keep GSIs reasonably in sync with the base table. If the GSI falls too
far behind, DynamoDB applies back-pressure to the base table writes to prevent unbounded
lag. This is a deliberate design choice to prevent GSIs from becoming arbitrarily stale.

### 8.3 Prevention

1. **GSI WCU ≥ Base table write rate** that affects the GSI
2. **Use on-demand mode** — both base table and GSI scale automatically
3. **Monitor GSI write throttling:** `ThrottledRequests` metric per GSI
4. **Auto-scaling on GSI** with appropriate target utilization (70%)
5. **Sparse indexes** reduce the number of writes that affect the GSI

### 8.4 Diagnosis

```
Symptoms:
  - Base table writes getting throttled
  - Base table has plenty of provisioned WCU
  - GSI ThrottledRequests metric is high

CloudWatch metrics to check:
  - Per-GSI: ConsumedWriteCapacityUnits vs ProvisionedWriteCapacityUnits
  - Per-GSI: ThrottledRequests
  - Base table: WriteThrottleEvents (may be caused by GSI!)
```

---

## 9. Design Patterns

### 9.1 Single-Table Design with Overloaded GSI

Instead of many GSIs, use one GSI with overloaded keys:

```
Base Table:
  PK         | SK              | GSI1PK      | GSI1SK
  USER#U001  | PROFILE         | EMAIL#a@b   | USER#U001
  USER#U001  | ORDER#O001      | STATUS#PEND | 2024-01-15
  USER#U001  | ORDER#O002      | STATUS#SHIP | 2024-01-20
  PRODUCT#P1 | METADATA        | CAT#ELECTR  | PRODUCT#P1

GSI-1 (PK = GSI1PK, SK = GSI1SK):
  → Query: "Find user by email"     → PK = EMAIL#a@b
  → Query: "Find pending orders"    → PK = STATUS#PEND
  → Query: "Find products in Electronics" → PK = CAT#ELECTR

One GSI serves multiple access patterns!
```

### 9.2 GSI Overloading

```
Problem: Need 5 different query patterns → 5 GSIs → 5x write amplification

Solution: Overload 1-2 GSIs:
  GSI1PK and GSI1SK contain different values depending on the entity type
  → Same GSI serves different queries based on the key prefix
  → Reduces GSI count from 5 to 1-2
  → Dramatically reduces write amplification
```

### 9.3 Inverted Index Pattern

```
Base Table:
  PK = UserId, SK = GroupId  → "Which groups does user U001 belong to?"

Need: "Which users belong to group G001?"

GSI (inverted):
  PK = GroupId, SK = UserId  → Swap PK and SK

One GSI gives you the inverse relationship.
```

### 9.4 GSI Write Sharding for Hot GSI Partitions

If a GSI partition key is hot (e.g., Status = "ACTIVE"):

```
Instead of:
  GSI PK = Status  → "ACTIVE" partition is hot

Use:
  GSI PK = Status#Shard  → "ACTIVE#1", "ACTIVE#2", ..., "ACTIVE#10"

  Write: random shard suffix
  Read:  parallel query all 10 shards, aggregate
```

---

## 10. Interview Angles

### 10.1 "Explain GSI vs LSI"

"A GSI is implemented as a separate internal table with its own partitions and throughput.
It can have a completely different partition key and sort key from the base table, and
data is replicated asynchronously — so it only supports eventually consistent reads.
An LSI shares the same partition as the base table with a different sort key. Because it's
co-located, it supports both EC and SC reads, but is limited to 10 GB per item collection
and must be created at table creation time."

### 10.2 "What's GSI throttle cascading and how do you prevent it?"

"If a GSI's write throughput is insufficient to keep up with base table writes, DynamoDB
back-pressures the base table to prevent the GSI from falling too far behind. This means
an under-provisioned GSI can throttle your entire base table, even if the base table has
plenty of capacity. Prevention: provision GSI WCU at least equal to the base table write
rate, or use on-demand mode. Monitor per-GSI ThrottledRequests in CloudWatch."

### 10.3 "Why are GSI reads eventually consistent only?"

"GSIs are separate internal tables updated asynchronously from the base table. When you
write to the base table, the Paxos commit happens on the base table's partitions. The
GSI update is a separate asynchronous write to different partitions. Because the GSI data
is always slightly behind the base table, strong consistency isn't possible without
making the GSI update synchronous, which would add latency to every base table write
proportional to the number of GSIs."

### 10.4 "A customer's item collection is approaching 10 GB with LSI. What do you recommend?"

```
1. Immediate: Monitor with ReturnItemCollectionMetrics = SIZE
   Set alarm at 8 GB for early warning

2. Short term: Reduce item sizes
   - Archive old items to S3
   - Compress large attributes
   - Remove unused attributes

3. Long term: Migrate from LSI to GSI
   - GSI has no item collection size limit
   - Tradeoff: lose SC reads on the index
   - Or: redesign partition key for better distribution

4. If SC reads on index are critical:
   - Redesign the partition key to distribute data across more partitions
   - Each partition key value's collection stays under 10 GB
```

### 10.5 "How do you decide how many GSIs to create?"

```
Factors:
  1. Write amplification: N GSIs → up to (1+N)x write cost
  2. Storage: each GSI duplicates projected data
  3. Operational complexity: each GSI needs capacity management

Strategy:
  1. Start with 0 GSIs — can you serve all queries from the base table?
  2. Use single-table design with overloaded GSI keys to minimize index count
  3. Sparse indexes to reduce write amplification
  4. KEYS_ONLY projection to minimize storage
  5. On-demand mode to avoid GSI throttle cascading

Rule of thumb: most tables need 1-3 GSIs.
If you need 10+, consider redesigning your data model.
```

---

## Appendix: Key Numbers

| Property | Value |
|----------|-------|
| GSIs per table | 20 (default, adjustable) |
| LSIs per table | 5 (hard limit) |
| LSI item collection size | 10 GB per partition key value |
| Projected attributes (INCLUDE) | 100 total across all indexes |
| Index item overhead | 100 bytes per item |
| GSI consistency | Eventually consistent only |
| LSI consistency | EC + SC |
| GSI throughput | Separate from base table |
| LSI throughput | Shared with base table |
| GSI creation | Anytime (with async backfill) |
| LSI creation | Table creation only |
| Supported operations | Query + Scan only (no GetItem) |
